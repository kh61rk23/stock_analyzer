# ==============================================================
# facility_expansion_monitor.py  v1.0  (I-4)
# ==============================================================
# 【役割】
#   主要自治体の建設許可情報・環境省の環境影響評価データ・
#   経産省の工場立地動向から、大規模施設（工場・物流拠点・
#   データセンター・研究所等）の着工シグナルを生成する。
#
# 【シグナル生成ロジック】
#   1. 環境省 環境影響評価データベース・経産省統計をスクレイピング
#   2. FACILITY_SECTOR_MAP のキーワードで着工種別を判定する
#   3. 面積 >= FACILITY_MIN_AREA_SQM の大規模案件のみ対象とする
#   4. 対象業種コードに FACILITY_EXPANSION_BONUS を加算する
#   5. FACILITY_EXPANSION_TTL_DAYS を超えたシグナルは自動失効する
#
# 【キャッシュ】
#   CACHE_DIR/facility/facility_project_cache.json
#     TTL: FACILITY_CACHE_TTL_DAYS（180日）
#     スキーマ:
#       {
#         "last_updated":   str,
#         "projects":       [{name, location, area_sqm, type, sectors, ...}],
#         "sector_boosts":  {sector_code: float},
#         "active_signals": [{...}],   # TTL 内のシグナル
#       }
#
# 【実行頻度】
#   stock_analyzer_main.py から週次バッチ（月曜）で呼ばれる。
#   TTL 180日以内はキャッシュを返す。
#
# 【依存】
#   config.py (BLOCK 26: FACILITY_SECTOR_MAP / FACILITY_MIN_AREA_SQM /
#              FACILITY_EXPANSION_BONUS / FACILITY_EXPANSION_TTL_DAYS /
#              FACILITY_CACHE_TTL_DAYS / FACILITY_CACHE_PATH /
#              FACILITY_DATA_SOURCES)
#   requests / re（標準ライブラリ）
#
# 【呼び出し方（stock_analyzer_main.py から）】
#   _fem = _load_local_module("facility_expansion_monitor", ...)
#   fem_result = _fem.run_facility_expansion_monitor(cfg, results)
#   results["facility_expansion"] = fem_result
# ==============================================================

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


# ==============================================================
# BLOCK 1: キャッシュ管理
# ==============================================================

def _facility_cache_dir(cfg) -> Path:
    """facility キャッシュディレクトリを返す（なければ作成）。"""
    base = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache"))
    d = base / "facility"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_cache(cfg) -> dict:
    """
    facility_project_cache.json を読み込む。
    TTL 切れの場合は空 dict を返す。

    Returns:
        {
          "last_updated":   str,
          "projects":       [{...}],
          "sector_boosts":  {sector_code: float},
          "active_signals": [{...}],
        }
    """
    fname = getattr(cfg, "FACILITY_CACHE_PATH",
                    "facility_project_cache.json")
    p     = _facility_cache_dir(cfg) / fname
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        ttl  = int(getattr(cfg, "FACILITY_CACHE_TTL_DAYS", 180))
        last = data.get("last_updated", "")
        if last:
            age = (datetime.now() - datetime.fromisoformat(last)).days
            if age > ttl:
                return {}
        return data
    except Exception as e:
        print(f"    ⚠️  [facility] キャッシュ読み込み失敗: {str(e)[:60]}")
        return {}


def _save_cache(cfg, data: dict) -> None:
    """facility_project_cache.json を保存する。"""
    fname = getattr(cfg, "FACILITY_CACHE_PATH",
                    "facility_project_cache.json")
    p     = _facility_cache_dir(cfg) / fname
    try:
        data["last_updated"] = datetime.now().isoformat()
        p.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"    ⚠️  [facility] キャッシュ保存失敗: {str(e)[:60]}")


# ==============================================================
# BLOCK 2: HTTP 取得ユーティリティ
# ==============================================================

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
}
from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'B5_FACILITY_EXPANSION_MONITOR_REQUEST_TIMEOUT': '_REQUEST_TIMEOUT',
    'B6_FACILITY_EXPANSION_MONITOR_REQUEST_WAIT': '_REQUEST_WAIT',
}

_REQUEST_TIMEOUT = 20
_REQUEST_WAIT    = 2.0


