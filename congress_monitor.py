# ==============================================================
# congress_monitor.py  v1.0  (Wave 4 - Policy Ph4)
# ==============================================================
# 【役割】
#   米国議会における重要法案の進行状況を追跡し、
#   日本の業種への影響スコアを計算する。
#
# 【データソース】
#   Congress.gov API (api.congress.gov) - 無料・APIキー必要
#   API Key 取得: https://api.congress.gov/sign-up/
#   APIキー未設定時: フォールバックとしてロイターRSS等から
#   キーワード検出のみを実施する。
#
# 【議会進行ステージ（CONGRESS_BILL_STAGES）】
#   introduced → committee → floor_vote → passed → signed
#   各ステージに CONGRESS_STAGE_MULTIPLIER で乗数を設定。
#
# 【スコア計算】
#   $impact\_score = base\_score \times stage\_multiplier$
#   $final\_adj = \min(impact\_score, SCORE\_MAX\_POLICY\_L4\_ADJ\_W4)$
#
# 【出力スキーマ（run_congress_monitor の返り値）】
#   {
#     "bills":          [...],  # 追跡中法案リスト
#     "sector_boosts":  {...},  # {sector_code: adj_pt}
#     "new_bills":      [...],  # 新規検出法案
#     "stage_changes":  [...],  # ステージ変化リスト
#     "total_tracked":  int,
#     "cached":         bool,
#     "timestamp":      str,
#   }
#
# 【キャッシュ】
#   CACHE_DIR/congress/congress_bill_cache.json
#   TTL: POLICY_STRUCTURAL_CACHE_TTL_MONTHS (12ヶ月)
#
# 【依存】
#   config.py (BLOCK 35: CONGRESS_BILL_STAGES /
#              CONGRESS_STAGE_MULTIPLIER /
#              POLICY_SECTOR_IMPACT_MAP /
#              SCORE_MAX_POLICY_L4_ADJ_W4)
# ==============================================================

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


# ==============================================================
# BLOCK 1: 定数フォールバック
# ==============================================================


from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'CM_DEFAULT_STAGE_MULTIPLIER': '_DEFAULT_STAGE_MULTIPLIER',
    'B4_CONGRESS_MONITOR_DEFAULT_CACHE_TTL_MONTHS': '_DEFAULT_CACHE_TTL_MONTHS',
    'B6_CONGRESS_MONITOR_DEFAULT_L4_MAX_ADJ': '_DEFAULT_L4_MAX_ADJ',
}

_DEFAULT_STAGE_MULTIPLIER = {
    "introduced":  0.20,
    "committee":   0.40,
    "floor_vote":  0.70,
    "passed":      0.90,
    "signed":      1.00,
}

_DEFAULT_CACHE_TTL_MONTHS = 12
_DEFAULT_L4_MAX_ADJ       = 25.0

# Congress.gov API エンドポイント
_CONGRESS_API_BASE = "https://api.congress.gov/v3"

# フォールバック用キーワード検索対象 RSS
_FALLBACK_RSS_URLS = [
    "https://www.reuters.com/news/rss/us",
    "https://feeds.bloomberg.com/politics/news.rss",
]


def _get_stage_multiplier(cfg) -> dict:
    return getattr(cfg, "CONGRESS_STAGE_MULTIPLIER", _DEFAULT_STAGE_MULTIPLIER)


def _get_impact_map(cfg) -> dict:
    return getattr(cfg, "POLICY_SECTOR_IMPACT_MAP", {})


def _get_l4_max(cfg) -> float:
    return float(getattr(cfg, "SCORE_MAX_POLICY_L4_ADJ_W4", _DEFAULT_L4_MAX_ADJ))


# ==============================================================
# BLOCK 2: キャッシュ管理
# ==============================================================

