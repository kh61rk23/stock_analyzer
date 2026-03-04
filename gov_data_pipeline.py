# ==============================================================
# gov_data_pipeline.py  v3.0  (e-Stat API 動的ID検索版)
# ==============================================================
# 【v2.0 → v3.0 変更点】
#   統計表IDをハードコードせず、getStatsList APIで動的に検索する。
#   IDが変わっても壊れない設計。キャッシュで毎回検索を回避。
#
# 【e-Stat APIの2種類のID問題】
#   ファイル型ID: Excel/CSVダウンロード用。getStatsDataでは使えない。
#   DB型ID:      getStatsDataで直接クエリ可能。今回はこちらを使う。
#   → getStatsListで「DB型のみ」を検索してIDを自動取得する。
#
# 【config.py で必要な設定】
#   ESTAT_API_KEY : str = ""  # e-Stat アプリケーションID
# ==============================================================

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Optional


ESTAT_LIST_URL = "https://api.e-stat.go.jp/rest/3.0/app/json/getStatsList"
ESTAT_DATA_URL = "https://api.e-stat.go.jp/rest/3.0/app/json/getStatsData"


# ==============================================================
# BLOCK 0: 統計定義（statsDataIdは空欄→動的検索で補完）
# ==============================================================
# searchWord: getStatsListで検索するキーワード
# statsCode : 政府統計コード（8桁）で絞り込む
#   00200521 = 消費者物価指数（総務省）
#   00550300 = 鉱工業指数（経産省）
#   00450071 = 毎月勤労統計（厚労省）
#   00200561 = 家計調査（総務省）
#   00200531 = 労働力調査（総務省）
#   00600220 = 建設着工統計（国交省）
#   00100409 = 機械受注（内閣府）
#   00350300 = 貿易統計（財務省）

STAT_DEFINITIONS = [
    {
        "id":         "cpi",
        "name":       "消費者物価指数（CPI）",
        "statsCode":  "00200521",
        "searchWord": "消費者物価指数 月次 総合",
        "unit":       "（2020年=100）",
        "direction":  "higher_is_bad",
        "sectors":    ["1027", "1033", "1019", "1004"],
        "description":"物価上昇は購買力を下げ、小売・外食・生活用品に逆風",
    },
    {
        "id":         "industrial_production",
        "name":       "鉱工業生産指数",
        "statsCode":  "00550300",
        "searchWord": "鉱工業指数 生産指数 月次",
        "unit":       "（2015年=100）",
        "direction":  "higher_is_good",
        "sectors":    ["1015", "1016", "1007", "1012"],
        "description":"生産指数の上昇は製造業全般の追い風",
    },
    {
        "id":         "wages",
        "name":       "毎月勤労統計（現金給与総額）",
        "statsCode":  "00450071",
        "searchWord": "毎月勤労統計 現金給与総額 月次",
        "unit":       "（円）",
        "direction":  "higher_is_good",
        "sectors":    ["1027", "1033", "1004", "1019"],
        "description":"賃金上昇は消費拡大につながり、小売・外食・サービス業に追い風",
    },
    {
        "id":         "household_spending",
        "name":       "家計調査（消費支出）",
        "statsCode":  "00200561",
        "searchWord": "家計調査 消費支出 月次 二人以上",
        "unit":       "（円）",
        "direction":  "higher_is_good",
        "sectors":    ["1027", "1033", "1004", "1026"],
        "description":"消費支出の増加は内需型業種全般の追い風",
    },
    {
        "id":         "unemployment",
        "name":       "労働力調査（完全失業率）",
        "statsCode":  "00200531",
        "searchWord": "労働力調査 完全失業率 月次",
        "unit":       "（%）",
        "direction":  "higher_is_bad",
        "sectors":    ["1027", "1033", "1028", "1032"],
        "description":"失業率上昇は景気悪化を示し、内需・金融・不動産に逆風",
    },
    {
        "id":         "construction_starts",
        "name":       "建設工事着工統計",
        "statsCode":  "00600220",
        "searchWord": "建設工事着工 新設住宅 月次",
        "unit":       "（戸）",
        "direction":  "higher_is_good",
        "sectors":    ["1003", "1032", "1012", "1014"],
        "description":"着工件数の増加は建設・不動産・鉄鋼の追い風",
    },
    {
        "id":         "machinery_orders",
        "name":       "機械受注統計（民需）",
        "statsCode":  "00100409",
        "searchWord": "機械受注 民需 月次",
        "unit":       "（億円）",
        "direction":  "higher_is_good",
        "sectors":    ["1015", "1016", "1018"],
        "description":"機械受注の増加は設備投資拡大を示し、機械・電気機器に先行追い風",
    },
    {
        "id":         "trade_exports",
        "name":       "貿易統計（輸出総額）",
        "statsCode":  "00350300",
        "searchWord": "貿易統計 輸出 月次",
        "unit":       "（百万円）",
        "direction":  "higher_is_good",
        "sectors":    ["1017", "1015", "1007", "1016"],
        "description":"輸出増加は外需型製造業（自動車・機械・化学）の追い風",
    },
]

