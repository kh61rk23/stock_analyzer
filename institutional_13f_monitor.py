# ==============================================================
# institutional_13f_monitor.py  v1.0  (Wave 4 - Policy Ph3)
# ==============================================================
# 【役割】
#   SEC EDGAR Form 13F を四半期ごとに取得し、
#   機関投資家のポジション変化を検出・Tier別スコアリングを行う。
#
# 【13F とは】
#   米国 SEC 規則により運用資産1億ドル超の機関投資家が四半期末から
#   45日以内に提出義務がある保有株式開示書類。
#   URL: https://www.sec.gov/cgi-bin/browse-edgar
#   提供API: https://data.sec.gov/submissions/{CIK}.json (無料・APIキー不要)
#
# 【Tier 定義（config.INSTITUTION_TIER_MAP）】
#   Tier 1: Berkshire Hathaway（Buffett）→ 最高優先度
#   Tier 2: Pershing Square / Elliott 等（著名アクティビスト）
#   Tier 3: BlackRock / Vanguard / State Street 等（大手機関）
#
# 【スコアリング】
#   $tier\_score = base\_score \times (1 + value\_multiplier)$
#   Tier 1: base=15pt / Tier 2: base=10pt / Tier 3: base=5pt
#   新規取得（new_position）: +100% / 増加（increased）: +50%
#   減少（decreased）: -50% / 売却（sold）: -100%
#   上限: SCORE_MAX_INSTITUTION_ADJ (15pt)
#
# 【出力スキーマ（run_institutional_13f_monitor の返り値）】
#   {
#     "positions":       {...},   # {cik: {ticker: position_dict}}
#     "changes":         [...],   # ポジション変化リスト
#     "scored":          [...],   # スコアリング済みリスト
#     "sector_boosts":   {...},   # {sector_code: adj_pt}
#     "new_tier1":       [...],   # Tier1新規取得（Buffettアラート用）
#     "new_large":       [...],   # 10億ドル超新規取得（Gmailアラート用）
#     "total_changes":   int,
#     "cached":          bool,
#     "quarter":         str,     # "2026Q1" など
#     "timestamp":       str,
#   }
#
# 【キャッシュ】
#   CACHE_DIR/institutional/institutional_history.json
#     TTL: INSTITUTION_HISTORY_TTL_DAYS (180日)
#   CACHE_DIR/institutional/13f_positions_{quarter}.json
#     四半期ごとのポジションスナップショット（恒久保持）
#
# 【実行頻度】
#   四半期フラグ（is_quarter_end）が True の場合のみフル取得。
#   それ以外は institutional_history.json のキャッシュを返す。
#
# 【呼び出し方（stock_analyzer_main.py [17.7] から）】
#   from institutional_13f_monitor import run_institutional_13f_monitor
#   result = run_institutional_13f_monitor(cfg, results, is_quarter_end=False)
#
# 【依存】
#   config.py (BLOCK 35: INSTITUTION_TIER_MAP / SCORE_MAX_INSTITUTION_ADJ /
#              INSTITUTION_HISTORY_TTL_DAYS / INSTITUTION_MIN_NEW_POSITION_USD)
# ==============================================================

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


# ==============================================================
# BLOCK 1: Tier 定義・スコア定数フォールバック
# ==============================================================

_TIER_BASE_SCORE: dict = {
    1: 15.0,   # Buffett クラス
    2: 10.0,   # 著名アクティビスト
    3:  5.0,   # 大手機関
}

_CHANGE_MULTIPLIER: dict = {
    "new_position": 1.0,    # 新規取得: +100%
    "increased":    0.5,    # 増加:     +50%
    "decreased":   -0.5,    # 減少:     -50%
    "sold":        -1.0,    # 売却:     -100%
    "unchanged":    0.0,    # 変化なし
}

# SEC EDGAR の主要機関 CIK（Central Index Key）
# INSTITUTION_TIER_MAP の名称と対応
_KNOWN_CIKS: dict = {
    "0001067983": {"name": "Berkshire Hathaway Inc", "tier": 1},
    "0001336528": {"name": "Pershing Square Capital Management", "tier": 2},
    "0001418819": {"name": "Elliott Investment Management", "tier": 2},
    "0001011006": {"name": "Third Point LLC", "tier": 2},
    "0001364742": {"name": "BlackRock Inc", "tier": 3},
    "0000102909": {"name": "Vanguard Group", "tier": 3},
    "0000093751": {"name": "State Street", "tier": 3},
    "0000315066": {"name": "Fidelity", "tier": 3},
    "0001113169": {"name": "T. Rowe Price", "tier": 3},
}