def _congress_cache_dir(cfg) -> Path:
    base = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache"))
    d = base / "congress"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_bill_cache(cfg) -> dict:
    """
    congress_bill_cache.json を読み込む。
    TTL: POLICY_STRUCTURAL_CACHE_TTL_MONTHS（12ヶ月）

    Returns:
        {
          "bills": {bill_id: bill_dict},
          "last_updated": str,
        }
    """
    p = _congress_cache_dir(cfg) / "congress_bill_cache.json"
    if not p.exists():
        return {"bills": {}, "last_updated": ""}
    try:
        data      = json.loads(p.read_text(encoding="utf-8"))
        ttl_months = int(getattr(cfg, "POLICY_STRUCTURAL_CACHE_TTL_MONTHS",
                                  _DEFAULT_CACHE_TTL_MONTHS))
        cutoff    = datetime.now() - timedelta(days=ttl_months * 30)
        # TTL切れ法案を除去（signed 済みで cutoff 以前のもの）
        bills = data.get("bills", {})
        cleaned = {
            bid: b for bid, b in bills.items()
            if not (b.get("stage") == "signed"
                    and _safe_parse_date(b.get("last_updated", ""))
                    < cutoff)
        }
        data["bills"] = cleaned
        return data
    except Exception as e:
        print(f"    ⚠️  [congress] キャッシュ読み込み失敗: {str(e)[:60]}")
        return {"bills": {}, "last_updated": ""}


def _save_bill_cache(cfg, data: dict) -> None:
    p = _congress_cache_dir(cfg) / "congress_bill_cache.json"
    try:
        data["last_updated"] = datetime.now().isoformat()
        p.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"    ⚠️  [congress] キャッシュ保存失敗: {str(e)[:60]}")


def _safe_parse_date(s: str) -> datetime:
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return datetime(2000, 1, 1)


# ==============================================================
# BLOCK 3: Congress.gov API 取得
# ==============================================================

def _fetch_recent_bills(
    api_key:  str,
    congress: int = 119,   # 119th Congress (2025-2027)
    limit:    int = 20,
    timeout:  int = 15,
) -> list[dict]:
    """
    Congress.gov API から最近更新された法案を取得する。

    Returns:
        [{"bill_id": str, "title": str, "latest_action": str,
          "stage": str, "sponsor": str, "introduced_date": str}, ...]
    """
    if not api_key:
        return []

    url = (
        f"{_CONGRESS_API_BASE}/bill/{congress}"
        f"?sort=updateDate+desc&limit={limit}"
        f"&api_key={api_key}"
    )
    try:
        import urllib.request
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "stock_analyzer/1.0",
                "Accept":     "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data  = json.loads(resp.read().decode("utf-8"))
        bills_raw = data.get("bills", [])
        results   = []
        for b in bills_raw:
            bill_id = (
                f"{b.get('type','')}{b.get('number','')}/"
                f"{b.get('congress','')}"
            )
            action_text = (
                b.get("latestAction", {}).get("text", "") or ""
            )
            stage = _infer_stage_from_action(action_text)
            results.append({
                "bill_id":         bill_id,
                "title":           (b.get("title") or "")[:120],
                "latest_action":   action_text[:200],
                "stage":           stage,
                "sponsor":         b.get("sponsors", [{}])[0].get("name", ""),
                "introduced_date": b.get("introducedDate", ""),
                "last_updated":    b.get("updateDate", ""),
                "url":             (
                    f"https://www.congress.gov/bill/{congress}th-congress"
                    f"/{(b.get('type','') or '').lower().replace(' ','-')}"
                    f"/{b.get('number','')}"
                ),
            })
        return results
    except Exception as e:
        print(f"    ⚠️  [congress] API取得失敗: {str(e)[:60]}")
        return []


def _infer_stage_from_action(action_text: str) -> str:
    """
    latest_action テキストから議会ステージを推定する。

    Returns:
        str: "introduced" | "committee" | "floor_vote" | "passed" | "signed"
    """
    t = action_text.lower()
    if any(kw in t for kw in ["became public law", "signed by president",
                                "enacted", "signed into law"]):
        return "signed"
    if any(kw in t for kw in ["passed house", "passed senate",
                                "agreed to in"]):
        return "passed"
    if any(kw in t for kw in ["floor", "vote", "cloture",
                                "roll call", "yeas"]):
        return "floor_vote"
    if any(kw in t for kw in ["committee", "referred to",
                                "subcommittee", "markup"]):
        return "committee"
    return "introduced"