def _safe_get(url: str, params: dict = None,
              timeout: int = _REQUEST_TIMEOUT) -> Optional[str]:
    """
    requests.get を try/except で保護し HTML テキストを返す。
    失敗時は None を返す。
    """
    try:
        import requests  # type: ignore
        resp = requests.get(
            url,
            params  = params,
            headers = _DEFAULT_HEADERS,
            timeout = timeout,
        )
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        print(f"    ⚠️  [facility] HTTP取得失敗 ({url[:60]}): {str(e)[:50]}")
        return None


# ==============================================================
# BLOCK 3: 環境影響評価データベース スクレイパー
# ==============================================================

def _scrape_env_assessment() -> list[dict]:
    """
    環境省 環境影響評価情報支援ネットワーク から
    大規模工場・施設の環境アセス対象事業を取得する。

    対象URL: https://www.env.go.jp/policy/assess/

    取得する情報:
      - 事業名（project_name）
      - 事業種別（project_type）: 工場・発電所・道路等
      - 事業者名（company_name）
      - 都道府県（prefecture）
      - 告示日（announce_date）

    Returns:
        [{"project_name": str, "project_type": str, "company_name": str,
          "prefecture": str, "announce_date": str, "area_sqm": float,
          "source": "env_assessment"}, ...]
    """
    projects: list[dict] = []
    base_url  = "https://www.env.go.jp/policy/assess/3-1various/jigyo/index.html"
    html      = _safe_get(base_url)

    if not html:
        # フォールバック: 環境省RSS・報道発表を参照
        fallback_url = "https://www.env.go.jp/press/rss.xml"
        rss = _safe_get(fallback_url)
        if rss:
            # RSS から環境アセス関連を抽出
            titles = re.findall(
                r"<title><!\[CDATA\[([^\]]*(?:工場|施設|アセス|環境影響)[^\]]*)\]\]>",
                rss, re.IGNORECASE
            )
            for t in titles[:10]:
                projects.append({
                    "project_name": t.strip(),
                    "project_type": _classify_facility_type(t),
                    "company_name": "",
                    "prefecture":   "",
                    "announce_date": datetime.now().strftime("%Y-%m-%d"),
                    "area_sqm":     0.0,
                    "source":       "env_rss",
                })
        return projects

    try:
        from bs4 import BeautifulSoup  # type: ignore
        soup  = BeautifulSoup(html, "html.parser")

        # 環境アセス対象事業テーブルを解析
        tables = soup.find_all("table")
        for table in tables:
            rows = table.find_all("tr")
            for row in rows[1:]:   # ヘッダー行をスキップ
                cols = row.find_all(["td", "th"])
                if len(cols) < 3:
                    continue
                texts = [c.get_text(strip=True) for c in cols]
                project_name = texts[0] if len(texts) > 0 else ""
                project_type = texts[1] if len(texts) > 1 else ""
                company_name = texts[2] if len(texts) > 2 else ""
                prefecture   = texts[3] if len(texts) > 3 else ""

                if not project_name:
                    continue

                # 面積情報を project_name / 付近のテキストから抽出
                area_sqm = _extract_area_sqm(
                    " ".join(texts)
                )

                projects.append({
                    "project_name": project_name,
                    "project_type": project_type or _classify_facility_type(project_name),
                    "company_name": company_name,
                    "prefecture":   prefecture,
                    "announce_date": datetime.now().strftime("%Y-%m-%d"),
                    "area_sqm":     area_sqm,
                    "source":       "env_assessment",
                })

    except ImportError:
        # BeautifulSoup 未インストール時は正規表現でフォールバック
        # テーブルセルの内容を抽出
        cells = re.findall(r"<td[^>]*>([^<]{3,100})</td>", html)
        for i in range(0, len(cells) - 2, 4):
            name = cells[i].strip()
            if name and any(kw in name for kw in
                            ["工場", "施設", "発電", "物流", "センター"]):
                projects.append({
                    "project_name": name,
                    "project_type": _classify_facility_type(name),
                    "company_name": cells[i + 2].strip() if i + 2 < len(cells) else "",
                    "prefecture":   "",
                    "announce_date": datetime.now().strftime("%Y-%m-%d"),
                    "area_sqm":     0.0,
                    "source":       "env_assessment_regex",
                })
    except Exception as e:
        print(f"    ⚠️  [facility] 環境アセス パース失敗: {str(e)[:60]}")

    return projects


# ==============================================================
# BLOCK 4: 経産省 工場立地動向 スクレイパー
# ==============================================================