# 四半期終了月（3/6/9/12月）
_QUARTER_END_MONTHS = {3, 6, 9, 12}


def _current_quarter() -> str:
    """現在の四半期文字列を返す（例: "2026Q1"）。"""
    now = datetime.now()
    q   = (now.month - 1) // 3 + 1
    return f"{now.year}Q{q}"


def _is_quarter_filing_period() -> bool:
    """
    13F提出期間（四半期末から45日以内）かどうかを判定する。
    1月15日〜2月14日 / 4月15日〜5月14日 / 7月15日〜8月14日 / 10月15日〜11月14日
    """
    now = datetime.now()
    m, d = now.month, now.day
    # 四半期末の翌月15日〜翌々月14日
    filing_windows = [
        (1, 15, 2, 14),    # Q4末(12月)→1/15〜2/14
        (4, 15, 5, 14),    # Q1末(3月)→4/15〜5/14
        (7, 15, 8, 14),    # Q2末(6月)→7/15〜8/14
        (10, 15, 11, 14),  # Q3末(9月)→10/15〜11/14
    ]
    for start_m, start_d, end_m, end_d in filing_windows:
        if (m == start_m and d >= start_d) or \
           (m == end_m and d <= end_d):
            return True
    return False


# ==============================================================
# BLOCK 2: キャッシュ管理
# ==============================================================

def _inst_cache_dir(cfg) -> Path:
    base = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache"))
    d = base / "institutional"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_history(cfg) -> dict:
    """
    institutional_history.json を読み込む。
    TTL: INSTITUTION_HISTORY_TTL_DAYS (180日)

    Returns:
        {
          "quarters": {"2026Q1": {cik: {ticker: {...}}}},
          "changes":  [...],
          "last_updated": str,
        }
    """
    p = _inst_cache_dir(cfg) / "institutional_history.json"
    if not p.exists():
        return {"quarters": {}, "changes": [], "last_updated": ""}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        ttl  = int(getattr(cfg, "INSTITUTION_HISTORY_TTL_DAYS", 180))
        # changes の TTL 切れエントリを除去
        cutoff = datetime.now() - timedelta(days=ttl)
        changes = [
            c for c in data.get("changes", [])
            if _safe_parse_dt(c.get("detected_at", "")) >= cutoff
        ]
        data["changes"] = changes
        return data
    except Exception as e:
        print(f"    ⚠️  [13f] history 読み込み失敗: {str(e)[:60]}")
        return {"quarters": {}, "changes": [], "last_updated": ""}


def _save_history(cfg, data: dict) -> None:
    p = _inst_cache_dir(cfg) / "institutional_history.json"
    try:
        data["last_updated"] = datetime.now().isoformat()
        p.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"    ⚠️  [13f] history 保存失敗: {str(e)[:60]}")


def _safe_parse_dt(s: str) -> datetime:
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return datetime(2000, 1, 1)


# ==============================================================
# BLOCK 3: SEC EDGAR API からポジション取得
# ==============================================================

def _fetch_cik_filings(cik: str, timeout: int = 15) -> Optional[dict]:
    """
    SEC EDGAR submissions API から CIK のファイリング一覧を取得する。
    URL: https://data.sec.gov/submissions/{CIK}.json

    Returns:
        submissions dict or None
    """
    cik_padded = cik.lstrip("0").zfill(10)
    url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
    try:
        import urllib.request
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "stock_analyzer/1.0 (contact@example.com)",
                "Accept":     "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"    ⚠️  [13f] CIK {cik} 取得失敗: {str(e)[:50]}")
        return None