# ==============================================================
# BLOCK 4: キーワードマッチによる法案種別判定
# ==============================================================

def _classify_bill_type(title: str, action: str, cfg) -> list[str]:
    """
    法案タイトル・アクションから POLICY_SECTOR_IMPACT_MAP のキーを特定する。

    Returns:
        [bill_type_key, ...]  マッチした法案種別キーのリスト
    """
    impact_map = _get_impact_map(cfg)
    text       = f"{title} {action}".lower()
    matched    = []

    # キーワードマッピング（法案種別 → 検索キーワード）
    _BILL_KEYWORDS: dict = {
        "semiconductor_subsidy": [
            "semiconductor", "chips", "fab", "foundry",
        ],
        "clean_energy": [
            "clean energy", "renewable", "solar", "wind",
            "green", "climate",
        ],
        "tariff_steel": [
            "tariff", "steel", "aluminum", "trade remedy",
        ],
        "pharma_pricing": [
            "drug price", "prescription", "medicare negotiat",
            "pharmaceutical price",
        ],
        "defense_spending": [
            "defense", "military", "ndaa", "pentagon",
            "armed forces",
        ],
        "financial_regulation": [
            "bank regulation", "financial stability",
            "dodd-frank", "capital requirement",
        ],
        "infrastructure": [
            "infrastructure", "highway", "bridge",
            "broadband", "water system",
        ],
    }

    for bill_type, keywords in _BILL_KEYWORDS.items():
        if bill_type not in impact_map:
            continue
        if any(kw in text for kw in keywords):
            matched.append(bill_type)

    return matched


# ==============================================================
# BLOCK 5: フォールバック RSS 検索
# ==============================================================

def _fetch_from_rss_fallback(cfg) -> list[dict]:
    """
    Congress.gov API が使えない場合のフォールバック。
    ロイター等の RSS から議会関連ニュースをキーワード検索する。

    Returns:
        [{"bill_id": str, "title": str, "stage": str, ...}]
    """
    items = []
    try:
        import feedparser
    except ImportError:
        return items

    impact_map = _get_impact_map(cfg)

    for url in _FALLBACK_RSS_URLS:
        try:
            parsed = feedparser.parse(url)
            for entry in parsed.entries[:20]:
                title   = getattr(entry, "title",   "") or ""
                summary = getattr(entry, "summary", "") or ""
                text    = f"{title} {summary}".lower()

                # 議会関連キーワード検出
                if not any(kw in text for kw in [
                    "congress", "senate", "house", "bill",
                    "legislation", "law", "vote",
                ]):
                    continue

                # 法案種別判定
                bill_types = _classify_bill_type(title, summary, cfg)
                if not bill_types:
                    continue

                # ステージ推定
                stage = _infer_stage_from_action(text)

                bill_id = f"RSS_{abs(hash(title))}"
                items.append({
                    "bill_id":         bill_id,
                    "title":           title[:120],
                    "latest_action":   summary[:200],
                    "stage":           stage,
                    "sponsor":         "",
                    "introduced_date": "",
                    "last_updated":    datetime.now().strftime("%Y-%m-%d"),
                    "url":             getattr(entry, "link", ""),
                    "bill_types":      bill_types,
                    "source":          "rss_fallback",
                })
            time.sleep(0.5)
        except Exception as e:
            print(f"    ⚠️  [congress] RSS取得失敗 ({url[:40]}): "
                  f"{str(e)[:40]}")

    return items


# ==============================================================
# BLOCK 6: ステージ変化検出・スコア計算
# ==============================================================