SECTOR_NAMES = {
    "1003": "建設業",     "1004": "食料品",
    "1007": "化学",       "1008": "医薬品",
    "1012": "鉄鋼",       "1014": "金属製品",
    "1015": "機械",       "1016": "電気機器",
    "1017": "輸送用機器", "1018": "精密機器",
    "1019": "その他製品", "1020": "電気・ガス業",
    "1021": "陸運業",     "1026": "卸売業",
    "1027": "小売業",     "1028": "銀行業",
    "1032": "不動産業",   "1033": "サービス業",
}


# ==============================================================
# BLOCK 1: 環境セットアップ
# ==============================================================

def setup_dirs(cfg) -> dict:
    """実行環境に応じてディレクトリを準備する（Colab/GitHub Actions両対応）"""
    # GitHub Actions では Colab のマウントは不要（スキップが正常動作）
    try:
        from google.colab import drive
        drive.mount("/content/drive")
        print("  ✅ Google Drive マウント完了")
    except Exception:
        pass  # GitHub Actions では正常スキップ。ログ出力しない

    insight_dir = Path(cfg.INSIGHT_DIR) / "gov"
    data_dir    = Path(cfg.DATA_DIR) / "gov_cache"

    for d in [insight_dir, data_dir]:
        try:
            d.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            pass

    return {"insight_dir": insight_dir, "data_dir": data_dir}


# ==============================================================
# BLOCK 2: statsDataId 動的検索
# ==============================================================

def search_statsdata_id(
    app_id:      str,
    stats_code:  str,
    search_word: str,
    data_dir:    Optional[Path] = None,
) -> Optional[str]:
    """
    getStatsList API でDB型の統計表IDを検索して返す。
    同一月内はキャッシュを使い、API呼び出しを節約する。

    Returns:
        statsDataId (str) or None
    """
    try:
        import requests
    except ImportError:
        return None

    # キャッシュ確認（月次）
    if data_dir:
        month  = datetime.now().strftime("%Y-%m")
        key    = stats_code.replace("/", "_")
        cache  = data_dir / f"id_cache_{key}_{month}.txt"
        if cache.exists():
            cached = cache.read_text().strip()
            if cached:
                return cached

    params = {
        "appId":             app_id,
        "lang":              "J",
        "statsCode":         stats_code,
        "searchWord":        search_word,
        "searchKind":        "1",   # 1=統計情報（DB型含む）
        "explanationGetFlg": "N",
    }

    try:
        resp = requests.get(ESTAT_LIST_URL, params=params, timeout=20)
        if resp.status_code != 200:
            return None

        raw    = resp.json()
        status = raw.get("GET_STATS_LIST", {}).get("RESULT", {}).get("STATUS", -1)
        if status != 0:
            return None

        tables = (raw.get("GET_STATS_LIST", {})
                     .get("DATALIST_INF", {})
                     .get("TABLE_INF", []))
        if not tables:
            return None

        # TABLE_INFは単一dictの場合もリストの場合もある
        if isinstance(tables, dict):
            tables = [tables]

        # DB型（STATISTICS_NAME に「DB」を含むか、または最初のものを採用）
        db_tables = [t for t in tables
                     if "DB" in str(t.get("STATISTICS_NAME", "")).upper()
                     or "データベース" in str(t.get("STATISTICS_NAME", ""))]

        chosen = db_tables[0] if db_tables else tables[0]
        found_id = chosen.get("@id", "")

        if found_id and data_dir:
            try:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(found_id)
            except Exception:
                pass

        return found_id if found_id else None

    except Exception as e:
        print(f"    ⚠️  ID検索例外 ({stats_code}): {str(e)[:50]}")
        return None