def _fetch_13f_holdings(
    accession_number: str,
    cik: str,
    timeout: int = 20,
) -> list[dict]:
    """
    13F ファイリングの保有銘柄一覧を取得する。
    EDGAR Full-Text Search API を利用。

    Returns:
        [{"ticker": str, "value_usd": int, "shares": int,
          "name": str, "cusip": str}, ...]
    """
    holdings = []
    # accession_number: "0001067983-23-000001" → "0001067983230000001"
    acc_clean = accession_number.replace("-", "")
    cik_padded = cik.lstrip("0").zfill(10)
    url = (
        f"https://www.sec.gov/Archives/edgar/data/"
        f"{int(cik_padded)}/{acc_clean}/infotable.json"
    )
    try:
        import urllib.request
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "stock_analyzer/1.0 (contact@example.com)",
                "Accept":     "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        for h in data.get("data", []):
            # infotable.json のフォーマット:
            # [name, cusip, value_x1000, shares, type, ...]
            if not isinstance(h, list) or len(h) < 4:
                continue
            name  = str(h[0])
            cusip = str(h[1])
            try:
                value_usd = int(h[2]) * 1000
                shares    = int(h[3])
            except (ValueError, TypeError):
                continue
            holdings.append({
                "name":      name,
                "cusip":     cusip,
                "value_usd": value_usd,
                "shares":    shares,
                "ticker":    "",   # CUSIP→ticker 変換は別途
            })
    except Exception as e:
        print(f"    ⚠️  [13f] 保有リスト取得失敗 ({accession_number[:20]}): "
              f"{str(e)[:50]}")

    return holdings


def _get_latest_13f_accession(submissions: dict) -> Optional[str]:
    """
    submissions JSON から最新の 13F-HR ファイリングの
    accession number を返す。
    """
    try:
        recent = submissions.get("filings", {}).get("recent", {})
        forms  = recent.get("form", [])
        acc_ns = recent.get("accessionNumber", [])
        for i, form in enumerate(forms):
            if form in ("13F-HR", "13F-HR/A") and i < len(acc_ns):
                return acc_ns[i]
    except Exception:
        pass
    return None


def _cusip_to_ticker(cusip: str) -> str:
    """
    CUSIP から ticker への変換。
    実装: 簡易マッピングテーブル（主要銘柄のみ）。
    完全対応は OpenFIGI API が必要だが有料のため省略。

    Returns:
        str: ticker or ""
    """
    # 主要 CUSIP → ticker マッピング（拡張可能）
    _CUSIP_MAP = {
        "037833100": "AAPL",
        "594918104": "MSFT",
        "02079K305": "GOOGL",
        "023135106": "AMZN",
        "67066G104": "NVDA",
        "30303M102": "META",
        "88160R101": "TSLA",
        "92826C839": "V",
        "14272R107": "CARR",
        "922908363": "VZ",
        "931142103": "WMT",
        "808513105": "SCHW",
        "857477103": "STZ",
    }
    return _CUSIP_MAP.get(cusip, "")


# ==============================================================
# BLOCK 4: ポジション変化の検出
# ==============================================================

def _detect_changes(
    curr_positions: dict,
    prev_positions: dict,
    cik_info:       dict,
) -> list[dict]:
    """
    前四半期との比較でポジション変化を検出する。

    Args:
        curr_positions: {ticker: {"value_usd": int, "shares": int, ...}}
        prev_positions: 同上（前四半期）
        cik_info:       {"name": str, "tier": int}

    Returns:
        [{"ticker": str, "change_type": str, "tier": int,
          "institution": str, "value_usd": int,
          "prev_value_usd": int, "value_change_usd": int}, ...]
    """
    changes = []
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    inst    = cik_info.get("name", "")
    tier    = cik_info.get("tier", 3)

    # 全ティッカーの和集合
    all_tickers = set(curr_positions) | set(prev_positions)

    for ticker in all_tickers:
        curr = curr_positions.get(ticker)
        prev = prev_positions.get(ticker)

        if curr and not prev:
            change_type = "new_position"
        elif not curr and prev:
            change_type = "sold"
        elif curr and prev:
            curr_val = curr.get("value_usd", 0)
            prev_val = prev.get("value_usd", 0)
            if curr_val > prev_val * 1.10:
                change_type = "increased"
            elif curr_val < prev_val * 0.90:
                change_type = "decreased"
            else:
                change_type = "unchanged"
        else:
            continue

        if change_type == "unchanged":
            continue

        changes.append({
            "ticker":           ticker,
            "institution":      inst,
            "tier":             tier,
            "change_type":      change_type,
            "value_usd":        (curr or {}).get("value_usd", 0),
            "prev_value_usd":   (prev or {}).get("value_usd", 0),
            "value_change_usd": (
                (curr or {}).get("value_usd", 0) -
                (prev or {}).get("value_usd", 0)
            ),
            "shares":           (curr or {}).get("shares", 0),
            "name":             (curr or prev or {}).get("name", ticker),
            "detected_at":      now_str,
        })

    return changes


# ==============================================================
# BLOCK 5: スコアリング
# ==============================================================