def _detect_stage_changes(
    new_bills:   list[dict],
    cached_bills: dict,
) -> list[dict]:
    """
    前回キャッシュとの比較でステージ変化を検出する。

    Returns:
        [{"bill_id": str, "title": str,
          "prev_stage": str, "new_stage": str,
          "bill_types": list}, ...]
    """
    changes   = []
    stage_order = {
        "introduced": 0, "committee": 1,
        "floor_vote": 2, "passed": 3, "signed": 4,
    }

    for bill in new_bills:
        bill_id = bill.get("bill_id", "")
        prev    = cached_bills.get(bill_id, {})
        prev_stage = prev.get("stage", "")
        new_stage  = bill.get("stage",  "introduced")

        if not prev_stage:
            continue   # 新規法案はステージ変化ではない

        # 進行方向の変化のみ検出（後退は無視）
        if (stage_order.get(new_stage, 0) >
                stage_order.get(prev_stage, 0)):
            changes.append({
                "bill_id":    bill_id,
                "title":      bill.get("title", "")[:80],
                "prev_stage": prev_stage,
                "new_stage":  new_stage,
                "bill_types": bill.get("bill_types", []),
                "url":        bill.get("url", ""),
            })

    return changes


def _calc_sector_boosts(
    bills:  list[dict],
    cfg,
) -> dict[str, float]:
    """
    全法案のスコアを業種コードに集約する。

    $impact\\_score = base\\_score \times stage\\_multiplier$
    $final\\_adj = \\min(impact\\_score, SCORE\\_MAX\\_POLICY\\_L4\\_ADJ\\_W4)$

    Returns:
        {sector_code: total_adj_pt}
    """
    impact_map       = _get_impact_map(cfg)
    stage_multiplier = _get_stage_multiplier(cfg)
    l4_max           = _get_l4_max(cfg)
    boosts: dict     = {}

    for bill in bills:
        stage      = bill.get("stage", "introduced")
        multiplier = stage_multiplier.get(stage, 0.20)
        bill_types = bill.get("bill_types", [])

        for bt in bill_types:
            impact = impact_map.get(bt, {})
            sectors    = impact.get("sectors", [])
            base_score = float(impact.get("base_score", 0.0))
            direction  = impact.get("direction", "tailwind")

            adj = base_score * multiplier
            if direction == "headwind":
                adj = -adj

            for sc in sectors:
                boosts[sc] = round(
                    max(-l4_max, min(l4_max,
                                    boosts.get(sc, 0.0) + adj)), 2
                )

    return boosts


# ==============================================================
# BLOCK 7: メインエントリポイント
# ==============================================================