# ==============================================================
# BLOCK 3: 統計データ取得
# ==============================================================

def _parse_time_code(time_code: str) -> str:
    if len(time_code) >= 6:
        year  = time_code[:4]
        month = time_code[4:6]
        return f"{year}-{month}" if month != "00" else year
    return time_code


def fetch_estat_data(
    stats_data_id: str,
    app_id:        str,
    data_dir:      Optional[Path] = None,
    limit:         int = 14,
) -> Optional[list]:
    """
    getStatsData API からデータを取得する。

    Returns:
        [{"time": "2024-01", "value": float, "time_code": str}, ...]
    """
    try:
        import requests
    except ImportError:
        return None

    # キャッシュ（月次）
    if data_dir:
        month = datetime.now().strftime("%Y-%m")
        cache = data_dir / f"data_{stats_data_id}_{month}.json"
        if cache.exists():
            try:
                return json.loads(cache.read_text(encoding="utf-8"))
            except Exception:
                pass

    params = {
        "appId":             app_id,
        "lang":              "J",
        "statsDataId":       stats_data_id,
        "metaGetFlg":        "N",
        "cntGetFlg":         "N",
        "explanationGetFlg": "N",
        "annotationGetFlg":  "N",
        "replaceSpChars":    "0",
        "limit":             str(limit * 3),
    }

    try:
        resp = requests.get(ESTAT_DATA_URL, params=params, timeout=30)
        if resp.status_code != 200:
            print(f"    ⚠️  HTTP {resp.status_code} (ID={stats_data_id})")
            return None

        raw    = resp.json()
        status = (raw.get("GET_STATS_DATA", {})
                     .get("RESULT", {}).get("STATUS", -1))
        if status != 0:
            msg = (raw.get("GET_STATS_DATA", {})
                      .get("RESULT", {}).get("ERROR_MSG", ""))
            print(f"    ⚠️  e-Stat エラー: {msg[:80]}")
            return None

        values = (raw["GET_STATS_DATA"]["STATISTICAL_DATA"]
                     .get("DATA_INF", {}).get("VALUE", []))
        if not values:
            return None

        records = []
        for v in values:
            time_code = v.get("@time", "")
            value_str = v.get("$", "")
            if not value_str or value_str in ["-", "***", "…", "x", "X", ""]:
                continue
            try:
                value = float(value_str.replace(",", ""))
                records.append({
                    "time":      _parse_time_code(time_code),
                    "value":     value,
                    "time_code": time_code,
                })
            except (ValueError, TypeError):
                continue

        records.sort(key=lambda x: x.get("time_code", ""), reverse=True)
        records = records[:limit]

        if records and data_dir:
            try:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(
                    json.dumps(records, ensure_ascii=False, indent=2),
                    encoding="utf-8"
                )
            except Exception:
                pass

        return records if records else None

    except Exception as e:
        print(f"    ⚠️  データ取得例外 (ID={stats_data_id}): {str(e)[:60]}")
        return None


# ==============================================================
# BLOCK 4: 変化率計算・業種影響判定
# ==============================================================

def calculate_changes(records: list) -> dict:
    if not records:
        return {}
    latest = records[0]
    result = {
        "latest_value": latest["value"],
        "latest_time":  latest["time"],
        "mom_change":   None,
        "yoy_change":   None,
    }
    if len(records) >= 2:
        prev = records[1]["value"]
        if prev and prev != 0:
            result["mom_change"] = round(
                (latest["value"] - prev) / abs(prev) * 100, 2)
    if len(records) >= 13:
        yr = records[12]["value"]
        if yr and yr != 0:
            result["yoy_change"] = round(
                (latest["value"] - yr) / abs(yr) * 100, 2)
    return result