def _scrape_meti_factory() -> list[dict]:
    """
    経産省の「工場立地動向調査」結果ページから
    大規模工場立地情報を取得する。

    対象URL: https://www.meti.go.jp/statistics/

    工場立地動向調査は半期ごとの公表のため、
    最新の報道発表ページをスクレイピングする。

    Returns:
        [{"project_name": str, "company_name": str, "prefecture": str,
          "area_sqm": float, "industry": str, "source": "meti_factory"}, ...]
    """
    projects: list[dict] = []

    # 経産省 工場立地調査 専用ページ
    target_url = (
        "https://www.meti.go.jp/statistics/tyo/kojo/result-2.html"
    )
    html = _safe_get(target_url)

    if not html:
        # フォールバック: 経産省報道発表RSS
        rss_url = "https://www.meti.go.jp/press/rss.xml"
        rss = _safe_get(rss_url)
        if rss:
            titles = re.findall(
                r"<title><!\[CDATA\[([^\]]*(?:工場|立地|半導体|データセンター)[^\]]*)\]\]>",
                rss, re.IGNORECASE
            )
            for t in titles[:10]:
                projects.append({
                    "project_name": t.strip(),
                    "company_name": "",
                    "prefecture":   "",
                    "area_sqm":     0.0,
                    "industry":     _classify_facility_type(t),
                    "source":       "meti_rss",
                })
        return projects

    try:
        from bs4 import BeautifulSoup  # type: ignore
        soup = BeautifulSoup(html, "html.parser")

        # 工場立地動向の統計テーブルを解析
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            for row in rows[1:]:
                cols  = row.find_all(["td", "th"])
                texts = [c.get_text(strip=True) for c in cols]
                if len(texts) < 2:
                    continue

                # 業種・件数・面積の列を特定
                for i, t in enumerate(texts):
                    if any(kw in t for kw in
                           ["製造", "情報", "半導体", "食品", "化学", "機械"]):
                        # 面積情報を隣接列から取得
                        area_text = texts[i + 1] if i + 1 < len(texts) else ""
                        area_sqm  = _extract_area_sqm(area_text)
                        projects.append({
                            "project_name": f"{t}工場（統計集計）",
                            "company_name": "",
                            "prefecture":   texts[i - 1] if i > 0 else "",
                            "area_sqm":     area_sqm,
                            "industry":     t,
                            "source":       "meti_factory",
                        })
                        break

    except ImportError:
        # BeautifulSoup 未インストール時
        industry_patterns = re.findall(
            r"(半導体|自動車|データセンター|物流|食品|化学|医薬品)[^\d]*"
            r"(\d+(?:\.\d+)?)\s*(?:ha|㎡|万㎡)",
            html
        )
        for industry, area_str in industry_patterns:
            try:
                area_sqm = float(area_str)
                # ha → ㎡ 変換（ha の場合は10000倍）
                if "ha" in html[html.find(area_str) - 50: html.find(area_str) + 10]:
                    area_sqm *= 10000
                projects.append({
                    "project_name": f"{industry}関連施設",
                    "company_name": "",
                    "prefecture":   "",
                    "area_sqm":     area_sqm,
                    "industry":     industry,
                    "source":       "meti_factory_regex",
                })
            except ValueError:
                continue
    except Exception as e:
        print(f"    ⚠️  [facility] 経産省工場立地 パース失敗: {str(e)[:60]}")

    return projects


# ==============================================================
# BLOCK 5: ユーティリティ関数
# ==============================================================

def _extract_area_sqm(text: str) -> float:
    """
    テキストから面積（㎡）を抽出する。
    ha 表記は ×10000 して ㎡ に変換する。

    Args:
        text: 面積情報を含む文字列

    Returns:
        float: 面積（㎡）。抽出失敗時は 0.0
    """
    # 万㎡パターン（例: 5万㎡ = 50000㎡）
    m = re.search(r"(\d+(?:\.\d+)?)\s*万\s*(?:㎡|平方メートル|m2)", text)
    if m:
        try:
            return float(m.group(1)) * 10000
        except ValueError:
            pass

    # ha パターン（例: 15ha = 150000㎡）
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:ha|ヘクタール)", text)
    if m:
        try:
            return float(m.group(1)) * 10000
        except ValueError:
            pass

    # ㎡ パターン（例: 50000㎡）
    m = re.search(r"(\d{4,})\s*(?:㎡|平方メートル|m2|㎡)", text)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass

    return 0.0


