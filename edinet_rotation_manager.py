# ==============================================================
# edinet_rotation_manager.py  v1.0
# ==============================================================
# 【概要】
#   EdinetDB（無料プラン: 100回/日）を毎日33社分消費し、
#   東証全上場銘柄を約77日（11週）でローテーション取得する。
#   取得済みデータはローカルDB（JSON）に蓄積し、
#   screenerはAPIを叩かずDBを参照することでコールを節約する。
#
# 【1日あたりのコール配分（合計80コール・バッファ20）】
#   ウォッチリスト優先    : 最大10社 × 1コール = 10コール
#   screener通過候補      : 最大 8社 × 1コール =  8コール
#   ローテーション取得    : 最大33社 × 1コール = 33コール
#   rankings取得          : 2回固定             =  2コール
#   リトライバッファ      : 5コール（失敗時再試行用）
#   合計                  :                      58コール（余裕を持った設計）
#
# 【1コール = analysis（信用スコア+リスク）のみ取得】
#   financials / earnings は edinet_client.py に get_financials() /
#   get_earnings() が追加されたタイミングで本ファイルを拡張する。
#   現時点では search_company + get_analysis の2エンドポイントを使用。
#   （search_company は認証不要のため、実質 1コール/社）
#
# 【蓄積DBの構造】
#   {DATA_DIR}/edinet_db/
#     meta.json              ← 全銘柄の管理台帳（最終取得日・次回予定）
#     analysis/{E_CODE}.json ← 信用スコア・リスク情報（30日キャッシュ）
#
# 【config.py で必要な追加設定】
#   ENABLE_EDINET_ROTATION   : bool = True   有効化フラグ
#   EDINET_ROTATION_PER_DAY  : int  = 33     1日あたり取得社数
#   EDINET_WATCHLIST_TICKERS : list = []     ウォッチリスト銘柄（優先取得）
#   EDINET_SCREENER_CANDIDATES: list = []   screener通過候補（前日分）
#   EDINET_ROTATION_CACHE_DAYS: int = 30    analysis再取得間隔（日）
#   EDINET_DAILY_BUDGET      : int = 80     1日あたり最大コール数
#
# 【stock_analyzer_main.py からの呼び出し】
#   _erm = _load_local_module("edinet_rotation_manager", ...)
#   if _erm:
#       erm_result = _erm.run_edinet_rotation(cfg, results)
#       results["edinet_rotation"] = erm_result
#
# 【screener からの参照】
#   _erm = _load_local_module("edinet_rotation_manager", ...)
#   db   = _erm.load_db(cfg)
#   info = _erm.lookup(db, "8306.T")  # → analysis dict or None
# ==============================================================

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ==============================================================
# BLOCK 1: 定数・パス管理
# ==============================================================

DB_DIR_NAME   = "edinet_db"
META_FILENAME = "meta.json"
ANALYSIS_DIR  = "analysis"

# 東証プライム代表銘柄（sector_code → 上位銘柄リスト）
# JAPAN_SECTORSの33業種すべてに対応。各業種5社。
# ※ローテーション初回はこのリストから順に取得する
# SECTOR_REPRESENTATIVE_TICKERS = {
#     "1001": ["1301.T","1332.T","1379.T","2002.T","3941.T"],
#     "1002": ["1662.T","1605.T","1663.T","1664.T","1667.T"],
#     "1003": ["1925.T","1928.T","1803.T","1801.T","1802.T"],
#     "1004": ["2914.T","2802.T","2503.T","2501.T","2282.T"],
#     "1005": ["3401.T","3402.T","3404.T","3405.T","3410.T"],
#     "1006": ["3861.T","3863.T","3864.T","3865.T","3880.T"],
#     "1007": ["4063.T","4183.T","4901.T","4502.T","4188.T"],
#     "1008": ["4502.T","4503.T","4519.T","4523.T","4568.T"],
#     "1009": ["5019.T","5020.T","5021.T","5101.T","5103.T"],
#     "1010": ["5101.T","5108.T","5110.T","5191.T","5214.T"],
#    "1011": ["5201.T","5202.T","5214.T","5233.T","5301.T"],
#     "1012": ["5401.T","5406.T","5411.T","5423.T","5440.T"],
#     "1013": ["5711.T","5713.T","5714.T","5801.T","5802.T"],
#     "1014": ["5901.T","5902.T","5985.T","6011.T","6013.T"],
#     "1015": ["6301.T","6302.T","6305.T","6326.T","6361.T"],
#     "1016": ["6501.T","6503.T","6504.T","6594.T","6702.T"],
#     "1017": ["7203.T","7267.T","7269.T","7270.T","7201.T"],
#     "1018": ["7731.T","7733.T","7741.T","7762.T","7752.T"],
#     "1019": ["3765.T","4902.T","7951.T","7974.T","4684.T"],
#     "1020": ["9501.T","9502.T","9503.T","9531.T","9532.T"],
#     "1021": ["9001.T","9005.T","9007.T","9008.T","9009.T"],
#     "1022": ["9101.T","9104.T","9107.T","9119.T","9132.T"],
#     "1023": ["9201.T","9202.T","9205.T","9207.T","9048.T"],
#     "1024": ["9301.T","9302.T","9303.T","9304.T","9305.T"],
#     "1025": ["9432.T","9433.T","9434.T","9613.T","9984.T"],
#     "1026": ["2768.T","8001.T","8002.T","8031.T","8058.T"],
#     "1027": ["2651.T","3382.T","7532.T","7741.T","8267.T"],
#     "1028": ["8306.T","8316.T","8411.T","8354.T","8308.T"],
#     "1029": ["8601.T","8604.T","8002.T","8630.T","8697.T"],
#     "1030": ["8725.T","8750.T","8766.T","8795.T","8697.T"],
#     "1031": ["8253.T","8591.T","8601.T","8697.T","8725.T"],
#    "1032": ["8801.T","8802.T","8803.T","8804.T","8830.T"],
#     "1033": ["2413.T","2432.T","4307.T","4755.T","6098.T"],
# }