def _score_changes(changes: list[dict], cfg) -> list[dict]:
    """
    変化リストにスコアを付与する。

    $adj\\_pt = base\\_score \times (1 + multiplier)$
    上限: SCORE_MAX_INSTITUTION_ADJ

    Returns:
        [{"ticker": str, "adj_pt": float, ...（元フィールドも保持）}, ...]
    """
    max_adj = float(getattr(cfg, "SCORE_MAX_INSTITUTION_ADJ", 15.0))
    scored  = []

    for c in changes:
        tier        = c.get("tier", 3)
        change_type = c.get("change_type", "unchanged")
        base        = _TIER_BASE_SCORE.get(tier, 3.0)
        multiplier  = _CHANGE_MULTIPLIER.get(change_type, 0.0)

        raw_adj = base * (1.0 + multiplier)
        adj_pt  = round(max(-max_adj, min(max_adj, raw_adj)), 2)

        scored.append({**c, "adj_pt": adj_pt})

    # adj_pt の絶対値降順でソート
    scored.sort(key=lambda x: abs(x.get("adj_pt", 0)), reverse=True)
    return scored


def _build_sector_boosts(
    scored:    list[dict],
    results:   dict,
    cfg,
) -> dict[str, float]:
    """
    スコアリング結果を業種コードに集約する。
    ticker → sector_code のマッピングは screener candidates から取得。

    Returns:
        {sector_code: total_adj_pt}
    """
    # ticker → sector_code マッピングを構築
    ticker_sector: dict[str, str] = {}
    screener = results.get("screener", {})
    for cand in (screener.get("candidates", [])
                 if isinstance(screener, dict) else []):
        t  = cand.get("ticker", "")
        sc = cand.get("sector_code", "")
        if t and sc:
            ticker_sector[t] = sc

    max_adj = float(getattr(cfg, "SCORE_MAX_INSTITUTION_ADJ", 15.0))
    boosts: dict[str, float] = {}

    for s in scored:
        ticker = s.get("ticker", "")
        sc     = ticker_sector.get(ticker, "")
        if not sc:
            continue
        adj = s.get("adj_pt", 0.0)
        boosts[sc] = round(
            max(-max_adj, min(max_adj, boosts.get(sc, 0.0) + adj)), 2
        )

    return boosts


# ==============================================================
# BLOCK 6: メインエントリポイント
# ==============================================================