def judge_sector_impact(stat_def: dict, changes: dict) -> dict:
    if not changes:
        return {"signal": "neutral", "score": 0, "reason": "データ取得失敗"}

    direction = stat_def.get("direction", "higher_is_good")
    change    = changes.get("yoy_change")
    if change is None:
        change = changes.get("mom_change")
    if change is None:
        return {"signal": "neutral", "score": 0, "reason": "変化率計算不可"}

    th = 0.5
    if direction == "higher_is_good":
        if change >= th:
            return {"signal": "tailwind", "score": 1,
                    "reason": f"前年比+{change:.1f}%（上昇＝追い風）"}
        elif change <= -th:
            return {"signal": "headwind", "score": -1,
                    "reason": f"前年比{change:.1f}%（下落＝逆風）"}
    else:
        if change >= th:
            return {"signal": "headwind", "score": -1,
                    "reason": f"前年比+{change:.1f}%（上昇＝逆風）"}
        elif change <= -th:
            return {"signal": "tailwind", "score": 1,
                    "reason": f"前年比{change:.1f}%（下落＝追い風）"}

    return {"signal": "neutral", "score": 0,
            "reason": f"前年比{change:+.1f}%（横ばい）"}


# ==============================================================
# BLOCK 5: 業種別シグナル集計
# ==============================================================

def aggregate_sector_signals(stat_results: list) -> dict:
    sector_signals = {}
    for result in stat_results:
        impact = result.get("impact", {})
        stat   = result.get("stat", {})
        signal = impact.get("signal", "neutral")
        score  = impact.get("score", 0)
        reason = impact.get("reason", "")
        for code in stat.get("sectors", []):
            if code not in sector_signals:
                sector_signals[code] = {
                    "score": 0, "tailwinds": [], "headwinds": []
                }
            sector_signals[code]["score"] += score
            if signal == "tailwind":
                sector_signals[code]["tailwinds"].append(
                    f"{stat['name']}: {reason}")
            elif signal == "headwind":
                sector_signals[code]["headwinds"].append(
                    f"{stat['name']}: {reason}")
    return sector_signals


# ==============================================================
# BLOCK 6: Markdown レポート生成
# ==============================================================

def build_report(stat_results: list, sector_signals: dict) -> str:
    now   = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "# 政府統計パイプライン レポート",
        f"**生成日時**: {now}", "",
        "---", "",
        "## 📊 取得統計サマリー", "",
        "| 統計名 | 最新値 | 前月比 | 前年比 | シグナル |",
        "|--------|--------|--------|--------|---------|",
    ]
    for r in stat_results:
        stat    = r["stat"]
        changes = r.get("changes", {})
        impact  = r.get("impact", {})
        latest  = changes.get("latest_value", "N/A")
        t       = changes.get("latest_time", "")
        mom     = changes.get("mom_change")
        yoy     = changes.get("yoy_change")
        signal  = impact.get("signal", "neutral")
        icon    = {"tailwind": "📈 追い風", "headwind": "📉 逆風",
                   "neutral":  "➡️ 中立"}.get(signal, "➡️ 中立")
        mom_str = f"{mom:+.1f}%" if mom is not None else "―"
        yoy_str = f"{yoy:+.1f}%" if yoy is not None else "―"
        val_str = f"{latest:,.1f}" if isinstance(latest, float) else str(latest)
        lines.append(
            f"| {stat['name']} | {val_str}{stat['unit']} ({t}) "
            f"| {mom_str} | {yoy_str} | {icon} |")
    lines += ["", "---", "", "## 🏭 業種別 政府統計シグナル", ""]
    for code, data in sorted(sector_signals.items(),
                              key=lambda x: x[1]["score"], reverse=True):
        name  = SECTOR_NAMES.get(code, code)
        score = data["score"]
        icon  = "📈" if score > 0 else ("📉" if score < 0 else "➡️")
        lines.append(f"### {icon} {name}（スコア: {score:+d}）")
        if data["tailwinds"]:
            lines.append("**追い風:**")
            for tw in data["tailwinds"]:
                lines.append(f"- {tw}")
        if data["headwinds"]:
            lines.append("**逆風:**")
            for hw in data["headwinds"]:
                lines.append(f"- {hw}")
        lines.append("")
    lines += ["---",
              f"*出典: 政府統計の総合窓口（e-Stat）/ 取得日: {now}*", ""]
    return "\n".join(lines)


# ==============================================================
# BLOCK 7: メインエントリポイント
# ==============================================================