# ==============================================================
# BLOCK 2: 蓄積DB 読み書き
# ==============================================================

def _get_db_dir(cfg) -> Path:
    """蓄積DBのルートディレクトリを返す"""
    data_dir = Path(getattr(cfg, "DATA_DIR", "/tmp"))
    return data_dir / DB_DIR_NAME


def load_meta(cfg) -> dict:
    """
    meta.json を読み込んで返す。
    構造:
      {
        "8306.T": {
          "sec_code":     "8306",
          "edinet_code":  "E03518",
          "name":         "三菱UFJフィナンシャル・グループ",
          "market":       "Prime",
          "sector_code":  "1028",
          "last_fetched": "2026-03-10",
          "next_due":     "2026-04-09",
          "fetch_count":  3,
          "priority":     1,   # 1=ウォッチリスト/2=screener通過/3=通常
        },
        ...
      }
    """
    meta_path = _get_db_dir(cfg) / META_FILENAME
    if not meta_path.exists():
        return {}
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_meta(cfg, meta: dict):
    """meta.json を保存する"""
    db_dir = _get_db_dir(cfg)
    db_dir.mkdir(parents=True, exist_ok=True)
    meta_path = db_dir / META_FILENAME
    try:
        meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    except Exception as e:
        print(f"    ⚠️  meta.json 保存失敗: {str(e)[:60]}")


def load_db(cfg) -> dict:
    """
    蓄積DB全体（meta + analysisデータ）を返す。
    screener から参照するための公開API。

    Returns:
        dict: {"meta": {ticker: meta_entry}, "analysis": {ticker: analysis}}
    """
    meta = load_meta(cfg)
    analysis_dir = _get_db_dir(cfg) / ANALYSIS_DIR
    analysis = {}
    if analysis_dir.exists():
        for f in analysis_dir.glob("*.json"):
            try:
                edinet_code = f.stem
                analysis[edinet_code] = json.loads(
                    f.read_text(encoding="utf-8"))
            except Exception:
                pass

    # ticker → analysis にマッピング
    ticker_analysis = {}
    for ticker, meta_entry in meta.items():
        ec = meta_entry.get("edinet_code", "")
        if ec and ec in analysis:
            ticker_analysis[ticker] = analysis[ec]

    return {"meta": meta, "analysis": ticker_analysis}


def lookup(db: dict, ticker: str) -> Optional[dict]:
    """
    蓄積DB から1銘柄の analysis データを返す。
    screener から単体で参照するための公開API。

    Args:
        db     : load_db() の戻り値
        ticker : "8306.T" 形式

    Returns:
        dict | None: {credit_score, credit_rating, risk_factors, ...}
    """
    return db.get("analysis", {}).get(ticker)