def _classify_facility_type(text: str) -> str:
    """
    テキストから施設種別を判定する。

    Returns:
        str: FACILITY_SECTOR_MAP のキーのいずれか or "その他"
    """
    type_patterns = [
        (r"半導体|ウエハ|チップ|TSMC|ラピダス",        "半導体"),
        (r"データセンター|DC|サーバ|クラウド拠点",      "データセンター"),
        (r"研究所|R&D|ラボ|研究開発|創薬",              "研究所"),
        (r"物流|倉庫|配送|センター",                    "物流"),
        (r"工場|製造|生産|プラント",                    "工場"),
    ]
    for pattern, ftype in type_patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return ftype
    return "その他"


# ==============================================================
# BLOCK 6: プロジェクト分類・シグナル判定
# ==============================================================

def _classify_projects(
    projects:   list[dict],
    cfg,
) -> tuple[list[dict], list[dict]]:
    """
    取得したプロジェクトを大規模案件に絞り込み、
    FACILITY_SECTOR_MAP で業種コードを付与する。

    絞り込み条件:
      area_sqm >= FACILITY_MIN_AREA_SQM（10,000㎡）
      かつ project_type が FACILITY_SECTOR_MAP のいずれかのキーに合致

    Args:
        projects: _scrape_env_assessment() + _scrape_meti_factory() の結果
        cfg:      config モジュール

    Returns:
        (large_projects, signal_projects)
        large_projects:  面積条件を満たす全案件
        signal_projects: シグナル発火対象（業種マッチあり）
    """
    min_area    = float(getattr(cfg, "FACILITY_MIN_AREA_SQM", 10000.0))
    sector_map  = getattr(cfg, "FACILITY_SECTOR_MAP", {})
    ttl_days    = int(getattr(cfg, "FACILITY_EXPANSION_TTL_DAYS", 90))
    cutoff_date = datetime.now() - timedelta(days=ttl_days)

    large_projects:  list[dict] = []
    signal_projects: list[dict] = []

    for p in projects:
        area_sqm = float(p.get("area_sqm", 0.0))
        # 面積条件（0.0 = 面積情報なし → 閾値チェックをスキップし種別で判断）
        if area_sqm > 0 and area_sqm < min_area:
            continue

        # 有効期間チェック
        announce_str = p.get("announce_date", "")
        if announce_str:
            try:
                announce_dt = datetime.fromisoformat(announce_str)
                if announce_dt < cutoff_date:
                    continue
            except ValueError:
                pass

        large_projects.append(p)

        # 施設種別と FACILITY_SECTOR_MAP のマッチング
        ftype   = p.get("project_type", "") or _classify_facility_type(
            p.get("project_name", "")
        )
        sectors = sector_map.get(ftype, [])

        if sectors:
            signal_p = {**p, "matched_type": ftype, "sector_codes": sectors}
            signal_projects.append(signal_p)

    return large_projects, signal_projects


def _build_sector_boosts(
    signal_projects: list[dict],
    cfg,
) -> dict[str, float]:
    """
    シグナル発火プロジェクトから sector_boost を計算する。

    $boost = project\\_count \times FACILITY\\_EXPANSION\\_BONUS$
    業種ごとにプロジェクト数を集計して加算する。
    上限: SCORE_MAX_INSTITUTION_ADJ（共用）

    Args:
        signal_projects: _classify_projects() の signal_projects
        cfg:             config モジュール

    Returns:
        {sector_code: boost_pt}
    """
    bonus_per_project = float(getattr(cfg, "FACILITY_EXPANSION_BONUS",   6.0))
    max_adj           = float(getattr(cfg, "SCORE_MAX_INSTITUTION_ADJ",  15.0))

    # 業種別プロジェクト数を集計
    sector_count: dict[str, int] = {}
    for p in signal_projects:
        for sc in p.get("sector_codes", []):
            sector_count[sc] = sector_count.get(sc, 0) + 1

    boosts: dict[str, float] = {}
    for sc, count in sector_count.items():
        adj = round(count * bonus_per_project, 2)
        boosts[sc] = round(min(max_adj, adj), 2)

    return boosts


# ==============================================================
# BLOCK 7: メインエントリポイント
# ==============================================================