def run_institutional_13f_monitor(
    cfg,
    results:          dict,
    is_quarter_end:   bool = False,
) -> dict:
    """
    機関投資家 13F ポジション監視のメイン関数。
    stock_analyzer_main.py [17.7] から四半期フラグ付きで呼ばれる。

    Args:
        cfg:             config モジュール
        results:         stock_analyzer_main.results
        is_quarter_end:  True のときフル取得。False はキャッシュ利用。

    Returns:
        run_institutional_13f_monitor の出力スキーマ参照
    """
    print("\n  🏦 [13f] 機関投資家ポジション監視 開始...")
    t0      = datetime.now()
    quarter = _current_quarter()

    # ── キャッシュ確認（四半期フラグ False の場合） ───────────
    history = _load_history(cfg)
    if not is_quarter_end:
        cached_changes = history.get("changes", [])
        if cached_changes:
            print(f"    📦 [13f] キャッシュ利用 (quarter={quarter}): "
                  f"{len(cached_changes)}件の変化")
            # キャッシュからも scored / boosts を再構築
            _scored = _score_changes(cached_changes, cfg)
            _boosts = _build_sector_boosts(_scored, results, cfg)
            return _build_result(
                quarter, cached_changes, _scored, _boosts,
                history.get("positions", {}), cached=True,
            )
        print(f"    📋 [13f] キャッシュなし → 軽量スキャン実行")

    # ── 取得対象 CIK の確定 ───────────────────────────────────
    tier_map = getattr(cfg, "INSTITUTION_TIER_MAP", {})

    # config の INSTITUTION_TIER_MAP から CIK を逆引き（名前→CIK）
    # _KNOWN_CIKS をベースに tier_map の名称でフィルタ
    target_ciks = {
        cik: info for cik, info in _KNOWN_CIKS.items()
        if info["name"] in tier_map
        or any(info["name"].startswith(k) for k in tier_map)
    }
    # 未マッチの場合は _KNOWN_CIKS 全件を使用
    if not target_ciks:
        target_ciks = _KNOWN_CIKS

    print(f"    🎯 [13f] 監視対象: {len(target_ciks)}機関")

    # ── 前四半期のポジションを取得 ────────────────────────────
    quarters_data = history.get("quarters", {})
    prev_quarter  = _prev_quarter(quarter)
    prev_positions_all = quarters_data.get(prev_quarter, {})

    # ── 各機関の最新 13F を取得 ──────────────────────────────
    curr_positions_all: dict = {}
    all_changes:        list = []

    for cik, cik_info in target_ciks.items():
        name = cik_info["name"]
        print(f"    📄 [13f] {name} ({cik}) を取得中...")

        try:
            submissions = _fetch_cik_filings(cik)
            if not submissions:
                continue

            acc = _get_latest_13f_accession(submissions)
            if not acc:
                print(f"    ⚠️  [13f] {name}: 13F ファイリングなし")
                continue

            holdings = _fetch_13f_holdings(acc, cik)
            if not holdings:
                print(f"    ⚠️  [13f] {name}: 保有リスト取得失敗")
                continue

            # CUSIP → ticker 変換
            curr_pos: dict = {}
            for h in holdings:
                ticker = h.get("ticker") or _cusip_to_ticker(h.get("cusip", ""))
                if ticker:
                    curr_pos[ticker] = {
                        "value_usd": h["value_usd"],
                        "shares":    h["shares"],
                        "name":      h["name"],
                    }

            curr_positions_all[cik] = curr_pos
            prev_pos = prev_positions_all.get(cik, {})

            changes = _detect_changes(curr_pos, prev_pos, cik_info)
            all_changes.extend(changes)

            print(f"    ✅ [13f] {name}: "
                  f"{len(curr_pos)}銘柄保有 / "
                  f"{len(changes)}件の変化")
            time.sleep(0.5)   # SEC サーバへの負荷軽減

        except Exception as e:
            print(f"    ⚠️  [13f] {name} 処理エラー（スキップ）: "
                  f"{str(e)[:60]}")

    # ── スコアリング ──────────────────────────────────────────
    scored  = _score_changes(all_changes, cfg)
    boosts  = _build_sector_boosts(scored, results, cfg)

    # ── Tier1 新規取得 / 10億ドル超検出（アラート用）─────────
    min_large = float(getattr(cfg, "INSTITUTION_MIN_NEW_POSITION_USD",
                               1_000_000_000))
    new_tier1 = [
        c for c in all_changes
        if c.get("tier") == 1 and c.get("change_type") == "new_position"
    ]
    new_large = [
        c for c in all_changes
        if c.get("change_type") == "new_position"
        and c.get("value_usd", 0) >= min_large
    ]

    # ── history 更新 ──────────────────────────────────────────
    history["quarters"][quarter] = curr_positions_all
    history["changes"]           = all_changes
    history["positions"]         = curr_positions_all
    _save_history(cfg, history)

    # ── BQ 格納（us.institutional_positions）────────────────
    # edinet_bq_client.py 経由で INSERT。モジュール未配置・BQ未接続時も
    # メイン処理に影響を与えないよう try/except で全体を保護する。
    try:
        import sys as _sys_bq, importlib as _imp_bq
        from pathlib import Path as _pl_bq
        _sys_bq.path.insert(0, str(_pl_bq(__file__).parent))
        _bq = _imp_bq.import_module("edinet_bq_client")
        _project = getattr(cfg, "INSTITUTIONAL_BQ_PROJECT",
                           "stock-analyzer-edinet")
        _table   = getattr(cfg, "INSTITUTIONAL_BQ_TABLE",
                           "us.institutional_positions")
        _now_str = datetime.now().isoformat()
        _rows: list[dict] = []
        for _chg in all_changes:
            _rows.append({
                "quarter":          quarter,
                "cik":              next(
                    (k for k, v in target_ciks.items()
                     if v["name"] == _chg.get("institution", "")), ""
                ),
                "institution_name": _chg.get("institution", ""),
                "tier":             int(_chg.get("tier", 3)),
                "ticker":           _chg.get("ticker", ""),
                "company_name":     _chg.get("name", ""),
                "change_type":      _chg.get("change_type", ""),
                "value_usd":        int(_chg.get("value_usd", 0)),
                "prev_value_usd":   int(_chg.get("prev_value_usd", 0)),
                "value_change_usd": int(_chg.get("value_change_usd", 0)),
                "shares":           int(_chg.get("shares", 0)),
                "adj_pt":           float(
                    next((s["adj_pt"] for s in scored
                          if s["ticker"] == _chg.get("ticker", "")), 0.0)
                ),
                "detected_at":      _chg.get("detected_at", _now_str),
                "updated_at":       _now_str,
            })
        if _rows:
            _bq.insert_rows(
                project_id = _project,
                table_ref  = _table,
                rows       = _rows,
            )
            print(f"    📊 [13f] BQ格納: {len(_rows)}件 → {_table}")
    except ModuleNotFoundError:
        print("    ⚠️  [13f] edinet_bq_client 未配置 → BQ格納スキップ")
    except Exception as _bq_e:
        print(f"    ⚠️  [13f] BQ格納失敗（スキップ）: {str(_bq_e)[:60]}")

    elapsed = (datetime.now() - t0).seconds
    print(
        f"\n  ✅ [13f] 完了 ({elapsed}秒): "
        f"{len(all_changes)}件の変化検出 / "
        f"Tier1新規={len(new_tier1)}件 / "
        f"大口新規={len(new_large)}件"
    )

    result = _build_result(
        quarter, all_changes, scored, boosts,
        curr_positions_all, cached=False,
    )
    result["new_tier1"] = new_tier1
    result["new_large"] = new_large
    return result