def run_congress_monitor(cfg, results: Optional[dict] = None) -> dict:
    """
    米国議会法案モニタリングのメイン関数。
    stock_analyzer_main.py から呼ばれる。

    Args:
        cfg:     config モジュール
        results: stock_analyzer_main.results（None 可）

    Returns:
        run_congress_monitor の出力スキーマ参照
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    print("\n  🏛️  [congress_monitor] 議会法案追跡 開始...")
    t0 = datetime.now()

    # ── API キー確認 ──────────────────────────────────────────
    api_key = getattr(cfg, "CONGRESS_API_KEY", "") or ""

    # ── キャッシュ読み込み ────────────────────────────────────
    cache_data   = _load_bill_cache(cfg)
    cached_bills = cache_data.get("bills", {})

    # ── 法案取得 ─────────────────────────────────────────────
    raw_bills: list[dict] = []

    if api_key:
        print("    🔑 [congress] Congress.gov API で取得中...")
        raw_bills = _fetch_recent_bills(
            api_key  = api_key,
            congress = int(getattr(cfg, "CONGRESS_SESSION", 119)),
        )
        # 法案種別を付与
        for b in raw_bills:
            b["bill_types"] = _classify_bill_type(
                b.get("title", ""),
                b.get("latest_action", ""),
                cfg,
            )
        # 影響ある法案のみ残す
        raw_bills = [b for b in raw_bills if b.get("bill_types")]
    else:
        print("    ⚠️  [congress] CONGRESS_API_KEY 未設定 → "
              "RSS フォールバックで取得")
        raw_bills = _fetch_from_rss_fallback(cfg)

    print(f"    📋 [congress] 影響法案: {len(raw_bills)}件")

    # ── ステージ変化検出 ──────────────────────────────────────
    stage_changes = _detect_stage_changes(raw_bills, cached_bills)
    if stage_changes:
        print(f"    ⚡ [congress] ステージ変化: {len(stage_changes)}件")
        for sc in stage_changes[:3]:
            print(
                f"       [{sc['bill_id']}] "
                f"{sc['prev_stage']} → {sc['new_stage']}: "
                f"{sc['title'][:40]}"
            )

    # ── 新規法案の特定 ────────────────────────────────────────
    new_bills = [b for b in raw_bills if b["bill_id"] not in cached_bills]

    # ── キャッシュ更新 ────────────────────────────────────────
    for b in raw_bills:
        cached_bills[b["bill_id"]] = {
            **b,
            "cached_at": datetime.now().isoformat(),
        }
    cache_data["bills"] = cached_bills
    _save_bill_cache(cfg, cache_data)

    # ── 業種別ブースト計算 ────────────────────────────────────
    sector_boosts = _calc_sector_boosts(raw_bills, cfg)

    elapsed = (datetime.now() - t0).seconds
    total   = len(cached_bills)

    print(
        f"  ✅ [congress_monitor] 完了 ({elapsed}秒): "
        f"追跡{total}件 / 新規{len(new_bills)}件 / "
        f"ステージ変化{len(stage_changes)}件 / "
        f"影響業種{len(sector_boosts)}業種"
    )

    return {
        "bills":         list(raw_bills[:20]),
        "sector_boosts": sector_boosts,
        "new_bills":     new_bills[:10],
        "stage_changes": stage_changes,
        "total_tracked": total,
        "cached":        False,
        "timestamp":     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
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

    print("=== congress_monitor 単体テスト ===\n")

    # ── スコア計算の数値検証 ──────────────────────────────────
    mock_bills = [
        {
            "bill_id":   "HR1234/119",
            "title":     "CHIPS and Science Act Extension",
            "stage":     "committee",   # multiplier=0.40
            "bill_types": ["semiconductor_subsidy"],
            "latest_action": "Referred to Committee",
            "last_updated": "2026-05-01",
        },
        {
            "bill_id":   "S5678/119",
            "title":     "Steel Tariff Act",
            "stage":     "passed",      # multiplier=0.90
            "bill_types": ["tariff_steel"],
            "latest_action": "Passed Senate",
            "last_updated": "2026-05-28",
        },
    ]

    boosts = _calc_sector_boosts(mock_bills, cfg)

    print("  ── 業種別ブースト計算 ──")
    for sc, adj in sorted(boosts.items()):
        print(f"  sector={sc}: {adj:+.2f}pt")

    # semiconductor_subsidy: base=15.0 × 0.40 = 6.0 → 1016 +6.0
    # tariff_steel: base=-10.0 × 0.90 = -9.0 → 1012 -9.0
    sc_semi = boosts.get("1016", 0)
    sc_steel = boosts.get("1012", 0)
    assert abs(sc_semi - 6.0) < 0.01, f"1016 expected +6.0, got {sc_semi}"
    assert abs(sc_steel - (-9.0)) < 0.01, f"1012 expected -9.0, got {sc_steel}"
    print("  ✅ スコア計算 PASS")

    # ── ステージ変化検出 ──────────────────────────────────────
    cached = {
        "HR1234/119": {"stage": "introduced"},  # introduced → committee
        "S5678/119":  {"stage": "floor_vote"},  # floor_vote → passed
    }
    changes = _detect_stage_changes(mock_bills, cached)
    assert len(changes) == 2, f"変化2件期待、{len(changes)}件検出"
    print(f"  ✅ ステージ変化検出 PASS ({len(changes)}件)")

    print("\n  ✅ 全テスト PASS")