def _save_analysis(cfg, edinet_code: str, data: dict):
    """analysis データをファイルに保存する"""
    ana_dir = _get_db_dir(cfg) / ANALYSIS_DIR
    ana_dir.mkdir(parents=True, exist_ok=True)
    path = ana_dir / f"{edinet_code}.json"
    try:
        data["_saved_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    except Exception as e:
        print(f"    ⚠️  analysis 保存失敗 ({edinet_code}): {str(e)[:60]}")


# ==============================================================
# BLOCK 3: 取得キュー構築
# ==============================================================

def _build_fetch_queue(cfg, meta: dict, results: dict | None = None) -> list:
    """
    優先順位に従って本日取得すべき銘柄リストを返す。
    BigQuery edinet.doc_index から取得対象銘柄を動的に構築する。

    優先度:
      1. ウォッチリスト銘柄（EDINET_WATCHLIST_TICKERS）
      2. screener通過候補（EDINET_SCREENER_CANDIDATES）
      3. BQから取得した未取得銘柄（doc_index.fetched_flag=False）
    """
    today       = datetime.now().date()
    cache_days  = getattr(cfg, "EDINET_ROTATION_CACHE_DAYS", 30)
    per_day     = getattr(cfg, "EDINET_ROTATION_PER_DAY", 33)
    budget      = getattr(cfg, "EDINET_DAILY_BUDGET", 80)
    watchlist   = getattr(cfg, "EDINET_WATCHLIST_TICKERS", []) \
                  or getattr(cfg, "PRIORITY_TICKERS", [])
    candidates  = getattr(cfg, "EDINET_SCREENER_CANDIDATES", [])

    queue     = []
    seen      = set()
    
    # BQクライアントのインポート
    try:
        from edinet_bq_client import EDINETBQClient
        bq_client = EDINETBQClient()
    except ImportError:
        print("    ⚠️ edinet_bq_client のインポートに失敗しました。")
        bq_client = None

    # catalyst_proximity_score のマップを事前取得（_add() から参照）
    _cat_scores: dict[str, float] = {}
    try:
        _cat_scores = _get_catalyst_priority_tickers(cfg)
    except Exception:
        pass

    def _add(ticker: str, priority: int, reason: str):
        t = ticker.strip()
        if t and t not in seen:
            seen.add(t)
            queue.append({
                "ticker":                   t,
                "priority":                 priority,
                "reason":                   reason,
                "catalyst_proximity_score": _cat_scores.get(t, 0.0)
                    if "_cat_scores" in dir() else 0.0,
            })
    # ── 優先度1: ウォッチリスト（最大10社） ─────────────────
    for t in watchlist[:10]:
        entry = meta.get(t, {})
        last  = entry.get("last_fetched")
        if not last or _days_since(last) >= cache_days:
            _add(t, 1, "watchlist")

    # ── 優先度2: screener通過候補（最大8社） ────────────────
    for t in candidates[:8]:
        entry = meta.get(t, {})
        last  = entry.get("last_fetched")
        if not last or _days_since(last) >= cache_days:
            _add(t, 2, "screener_pass")

    # ── [FE-7] 優先度1.5相当: Catalyst 接近銘柄を上位挿入 ──
    # catalyst_proximity_score ≥ 1.0（14日以内）→ 優先度1へ昇格
    # catalyst_proximity_score ≥ 0.6（30日以内）→ 優先度2に追加
    _cat_tickers = _get_catalyst_priority_tickers(cfg)
    _cat_inserted = 0
    for _ct, _prox in _cat_tickers.items():
        if _cat_inserted >= 5:   # Catalyst 枠は最大5社
            break
        _entry = meta.get(_ct, {})
        _last  = _entry.get("last_fetched")
        if _last and _days_since(_last) < cache_days:
            continue   # 最近取得済みはスキップ
        # proximity_score ≥ 1.0 (14日以内) → 優先度1へ昇格
        _priority = 1 if _prox >= 1.0 else 2
        _add(_ct, _priority,
             f"catalyst_proximity(score={_prox:.1f})")
        _cat_inserted += 1

    # ── [S3-4] priority=0: deep_dive_trigger 指定銘柄（最優先）───
    # deep_dive_trigger.run_deep_dive_trigger() が cfg.EDINET_DEEP_DIVE_TICKERS
    # に注入したティッカーを最優先で取得する。
    # priority=0 は既存の priority=1（watchlist）より高優先。
    # キャッシュ期限（cache_days）に関わらず強制再取得する。
    _deep_dive_tickers = getattr(cfg, "EDINET_DEEP_DIVE_TICKERS", []) or []
    if _deep_dive_tickers:
        _dd_added = 0
        for t in _deep_dive_tickers[:5]:   # 深掘り枠は最大5社
            t = t.strip()
            if not t:
                continue
            # キャッシュ期限を無視して強制再取得（meta から last_fetched を無視）
            if t not in seen:
                seen.add(t)
                queue.append({
                    "ticker":                   t,
                    "priority":                 0,   # 最優先
                    "reason":                   "deep_dive_trigger",
                    "catalyst_proximity_score": _cat_scores.get(t, 0.0),
                })
                _dd_added += 1
        if _dd_added:
            print(f"    🎯 [deep_dive] priority=0 銘柄を追加: "
                  f"{_dd_added}社 {_deep_dive_tickers[:_dd_added]}")

    # ── 優先度3: BQから未取得銘柄を動的取得 ─────────────────
    # 予算上限チェック（rankings 2コール + バッファ 5コール を除いた枠）
    effective_budget = min(budget - 7, per_day)
    remaining_slots = effective_budget - len(queue)

    if bq_client and remaining_slots > 0:
        try:
            # edinet_bq_client.pyのメソッドを利用して未取得銘柄を取得
            unfetched_df = bq_client.get_unfetched_docs(limit=remaining_slots)
            
            # DataFrameとして返ってくる想定で処理
            if hasattr(unfetched_df, "empty") and not unfetched_df.empty:
                for _, row in unfetched_df.iterrows():
                    # カラム名が securities_code か company_code のどちらかであると推測
                    sec_code = str(row.get("securities_code", row.get("company_code", "")))
                    if sec_code:
                        ticker = f"{sec_code}.T" if not sec_code.endswith(".T") else sec_code
                        _add(ticker, 3, "bq_unfetched")
            # 辞書のリストとして返ってきた場合のフォールバック（get_unfetched_docsの重複定義への対応）
            elif isinstance(unfetched_df, list):
                for doc in unfetched_df:
                    sec_code = str(doc.get("company_code", doc.get("securities_code", "")))
                    if sec_code:
                        ticker = f"{sec_code}.T" if not sec_code.endswith(".T") else sec_code
                        _add(ticker, 3, "bq_unfetched")
                        
        except Exception as e:
            print(f"    ⚠️ BQからのunfetched取得失敗: {str(e)[:60]}")

    # ── [FE-7] catalyst_proximity_score で二次ソート ────────
    # 優先度（priority）が同じ場合は catalyst_proximity_score が高い方を先に配置
    _cat_scores = _get_catalyst_priority_tickers(cfg)
    queue = sorted(
        queue,
        key=lambda q: (
            q["priority"],
            -_cat_scores.get(q["ticker"], 0.0),
        )
    )[:effective_budget]

    print(f"    📋 取得キュー: {len(queue)}社 "
          f"（deep_dive:{sum(1 for q in queue if q['priority']==0)} / "
          f"watchlist:{sum(1 for q in queue if q['priority']==1)} / "
          f"screener:{sum(1 for q in queue if q['priority']==2)} / "
          f"rotation:{sum(1 for q in queue if q['priority']==3)}）")
    return queue

def _days_since(date_str: str | None) -> int:
    """'YYYY-MM-DD' 文字列から今日までの経過日数を返す"""
    if not date_str:
        return 999
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        return (datetime.now().date() - d).days
    except Exception:
        return 999

def _get_catalyst_priority_tickers(cfg=None) -> dict[str, float]:
    """
    catalyst_calendar.json から proximity_score が高い銘柄を返す。

    edinet_rotation_manager の優先キューで Catalyst 接近銘柄を
    上位に配置するために使用する。

    Returns:
        {ticker: proximity_score} — proximity_score 降順
        proximity_score ≥ 0.6（30日以内）の銘柄のみ返す
    """
    try:
        from catalyst_calendar import load_catalyst_calendar as _lcc
        cat_data    = _lcc(cfg)
        if not cat_data:
            return {}

        ticker_events = cat_data.get("ticker_events", {})
        result: dict[str, float] = {}

        for ticker, events in ticker_events.items():
            if not events:
                continue
            max_prox = max(
                (float(e.get("proximity_score", 0.0)) for e in events),
                default=0.0,
            )
            if max_prox >= 0.6:   # 30日以内の閾値
                result[ticker] = max_prox

        # proximity_score 降順でソート
        return dict(
            sorted(result.items(), key=lambda x: -x[1])
        )
    except Exception:
        return {}

# ==============================================================
# BLOCK 4: edinet_client 動的ロード
# ==============================================================

def _load_edinet_client(cfg):
    """edinet_client.py を動的 import する"""
    import importlib.util
    from pathlib import Path  # Pathが未定義なら追加してください

    # 候補となるパスのリスト（優先順位順）
    candidates = [
        Path(getattr(cfg, "DRIVE_BASE", "")) / "edinet_client.py",
        Path(__file__).parent / "edinet_client.py",
        Path("/content/edinet_client.py"),
    ]
    
    for path in candidates:
        if path.exists():
            spec = importlib.util.spec_from_file_location("edinet_client", path)
            
            # 論理的チェック：spec と loader が確実に存在する場合のみ処理
            if spec is not None and spec.loader is not None:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                return module
                
    print("    ⚠️  edinet_client.py が見つかりません")
    return None

# ==============================================================
# BLOCK 5: 1銘柄取得処理
# ==============================================================

def _fetch_one_ticker(ticker: str, cfg, edinet_client,
                      cache_dir: Path) -> dict:
    """
    1銘柄の analysis データを取得してDBに保存する。

    Returns:
        dict: {"ok": bool, "ticker": str, "edinet_code": str,
               "name": str, "data": dict, "error": str}
    """
    api_key = getattr(cfg, "EDINETDB_API_KEY", "")
    cache_days = getattr(cfg, "EDINET_ROTATION_CACHE_DAYS", 30)

    sec_code = ticker.replace(".T", "").replace(".OS", "").strip()

    # ── Step1: 企業検索（認証不要・無料） ────────────────────
    company = edinet_client.search_company(
        sec_code, api_key, cache_dir, cache_days=365  # 企業情報は1年キャッシュ
    )
    if not company:
        return {"ok": False, "ticker": ticker,
                "error": "企業が見つかりません"}

    edinet_code  = company.get("edinet_code", "")
    company_name = company.get("name", ticker)

    if not edinet_code:
        return {"ok": False, "ticker": ticker,
                "error": "EDINETコードなし"}

    time.sleep(0.8)  # レート制限対策

    # ── Step2: AI分析取得（1コール） ─────────────────────────
    # rotation managerでは edinet_client のキャッシュを使わず
    # 独自DBに保存するため、cache_dir=None で直接APIを叩く
    analysis = edinet_client.get_analysis(edinet_code, api_key)
    if not analysis:
        return {"ok": False, "ticker": ticker,
                "edinet_code": edinet_code,
                "name": company_name,
                "error": "analysis取得失敗"}

    # 独自DBに保存
    _save_analysis(cfg, edinet_code, analysis)

    return {
        "ok":           True,
        "ticker":       ticker,
        "edinet_code":  edinet_code,
        "name":         company_name,
        "data":         analysis,
        "error":        "",
    }


# ==============================================================
# BLOCK 6: meta.json 更新
# ==============================================================

def _update_meta(meta: dict, result: dict, priority: int, reason: str):
    """
    1銘柄の取得結果を meta に反映する。
    成功・失敗どちらでも記録する（失敗は next_due を明日に設定）。
    """
    ticker     = result["ticker"]
    today      = datetime.now().strftime("%Y-%m-%d")
    cache_days = 30  # デフォルト

    entry = meta.get(ticker, {
        "sec_code":    ticker.replace(".T","").replace(".OS",""),
        "edinet_code": "",
        "name":        ticker,
        "market":      "Prime",
        "fetch_count": 0,
        "priority":    priority,
    })

    entry["priority"]    = min(entry.get("priority", 9), priority)
    entry["fetch_count"] = entry.get("fetch_count", 0) + 1
    entry["last_reason"] = reason

    if result["ok"]:
        entry["edinet_code"]  = result.get("edinet_code", entry["edinet_code"])
        entry["name"]         = result.get("name", entry["name"])
        entry["last_fetched"] = today
        entry["last_status"]  = "ok"
        next_due = (datetime.now().date()
                    + timedelta(days=cache_days)).strftime("%Y-%m-%d")
        entry["next_due"] = next_due
    else:
        entry["last_status"]  = f"error: {result.get('error','')}"
        # 失敗は翌日リトライ
        entry["next_due"] = (datetime.now().date()
                             + timedelta(days=1)).strftime("%Y-%m-%d")

    meta[ticker] = entry


# ==============================================================
# BLOCK 7: rankings 取得（2コール）
# ==============================================================

def _fetch_rankings(cfg, edinet_client) -> dict:
    """
    EdinetDB の /rankings エンドポイントを呼び出す。
    edinet_client に get_rankings() が実装されていれば使用し、
    なければスキップする（将来拡張用）。

    Returns:
        dict: {"roe": [...], "credit_score": [...]} or {}
    """
    if not hasattr(edinet_client, "get_rankings"):
        # v2.0には未実装のため静かにスキップ
        return {}

    api_key = getattr(cfg, "EDINETDB_API_KEY", "")

    rankings = {}
    for metric in ["roe", "credit_score"]:
        try:
            result = edinet_client.get_rankings(metric, api_key)
            if result:
                rankings[metric] = result
                print(f"    📊 rankings/{metric}: {len(result)}社取得")
            time.sleep(0.5)
        except Exception as e:
            print(f"    ⚠️  rankings/{metric} 取得失敗: {str(e)[:60]}")

    return rankings


# ==============================================================
# BLOCK 8: 実行サマリーレポート生成
# ==============================================================

def _build_summary_report(fetch_results: list, meta: dict,
                           rankings: dict, cfg) -> str:
    """当日のローテーション実行サマリーをMarkdown形式で生成する"""
    today    = datetime.now().strftime("%Y-%m-%d")
    ok_list  = [r for r in fetch_results if r["ok"]]
    err_list = [r for r in fetch_results if not r["ok"]]

    lines = [
        f"# EdinetDB ローテーション実行レポート",
        f"**{today}** | 取得: {len(ok_list)}社成功 / {len(err_list)}社失敗",
        "",
        "## 取得成功銘柄",
        "",
        "| 銘柄 | 社名 | 信用スコア | レーティング | 優先度 |",
        "|-----|------|-----------|------------|-------|",
    ]

    for r in sorted(ok_list,
                    key=lambda x: meta.get(x["ticker"],{}).get("priority",9)):
        d       = r.get("data", {})
        score   = d.get("credit_score", "-")
        rating  = d.get("credit_rating", "-")
        prio    = meta.get(r["ticker"], {}).get("priority", 3)
        prio_s  = {1: "★ウォッチ", 2: "▲screener", 3: "通常"}.get(prio, "-")
        lines.append(
            f"| {r['ticker']} | {r.get('name','')[:12]} "
            f"| {score} | {rating} | {prio_s} |"
        )

    if err_list:
        lines += ["", "## 取得失敗銘柄", ""]
        for r in err_list:
            lines.append(f"- {r['ticker']}: {r.get('error','')}")

    # DB全体のカバレッジ
    total_tracked = len(meta)
    fetched_ever  = sum(1 for v in meta.values()
                        if v.get("last_fetched"))
    lines += [
        "",
        "## 蓄積DB カバレッジ",
        "",
        f"- 管理銘柄数: {total_tracked}社",
        f"- 取得済み: {fetched_ever}社",
        f"- カバレッジ: {fetched_ever/total_tracked*100:.1f}%" if total_tracked else "- カバレッジ: 0%",
    ]

    lines += ["", "---",
              f"*自動生成: edinet_rotation_manager.py v1.0 | {today}*"]
    return "\n".join(lines)


# ==============================================================
# BLOCK 9: メインエントリポイント
# ==============================================================

# 「辞書、または（|） None」を許可する最新の書き方
def run_edinet_rotation(cfg, results: dict | None = None) -> dict:
    """
    stock_analyzer_main.py の run_all_tasks() から呼ばれる。

    前提:
      - edinet_client.py がアクセス可能であること
      - EDINETDB_API_KEY が config に設定されていること
      - ENABLE_EDINET_ROTATION = True であること

    Returns:
        dict（results["edinet_rotation"] に格納される）:
          {
            "fetch_results":  [dict, ...],  取得試行一覧
            "ok_count":       int,
            "error_count":    int,
            "db_coverage":    float,        DB全体のカバレッジ率
            "rankings":       dict,
            "report_path":    str,
            "skipped":        bool,
          }
    """
    if not getattr(cfg, "ENABLE_EDINET_ROTATION", False):
        print("  ⏩ EdinetDB ローテーション: "
              "スキップ（ENABLE_EDINET_ROTATION=False）")
        return {"skipped": True, "ok_count": 0, "error_count": 0}

    api_key = getattr(cfg, "EDINETDB_API_KEY", "")
    if not api_key:
        print("  ⏩ EdinetDB ローテーション: "
              "スキップ（APIキー未設定）")
        return {"skipped": True, "ok_count": 0, "error_count": 0}

    _dd_count = len(getattr(cfg, "EDINET_DEEP_DIVE_TICKERS", []) or [])
    _dd_label = f" / deep_dive={_dd_count}社" if _dd_count else ""
    print(f"\n  🔄 EdinetDB ローテーション開始...{_dd_label}")
    start_time = time.time()

    # edinet_client ロード
    edinet_client = _load_edinet_client(cfg)
    if not edinet_client:
        return {"skipped": True, "ok_count": 0,
                "error_count": 0, "error": "edinet_client not found"}

    # キャッシュディレクトリ（edinet_clientの通常キャッシュ用）
    data_dir  = Path(getattr(cfg, "DATA_DIR", "/tmp"))
    cache_dir = data_dir / "edinet_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    # screener通過候補を results から自動取得（当日screener結果）
    screener_candidates = []
    if results:
        # 日本株screenerの上位銘柄（sector agent結果から）
        # main の書き込みキーは "sectors"（"sector_results" はフォールバック）
        for sector_result in results.get("sectors", results.get("sector_results", {})).values():
            top_cos = sector_result.get("top_companies", [])
            for co in top_cos[:2]:
                t = co.get("ticker", "")
                if t.endswith(".T") and t not in screener_candidates:
                    screener_candidates.append(t)
        # config に動的に注入
        if screener_candidates:
            existing = getattr(cfg, "EDINET_SCREENER_CANDIDATES", [])
            # 前日分 + 当日分をマージして重複除去
            merged = list(dict.fromkeys(screener_candidates + existing))
            cfg.EDINET_SCREENER_CANDIDATES = merged[:8]
            print(f"    📥 screener通過候補を注入: "
                  f"{len(cfg.EDINET_SCREENER_CANDIDATES)}社")

    # meta 読み込み
    meta = load_meta(cfg)
    print(f"    📚 既存DB: {len(meta)}社管理中")

    # 取得キュー構築
    queue = _build_fetch_queue(cfg, meta, results)
    if not queue:
        print("  ✅ 本日の取得対象なし（全銘柄が有効期間内）")
        return {
            "skipped":      False,
            "fetch_results": [],
            "ok_count":     0,
            "error_count":  0,
            "db_coverage":  _calc_coverage(meta),
            "rankings":     {},
            "report_path":  "",
        }

    # ── 銘柄取得ループ ────────────────────────────────────────
    fetch_results = []
    budget_used   = 0
    budget_max    = getattr(cfg, "EDINET_DAILY_BUDGET", 80)
    retry_budget  = getattr(cfg, "EDINET_RETRY_BUDGET", 5)

    for item in queue:
        if budget_used >= budget_max - retry_budget:
            print(f"    ⚠️  バジェット上限 {budget_max} に近づきました "
                  f"（使用: {budget_used}）")
            break

        ticker   = item["ticker"]
        priority = item["priority"]
        reason   = item["reason"]

        print(f"    [{budget_used+1}/{len(queue)}] {ticker} "
              f"（{reason}）...")

        try:
            result = _fetch_one_ticker(
                ticker, cfg, edinet_client, cache_dir)
            budget_used += 1  # analysis 1コール

            fetch_results.append(result)
            _update_meta(meta, result, priority, reason)

            if result["ok"]:
                d      = result.get("data", {})
                score  = d.get("credit_score", "-")
                rating = d.get("credit_rating", "-")
                print(f"      ✅ {result.get('name',ticker)}: "
                      f"信用スコア {score}/100 ({rating})")
            else:
                print(f"      ❌ {ticker}: {result.get('error','')}")

            # レート制限対策: 1社ごとに1秒待機
            time.sleep(1.0)

        except Exception as e:
            err_msg = str(e)[:80]
            if "429" in err_msg or "rate" in err_msg.lower():
                print(f"    🚫 レート制限に達しました。取得を中断します。")
                fetch_results.append({
                    "ok": False, "ticker": ticker,
                    "error": f"429: {err_msg}"
                })
                _update_meta(meta, {"ok": False, "ticker": ticker,
                                     "error": "429"}, priority, reason)
                break
            print(f"      ⚠️  {ticker} 予期せぬエラー: {err_msg}")
            fetch_results.append({
                "ok": False, "ticker": ticker, "error": err_msg
            })
            _update_meta(meta, {"ok": False, "ticker": ticker,
                                  "error": err_msg}, priority, reason)

    # ── rankings 取得（2コール・残バジェットがあれば） ─────────
    rankings = {}
    if budget_used < budget_max - 2:
        print("    📊 rankings 取得中...")
        rankings = _fetch_rankings(cfg, edinet_client)
        budget_used += len(rankings) * 1  # 取得成功数分を記録

    # ── meta 保存 ────────────────────────────────────────────
    save_meta(cfg, meta)

    # ── レポート保存 ─────────────────────────────────────────
    ok_cnt  = sum(1 for r in fetch_results if r["ok"])
    err_cnt = sum(1 for r in fetch_results if not r["ok"])
    report_path = None
    try:
        today   = datetime.now().strftime("%Y-%m-%d")
        out_dir = Path(cfg.INSIGHT_DIR) / "edinet_rotation"
        out_dir.mkdir(parents=True, exist_ok=True)
        report  = _build_summary_report(fetch_results, meta, rankings, cfg)
        report_path = out_dir / f"rotation_{today}.md"
        report_path.write_text(report, encoding="utf-8")
    except Exception as e:
        print(f"    ⚠️  レポート保存失敗: {str(e)[:60]}")

    elapsed   = time.time() - start_time
    coverage  = _calc_coverage(meta)
    print(f"\n  🏁 EdinetDB ローテーション完了")
    print(f"     取得成功: {ok_cnt}社 / 失敗: {err_cnt}社")
    print(f"     APIコール使用: {budget_used}/{budget_max}")
    print(f"     DBカバレッジ: {coverage:.1f}%")
    print(f"     所要時間: {elapsed:.0f}秒")
    # 政府EDINET XBRL直接取得はW4で停止。BQ更新はedinetdb_ingestorが担う。
    results["edinet_xbrl_fetch"] = {
        "skipped": True, "reason": "retired: EdinetDB-only ingestion"
    }
    return {
        "skipped":       False,
        "fetch_results": fetch_results,
        "ok_count":      ok_cnt,
        "error_count":   err_cnt,
        "budget_used":   budget_used,
        "db_coverage":   coverage,
        "db_total":      len(meta),
        "rankings":      rankings,
        "report_path":   str(report_path) if report_path else "",
        "elapsed_sec":   round(elapsed, 1),
        "timestamp":     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _calc_coverage(meta: dict) -> float:
    """DBのカバレッジ率（取得済み / 管理銘柄）を返す"""
    if not meta:
        return 0.0
    fetched = sum(1 for v in meta.values() if v.get("last_fetched"))
    return fetched / len(meta) * 100


# ==============================================================
# BLOCK 10: screener 連携ユーティリティ
# ==============================================================

def enrich_screener_result(result: dict, db: dict) -> dict:
    """
    screener の1銘柄結果辞書に EdinetDB の信用スコアを追記する。
    base_agent.fetch_financials() の結果と同じ形式で返す。

    Args:
        result : {"ticker": "8306.T", "per": ..., ...}
        db     : load_db() の戻り値

    Returns:
        result に以下が追記された dict:
          credit_score, credit_rating, risk_factors,
          risk_summary, business_summary
    """
    ticker   = result.get("ticker", "")
    analysis = lookup(db, ticker)
    if analysis:
        result["credit_score"]    = analysis.get("credit_score")
        result["credit_rating"]   = analysis.get("credit_rating")
        result["risk_factors"]    = analysis.get("risk_factors", [])
        result["risk_summary"]    = analysis.get("risk_summary", "")
        result["business_summary"]= analysis.get("business_summary", "")
        result["edinet_enriched"] = True
    else:
        result["edinet_enriched"] = False
    return result


def get_db_stats(cfg) -> dict:
    """
    蓄積DBの統計情報を返す（デバッグ・モニタリング用）。

    Returns:
        dict:
          total, fetched, coverage_pct,
          watchlist_fetched, oldest_fetch, newest_fetch
    """
    meta = load_meta(cfg)
    if not meta:
        return {"total": 0, "fetched": 0, "coverage_pct": 0.0}

    fetched_entries = [v for v in meta.values() if v.get("last_fetched")]
    dates           = [v["last_fetched"] for v in fetched_entries]

    return {
        "total":              len(meta),
        "fetched":            len(fetched_entries),
        "coverage_pct":       len(fetched_entries) / len(meta) * 100,
        "watchlist_fetched":  sum(1 for v in fetched_entries
                                  if v.get("priority") == 1),
        "screener_fetched":   sum(1 for v in fetched_entries
                                  if v.get("priority") == 2),
        "oldest_fetch":       min(dates) if dates else None,
        "newest_fetch":       max(dates) if dates else None,
    }


# ==============================================================
# 単体テスト
# ==============================================================

if __name__ == "__main__":
    import importlib.util, sys
    from pathlib import Path

    cfg = None
    for p in ["/content/config.py", str(Path(__file__).parent / "config.py")]:
        path_obj = Path(p)
        if path_obj.exists():
            spec = importlib.util.spec_from_file_location("config", p)
            # spec と loader の存在を明示的にチェック（型エラーの解消）
            if spec is not None and spec.loader is not None:
                cfg = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(cfg)
                break
    
    # cfg が None のままなら終了（ガードを明確化）
    if cfg is None:
        print("config.py が見つかりません")
        sys.exit(1)

    print("=== edinet_rotation_manager.py v1.0 単体テスト ===")
    # getattr を使っているのは、設定がない場合の安全策として非常に良い判断です
    print(f"ENABLE_EDINET_ROTATION: "
          f"{getattr(cfg, 'ENABLE_EDINET_ROTATION', False)}")
    print(f"EDINET_ROTATION_PER_DAY: "
          f"{getattr(cfg, 'EDINET_ROTATION_PER_DAY', 33)}")
    print(f"EDINET_DAILY_BUDGET: "
          f"{getattr(cfg, 'EDINET_DAILY_BUDGET', 80)}")

    # DB統計確認
    stats = get_db_stats(cfg)
    print(f"\n  DBカバレッジ: {stats.get('coverage_pct', 0):.1f}% "
          f"({stats.get('fetched', 0)}/{stats.get('total', 0)}社)")

    # ローテーション実行
    result = run_edinet_rotation(cfg, {})
    print(f"\n  取得成功: {result.get('ok_count', 0)}社")
    print(f"  取得失敗: {result.get('error_count', 0)}社")
    print(f"  APIコール: {result.get('budget_used', 0)}")
    print(f"  DBカバレッジ: {result.get('db_coverage', 0):.1f}%")