def run_facility_expansion_monitor(cfg, results: dict) -> dict:
    """
    施設着工モニターのメイン関数。
    stock_analyzer_main.py の週次バッチから呼ばれる。

    処理フロー:
      1. キャッシュ確認（TTL 180日以内はキャッシュを返す）
      2. 環境省・経産省から着工情報を取得
      3. 大規模案件（面積基準）に絞り込み
      4. FACILITY_SECTOR_MAP で業種コードを付与
      5. sector_boost を生成してキャッシュ保存

    Args:
        cfg:     config モジュール
        results: stock_analyzer_main.results（現時点では未使用、将来拡張用）

    Returns:
        {
          "sector_boosts":     {sector_code: float},
          "signal_projects":   [{...}],   # シグナル発火案件
          "large_projects":    [{...}],   # 大規模案件全件
          "total_fetched":     int,
          "total_large":       int,
          "total_signals":     int,
          "cached":            bool,
          "timestamp":         str,
          "skipped":           bool,
        }
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    if not getattr(cfg, "ENABLE_FACILITY_EXPANSION", True):
        print("  ⏩ 施設着工モニター: スキップ（ENABLE_FACILITY_EXPANSION=False）")
        return {"skipped": True, "sector_boosts": {}, "signal_projects": []}

    print("\n  🏭 [facility] 施設着工モニター 開始...")
    t0 = datetime.now()

    # ── キャッシュ確認 ────────────────────────────────────────
    cache = _load_cache(cfg)
    if cache:
        print(
            f"    📦 [facility] キャッシュ利用 "
            f"(シグナル={len(cache.get('signal_projects', []))}件): "
            f"TTL内のためスキップ"
        )
        return {**cache, "cached": True}

    sources_cfg = getattr(cfg, "FACILITY_DATA_SOURCES", {})
    all_projects: list[dict] = []

    # ── 環境省 環境影響評価DB ─────────────────────────────────
    env_cfg = sources_cfg.get("env_assessment_jp", {})
    if env_cfg.get("enabled", True):
        try:
            print("    🔍 [facility] 環境省 環境アセスDB 取得中...")
            env_projects = _scrape_env_assessment()
            all_projects.extend(env_projects)
            print(f"    ✅ [facility] 環境アセス: {len(env_projects)}件取得")
        except Exception as e:
            print(f"    ⚠️  [facility] 環境アセス取得エラー（スキップ）: "
                  f"{str(e)[:60]}")
        time.sleep(_REQUEST_WAIT)

    # ── 経産省 工場立地動向 ───────────────────────────────────
    meti_cfg = sources_cfg.get("meti_factory", {})
    if meti_cfg.get("enabled", True):
        try:
            print("    🔍 [facility] 経産省 工場立地動向 取得中...")
            meti_projects = _scrape_meti_factory()
            all_projects.extend(meti_projects)
            print(f"    ✅ [facility] 経産省工場立地: {len(meti_projects)}件取得")
        except Exception as e:
            print(f"    ⚠️  [facility] 経産省工場立地取得エラー（スキップ）: "
                  f"{str(e)[:60]}")

    print(f"    📊 [facility] 総取得: {len(all_projects)}件")

    if not all_projects:
        print("    ⚠️  [facility] 取得データなし → キャッシュ保存してスキップ")
        empty_result = {
            "sector_boosts":  {},
            "signal_projects": [],
            "large_projects":  [],
            "total_fetched":   0,
            "total_large":     0,
            "total_signals":   0,
            "cached":          False,
            "timestamp":       datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "skipped":         False,
        }
        _save_cache(cfg, empty_result)
        return empty_result

    # ── プロジェクト分類・シグナル判定 ───────────────────────
    large_projects, signal_projects = _classify_projects(all_projects, cfg)

    print(f"    🏗️  [facility] 大規模案件: {len(large_projects)}件")
    print(f"    🚨 [facility] シグナル発火: {len(signal_projects)}件")

    for sp in signal_projects:
        print(
            f"      ✦ {sp.get('project_name', '?')[:30]} "
            f"({sp.get('matched_type', '?')}) "
            f"→ 業種{sp.get('sector_codes', [])}"
        )

    # ── sector_boost 計算 ─────────────────────────────────────
    sector_boosts = _build_sector_boosts(signal_projects, cfg)

    if sector_boosts:
        print(f"    📈 [facility] sector_boost: {sector_boosts}")
    else:
        print("    ✅ [facility] sector_boost なし")

    # ── TTL 管理（FACILITY_EXPANSION_TTL_DAYS 超過分を除去）──
    ttl_days    = int(getattr(cfg, "FACILITY_EXPANSION_TTL_DAYS", 90))
    cutoff_date = (datetime.now() - timedelta(days=ttl_days)).strftime("%Y-%m-%d")
    active_signals = [
        sp for sp in signal_projects
        if sp.get("announce_date", "9999-99-99") >= cutoff_date
    ]

    # ── キャッシュ保存 ────────────────────────────────────────
    new_cache = {
        "sector_boosts":   sector_boosts,
        "signal_projects": signal_projects,
        "large_projects":  large_projects[:50],   # 上位50件のみ保存
        "active_signals":  active_signals,
        "total_fetched":   len(all_projects),
        "total_large":     len(large_projects),
        "total_signals":   len(signal_projects),
    }
    _save_cache(cfg, new_cache)

    # 結果はローカルキャッシュだけに残す（BQ の格納先テーブルは持たない）

    elapsed = (datetime.now() - t0).seconds
    print(
        f"\n  ✅ [facility] 完了 ({elapsed}秒): "
        f"取得{len(all_projects)}件 / "
        f"大規模{len(large_projects)}件 / "
        f"シグナル{len(signal_projects)}件 / "
        f"sector_boost={sector_boosts}"
    )

    return {
        "sector_boosts":   sector_boosts,
        "signal_projects": signal_projects,
        "large_projects":  large_projects,
        "total_fetched":   len(all_projects),
        "total_large":     len(large_projects),
        "total_signals":   len(signal_projects),
        "cached":          False,
        "timestamp":       datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "skipped":         False,
    }


# ==============================================================
# BLOCK 8: 単体テスト
# ==============================================================

if __name__ == "__main__":
    import importlib.util

    cfg = None
    for p in ["/content/config.py", "config.py"]:
        if Path(p).exists():
            spec = importlib.util.spec_from_file_location("config", p)
            if spec and spec.loader:
                cfg = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(cfg)
                break

    if cfg is None:
        print("ERROR: config.py が見つかりません")
        raise SystemExit(1)

    print("=== facility_expansion_monitor 単体テスト ===\n")

    # ── 面積抽出検証 ──────────────────────────────────────────
    assert _extract_area_sqm("5万㎡の工場建設")       == 50000.0
    assert _extract_area_sqm("15ha の用地")           == 150000.0
    assert _extract_area_sqm("延床面積25000㎡")       == 25000.0
    assert _extract_area_sqm("詳細不明")               == 0.0
    print("  ✅ 面積抽出 PASS")

    # ── 施設種別分類検証 ──────────────────────────────────────
    assert _classify_facility_type("半導体工場建設")       == "半導体"
    assert _classify_facility_type("大型物流センター着工") == "物流"
    assert _classify_facility_type("データセンター新設")   == "データセンター"
    assert _classify_facility_type("創薬研究所開設")       == "研究所"
    assert _classify_facility_type("自動車工場増設")       == "工場"
    print("  ✅ 施設種別分類 PASS")

    # ── プロジェクト分類・シグナル判定検証 ───────────────────
    mock_projects = [
        {
            "project_name": "半導体前工程棟新設",
            "project_type": "半導体",
            "company_name": "A社",
            "prefecture":   "熊本県",
            "announce_date": datetime.now().strftime("%Y-%m-%d"),
            "area_sqm":     80000.0,
            "source":       "env_assessment",
        },
        {
            "project_name": "大型物流センター",
            "project_type": "物流",
            "company_name": "B社",
            "prefecture":   "埼玉県",
            "announce_date": datetime.now().strftime("%Y-%m-%d"),
            "area_sqm":     15000.0,
            "source":       "env_assessment",
        },
        {
            "project_name": "小規模事務所増築",
            "project_type": "その他",
            "company_name": "C社",
            "prefecture":   "東京都",
            "announce_date": datetime.now().strftime("%Y-%m-%d"),
            "area_sqm":     500.0,   # 閾値未満
            "source":       "env_assessment",
        },
    ]

    large, signals = _classify_projects(mock_projects, cfg)
    print(f"\n  大規模案件: {len(large)}件")
    print(f"  シグナル案件: {len(signals)}件")
    for sp in signals:
        print(f"    {sp['project_name']} → {sp['sector_codes']}")

    # 半導体と物流は大規模、小規模事務所は除外
    assert len(large)   == 2, f"large={len(large)} ≠ 2"
    assert len(signals) == 2, f"signals={len(signals)} ≠ 2"

    # sector_boost 検証
    boosts = _build_sector_boosts(signals, cfg)
    print(f"\n  sector_boost: {boosts}")
    # 半導体 → ["1016","1018"]、物流 → ["1024","1021"]
    assert "1016" in boosts, "1016 (電気機器) が boosts に未登録"
    assert "1024" in boosts, "1024 (倉庫) が boosts に未登録"

    print("\n  ✅ 全テスト PASS")