def _build_result(
    quarter:   str,
    changes:   list,
    scored:    list,
    boosts:    dict,
    positions: dict,
    cached:    bool,
) -> dict:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return {
        "positions":     positions,
        "changes":       changes,
        "scored":        scored[:20],   # UI表示用に上位20件
        "sector_boosts": boosts,
        "new_tier1":     [],
        "new_large":     [],
        "total_changes": len(changes),
        "cached":        cached,
        "quarter":       quarter,
        "timestamp":     now_str,
    }


def _prev_quarter(quarter: str) -> str:
    """前四半期文字列を返す（例: "2026Q1" → "2025Q4"）。"""
    try:
        year = int(quarter[:4])
        q    = int(quarter[5])
        if q == 1:
            return f"{year-1}Q4"
        return f"{year}Q{q-1}"
    except Exception:
        return ""


# ==============================================================
# BLOCK 7: 単体テスト
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

    print("=== institutional_13f_monitor 単体テスト ===\n")

    # ── ポジション変化検出の数値検証 ─────────────────────────
    mock_curr = {
        "AAPL": {"value_usd": 10_000_000_000, "shares": 5_000_000, "name": "Apple"},
        "NVDA": {"value_usd":  2_000_000_000, "shares":   800_000, "name": "Nvidia"},
    }
    mock_prev = {
        "AAPL": {"value_usd":  9_000_000_000, "shares": 4_500_000, "name": "Apple"},
        "MSFT": {"value_usd":  1_000_000_000, "shares":   300_000, "name": "Microsoft"},
    }
    cik_info = {"name": "Berkshire Hathaway Inc", "tier": 1}
    changes  = _detect_changes(mock_curr, mock_prev, cik_info)

    print("  ── 変化検出結果 ──")
    for c in changes:
        print(f"  [{c['ticker']}] {c['change_type']} "
              f"value={c['value_usd']:,} "
              f"prev={c['prev_value_usd']:,}")

    assert any(c["ticker"] == "NVDA" and c["change_type"] == "new_position"
               for c in changes), "NVDA new_position 未検出"
    assert any(c["ticker"] == "MSFT" and c["change_type"] == "sold"
               for c in changes), "MSFT sold 未検出"
    # AAPL: 9→10億（+11.1%）→ increased
    assert any(c["ticker"] == "AAPL" and c["change_type"] == "increased"
               for c in changes), "AAPL increased 未検出"

    # ── スコアリング検証 ─────────────────────────────────────
    scored = _score_changes(changes, cfg)
    print("\n  ── スコアリング結果 ──")
    for s in scored:
        print(f"  [{s['ticker']}] {s['change_type']} "
              f"tier={s['tier']} adj_pt={s['adj_pt']:+.1f}pt")

    # Tier1 new_position: base=15 × (1+1.0) = 30 → clamp → 15
    nvda_s = next((s for s in scored if s["ticker"] == "NVDA"), None)
    assert nvda_s and nvda_s["adj_pt"] == 15.0, \
        f"NVDA adj_pt={nvda_s['adj_pt']} ≠ 15.0"
    # Tier1 sold: base=15 × (1-1.0) = 0
    msft_s = next((s for s in scored if s["ticker"] == "MSFT"), None)
    assert msft_s and msft_s["adj_pt"] == 0.0, \
        f"MSFT adj_pt={msft_s['adj_pt']} ≠ 0.0"

    print("\n  ✅ 全テスト PASS")