def run_gov_pipeline(cfg) -> dict:
    """
    全政府統計を取得・分析してレポートを保存するメインエントリ。
    stock_analyzer_main.py の mod.main(cfg=cfg) から呼ばれる。
    """
    print(f"\n{'='*60}")
    print("  📊 政府統計パイプライン v3.0 実行開始")
    print(f"{'='*60}")

    app_id = getattr(cfg, "ESTAT_API_KEY", "")
    if not app_id:
        print("  ⏩ ESTAT_API_KEY 未設定（config.py に追加してください）")
        return {"stats_fetched": 0, "stats_total": 0, "skipped": True}

    dirs        = setup_dirs(cfg)
    insight_dir = dirs["insight_dir"]
    data_dir    = dirs["data_dir"]
    stat_results = []

    for stat_def in STAT_DEFINITIONS:
        print(f"\n  📥 {stat_def['name']} 取得中...")

        # Step1: statsDataIdを動的に検索
        stats_id = search_statsdata_id(
            app_id      = app_id,
            stats_code  = stat_def["statsCode"],
            search_word = stat_def["searchWord"],
            data_dir    = data_dir,
        )

        if not stats_id:
            print(f"    ⚠️  statsDataId が見つかりません（statsCode={stat_def['statsCode']}）")
            stat_results.append({
                "stat":    stat_def,
                "records": [],
                "changes": {},
                "impact":  {"signal": "neutral", "score": 0, "reason": "ID取得失敗"},
            })
            time.sleep(0.3)
            continue

        print(f"    🔍 statsDataId: {stats_id}")

        # Step2: データ取得
        records = fetch_estat_data(stats_id, app_id, data_dir)

        if records:
            changes = calculate_changes(records)
            impact  = judge_sector_impact(stat_def, changes)
            icon    = {"tailwind": "📈", "headwind": "📉",
                       "neutral":  "➡️"}.get(impact["signal"], "➡️")
            print(f"  {icon} {impact['reason']}")
            stat_results.append({
                "stat":    stat_def,
                "records": records,
                "changes": changes,
                "impact":  impact,
            })
        else:
            print("  ⚠️  データ取得失敗またはデータなし")
            stat_results.append({
                "stat":    stat_def,
                "records": [],
                "changes": {},
                "impact":  {"signal": "neutral", "score": 0,
                             "reason": "データ取得失敗"},
            })

        time.sleep(0.5)

    sector_signals = aggregate_sector_signals(stat_results)
    report_md      = build_report(stat_results, sector_signals)
    today_str      = datetime.now().strftime("%Y-%m-%d")
    report_path    = insight_dir / f"gov_report_{today_str}.md"

    try:
        report_path.write_text(report_md, encoding="utf-8")
        print(f"\n  ✅ レポート保存: {report_path}")
    except Exception as e:
        print(f"\n  ⚠️  レポート保存失敗: {e}")
        report_path = None

    fetched = sum(1 for r in stat_results if r.get("records"))
    total   = len(STAT_DEFINITIONS)
    print(f"  📊 取得完了: {fetched}/{total} 統計")

    return {
        "stat_results":   stat_results,
        "sector_signals": sector_signals,
        "report_path":    str(report_path) if report_path else "",
        "stats_fetched":  fetched,
        "stats_total":    total,
        "skipped":        False,
    }


def main(cfg=None):
    """stock_analyzer_main.py からの呼び出し互換用"""
    if cfg is None:
        import importlib.util
        for p in ["/content/config.py",
                  str(Path(__file__).parent / "config.py")]:
            if Path(p).exists():
                spec = importlib.util.spec_from_file_location("config", p)
                cfg  = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(cfg)
                break
    return run_gov_pipeline(cfg)


if __name__ == "__main__":
    import importlib.util
    for p in ["/content/config.py", str(Path(__file__).parent / "config.py")]:
        if Path(p).exists():
            spec = importlib.util.spec_from_file_location("config", p)
            cfg  = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cfg)
            break
    print("=== gov_data_pipeline v3.0 単体テスト ===")
    key = getattr(cfg, "ESTAT_API_KEY", "")
    print(f"e-Stat APIキー: {'あり' if key else 'なし（スキップ）'}")
    if key:
        result = run_gov_pipeline(cfg)
        print(f"\n取得統計数: {result['stats_fetched']}/{result['stats_total']}")
