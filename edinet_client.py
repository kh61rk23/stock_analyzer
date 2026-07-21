# ==============================================================
# edinet_client.py  v3.0
# ==============================================================
# 【v2.0 → v3.0 変更点】
#   ① 日次予算管理: 1日80回上限を超えたら自動停止
#   ② 日次キャッシュ: 同日中は API を叩かずキャッシュを使用
#      （v2.0の100日キャッシュとは別に「今日は取得済み」フラグを管理）
#   ③ /financials エンドポイント追加: 6年間の財務トレンドを取得
#   ④ 政府EDINET API統合: 有報提出日・書類メタデータを取得（無制限）
#   ⑤ get_full_company_data() 追加: search + analysis + financials を
#      1関数で一括取得し、予算消費を最小化
#
# 【API使用量の設計】
#   screener上位20社  × 2コール(analysis+financials) = 40コール
#   セクター代表10社  × 2コール                     = 20コール
#   合計: 60コール/日 → 80上限に対して余裕あり
#
# 【日次キャッシュ設計】
#   DATA_DIR/edinet_cache/daily_YYYY-MM-DD.json
#   {
#     "date": "2026-03-09",
#     "calls_used": 45,
#     "companies": {
#       "E03518": { "analysis": {...}, "financials": {...}, "gov_meta": {...} }
#     }
#   }
#   → 同日中に同じEDINETコードへの重複リクエストをゼロにする
#
# 【政府EDINET API】
#   URL: https://disclosure.edinet-fsa.go.jp/api/v2/
#   認証: Ocp-Apim-Subscription-Key ヘッダー
#   用途: 有報提出日・書類ID一覧の取得（ドキュメントDLは行わない）
#
# 【依存】config.py / requests / json（標準ライブラリ）
# 【呼出】from edinet_client import get_full_company_data, get_sector_data
# ==============================================================

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

EDINETDB_BASE = "https://edinetdb.jp/v1"
GOV_EDINET_BASE = "https://disclosure.edinet-fsa.go.jp/api/v2"

# 1日あたりの最大APIコール数（民間EdinetDB）
DAILY_BUDGET = 80


# ==============================================================
# BLOCK 1: 日次予算管理・日次キャッシュ
# ==============================================================

def _get_daily_cache_path(cache_dir: Path) -> Path:
    today = datetime.now().strftime("%Y-%m-%d")
    return cache_dir / f"daily_{today}.json"


def _load_daily_cache(cache_dir: Path) -> dict:
    """今日の日次キャッシュを読み込む。存在しなければ初期値を返す。"""
    path = _get_daily_cache_path(cache_dir)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "calls_used": 0,
        "companies": {},
    }


def _save_daily_cache(cache_dir: Path, cache: dict):
    """日次キャッシュを保存する。"""
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = _get_daily_cache_path(cache_dir)
        path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _budget_ok(cache: dict) -> bool:
    """予算残があるか確認する。"""
    return cache.get("calls_used", 0) < DAILY_BUDGET


def _increment_budget(cache: dict, n: int = 1):
    """コール数カウンタをインクリメントする。"""
    cache["calls_used"] = cache.get("calls_used", 0) + n


# ==============================================================
# BLOCK 2: 共通ユーティリティ
# ==============================================================

def _headers_db(api_key: str) -> dict:
    return {"X-API-Key": api_key, "User-Agent": "StockAnalyzer/3.0"}


def _headers_gov(api_key: str) -> dict:
    return {
        "Ocp-Apim-Subscription-Key": api_key,
        "User-Agent": "StockAnalyzer/3.0",
    }


def _long_cache_path(cache_dir: Path, key: str) -> Path:
    """長期キャッシュ（100日）のパス（企業検索結果の永続化用）"""
    safe = key.replace("/", "_").replace(".", "_")
    return cache_dir / f"search_{safe}.json"


def _load_long_cache(path: Path, days: int = 100) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        if datetime.now() - mtime > timedelta(days=days):
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


# ==============================================================
# BLOCK 3: 企業検索（証券コード → EDINETコード）
# ==============================================================

def search_company(sec_code: str, api_key: str,
                   cache_dir: Optional[Path] = None,
                   cache_days: int = 100) -> Optional[dict]:
    """
    証券コード（例: "8306"）→ EdinetDB 企業情報取得。
    検索APIは認証不要で予算消費しない（search はノーカウント）。
    結果は長期キャッシュ（100日）に保存する。
    """
    # add by gemini 20260311
    sec_code = str(sec_code).replace(".T", "").replace(".OS", "").strip()
    # add by gemini 20260311
    try:
        import requests
    except ImportError:
        return None

    if cache_dir:
        cp = _long_cache_path(cache_dir, f"co_{sec_code}")
        cached = _load_long_cache(cp, cache_days)
        if cached:
            return cached

    headers = _headers_db(api_key) if api_key else {"User-Agent": "StockAnalyzer/3.0"}
    try:
        resp = requests.get(
            f"{EDINETDB_BASE}/search",
            params={"q": sec_code},
            headers=headers,
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        data = resp.json().get("data", [])
        for company in data:
            raw = str(company.get("sec_code", ""))
            code4 = raw[:4] if len(raw) >= 4 else raw
            if code4 == sec_code:
                if cache_dir:
                    cp = _long_cache_path(cache_dir, f"co_{sec_code}")
                    cp.parent.mkdir(parents=True, exist_ok=True)
                    cp.write_text(json.dumps(company, ensure_ascii=False, indent=2), encoding="utf-8")
                return company
        return None
    except Exception as e:
        print(f"      ⚠️  企業検索エラー ({sec_code}): {str(e)[:60]}")
        return None


# ==============================================================
# BLOCK 4: EdinetDB /analysis 取得
# ==============================================================

def _fetch_analysis(edinet_code: str, api_key: str) -> Optional[dict]:
    """
    EdinetDB /analysis エンドポイントから AI 分析データを取得する。
    日次キャッシュ経由で呼ばれるため、ここでは直接 HTTP リクエストのみ行う。
    """
    try:
        import requests
    except ImportError:
        return None

    try:
        resp = requests.get(
            f"{EDINETDB_BASE}/companies/{edinet_code}/analysis",
            headers=_headers_db(api_key),
            timeout=20,
        )
        if resp.status_code == 429:
            print("      ⚠️  EdinetDB レート制限（100回/日）に達しました")
            return None
        if resp.status_code != 200:
            return None

        raw = resp.json().get("data", {})
        risk_text = raw.get("risk_factors", "") or raw.get("risks", "") or ""

        return {
            "credit_score":      raw.get("credit_score", 0),
            "credit_rating":     raw.get("credit_rating", ""),
            "risk_summary":      (raw.get("risk_summary", "") or "")[:400],
            "business_summary":  (raw.get("business_summary", "") or "")[:400],
            "risk_factors":      _parse_risk_items(risk_text, max_items=7),
            "strengths":         (raw.get("strengths", "") or "")[:300],
            "weaknesses":        (raw.get("weaknesses", "") or "")[:300],
        }
    except Exception as e:
        print(f"      ⚠️  analysis 取得エラー ({edinet_code}): {str(e)[:60]}")
        return None

def _calc_accrual_ratio(records: list) -> Optional[float]:
    """最新年度のアクルーアル比率を計算する"""
    if not records:
        return None
    latest = records[-1]  # 年度順ソート済みなので末尾が最新
    ni    = latest.get("net_income")
    cf_op = latest.get("cf_operating")
    ta    = latest.get("total_assets")
    if ni is None or cf_op is None or ta is None or ta == 0:
        return None
    return round((ni - cf_op) / ta, 4)

# ==============================================================
# BLOCK 5: EdinetDB /financials 取得（新規）
# ==============================================================

def _fetch_financials(edinet_code: str, api_key: str) -> Optional[dict]:
    """
    EdinetDB /financials エンドポイントから6年間の財務トレンドを取得する。
    yfinance では取得しにくい日本株の詳細財務データが得られる。

    Returns:
        dict:
          {
            "years":          [2019, 2020, ..., 2024],
            "revenue":        [int, ...],   # 売上高（円）
            "operating_income": [int, ...], # 営業利益
            "net_income":     [int, ...],   # 純利益
            "total_assets":   [int, ...],   # 総資産
            "equity":         [int, ...],   # 純資産
            "eps":            [float, ...], # EPS
            "dividend_per_share": [float, ...], # 1株配当
            "revenue_cagr":   float,        # 売上CAGR（3年）
            "op_income_cagr": float,        # 営業利益CAGR（3年）
          }
    """
    try:
        import requests
    except ImportError:
        return None

    try:
        resp = requests.get(
            f"{EDINETDB_BASE}/companies/{edinet_code}/financials",
            headers=_headers_db(api_key),
            timeout=20,
        )
        if resp.status_code == 429:
            print("      ⚠️  EdinetDB レート制限（100回/日）に達しました")
            return None
        if resp.status_code != 200:
            return None

        raw = resp.json().get("data", {})
        if isinstance(raw, list):
            records = raw
        else:
            records = raw.get("records", raw.get("financials", []))

        if not records:
            return None

        # 年度順にソート（古い順）
        records = sorted(records, key=lambda x: x.get("fiscal_year", 0))

        years     = [r.get("fiscal_year") for r in records]
        revenue   = [r.get("revenue") or r.get("net_sales") for r in records]
        op_income = [r.get("operating_income") or r.get("operating_profit") for r in records]
        net_inc   = [r.get("net_income") or r.get("profit") for r in records]
        t_assets  = [r.get("total_assets") for r in records]
        equity    = [r.get("equity") or r.get("net_assets") for r in records]
        eps_list  = [r.get("eps") or r.get("earnings_per_share") for r in records]
        dps_list  = [r.get("dividend_per_share") or r.get("dps") for r in records]

        # CAGR計算（最新3年）
        def _cagr(values: list, years_n: int = 3) -> Optional[float]:
            vals = [v for v in values if v and v > 0]
            if len(vals) < years_n + 1:
                return None
            start, end = vals[-(years_n + 1)], vals[-1]
            try:
                return (end / start) ** (1 / years_n) - 1
            except Exception:
                return None

        return {
            "years":              years,
            "revenue":            revenue,
            "operating_income":   op_income,
            "net_income":         net_inc,
            "total_assets":       t_assets,
            "equity":             equity,
            "eps":                eps_list,
            "dividend_per_share": dps_list,
            "revenue_cagr":       _cagr(revenue),
            "op_income_cagr":     _cagr(op_income),
            "gross_profit":    [r.get("gross_profit")      for r in records],
            "cf_operating":    [r.get("cf_operating")       for r in records],
            "cf_investing":    [r.get("cf_investing")       for r in records],
            "cf_financing":    [r.get("cf_financing")       for r in records],
            "capex":           [r.get("capex")              for r in records],
            "cash":            [r.get("cash")               for r in records],
            "depreciation":    [r.get("depreciation")       for r in records],
            "rnd_expenses":    [r.get("rnd_expenses")       for r in records],
            "goodwill":        [r.get("goodwill")           for r in records],
            "trade_receivables":[r.get("trade_receivables") for r in records],
            "inventories":     [r.get("inventories")        for r in records],
            "retained_earnings":[r.get("retained_earnings") for r in records],

            # アクルーアル計算（EdinetDB単体で完結）
            # accrual_ratio = (純利益 - 営業CF) / 総資産
            "accrual_ratio": _calc_accrual_ratio(records),
        }
    except Exception as e:
        print(f"      ⚠️  financials 取得エラー ({edinet_code}): {str(e)[:60]}")
        return None


# ==============================================================
# BLOCK 6: 政府EDINET API（有報メタデータ取得）
# ==============================================================

def get_gov_edinet_filings(sec_code: str, gov_api_key: str,
                            cache_dir: Optional[Path] = None) -> Optional[dict]:
    """
    金融庁 EDINET API から指定企業の直近有価証券報告書メタデータを取得する。
    ドキュメントのダウンロードは行わず、提出日・書類IDのみ取得。

    Args:
        sec_code   : 4桁証券コード（"8306"）
        gov_api_key: 金融庁EDINET サブスクリプションキー
        cache_dir  : キャッシュ保存先

    Returns:
        dict | None:
          {
            "company_name": str,
            "edinet_code":  str,         # 金融庁EDINETコード（E0xxxx）
            "latest_doc_id": str,        # 最新有報の書類ID
            "submit_date":  str,         # 最新有報の提出日 YYYY-MM-DD
            "period_end":   str,         # 対象期間終了日
            "doc_count":    int,         # 過去5年の有報提出件数
          }
    """
    try:
        import requests
    except ImportError:
        return None

    if not gov_api_key:
        return None

    # 長期キャッシュ確認（30日）
    if cache_dir:
        cp = _long_cache_path(cache_dir, f"gov_{sec_code}")
        cached = _load_long_cache(cp, days=30)
        if cached:
            return cached

    headers = _headers_gov(gov_api_key)

    # 過去365日分の書類リストを検索（有価証券報告書 type=120）
    from datetime import date, timedelta as td
    end_date   = date.today()
    start_date = end_date - td(days=365)

    filings = []
    try:
        # 日ごとに叩くのではなく、全書類の一括取得を行う
        # ただし1日1000件上限のため、最新30日で有報を探す
        for delta in range(30):
            check_date = (end_date - td(days=delta)).strftime("%Y-%m-%d")
            resp = requests.get(
                f"{GOV_EDINET_BASE}/documents.json",
                params={"date": check_date, "type": 2},  # type=2: 定期報告書
                headers=headers,
                timeout=15,
            )
            if resp.status_code != 200:
                continue
            docs = resp.json().get("results", [])
            for doc in docs:
                # 証券コードで絞り込み
                doc_sec = str(doc.get("secCode", ""))[:4]
                if doc_sec == sec_code and doc.get("docTypeCode") == "120":  # 120=有価証券報告書
                    filings.append({
                        "doc_id":      doc.get("docID"),
                        "submit_date": doc.get("submitDateTime", "")[:10],
                        "period_end":  doc.get("periodEnd", ""),
                        "company_name": doc.get("filerName", ""),
                        "edinet_code": doc.get("edinetCode", ""),
                    })
            if filings:
                break  # 見つかったら終了
            time.sleep(0.2)

        if not filings:
            return None

        latest = filings[0]
        result = {
            "company_name":  latest["company_name"],
            "edinet_code":   latest["edinet_code"],
            "latest_doc_id": latest["doc_id"],
            "submit_date":   latest["submit_date"],
            "period_end":    latest["period_end"],
            "doc_count":     len(filings),
        }

        if cache_dir:
            cp = _long_cache_path(cache_dir, f"gov_{sec_code}")
            cp.parent.mkdir(parents=True, exist_ok=True)
            cp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

        return result

    except Exception as e:
        print(f"      ⚠️  政府EDINET エラー ({sec_code}): {str(e)[:60]}")
        return None


# ==============================================================
# BLOCK 7: 一括取得（メインエントリポイント）
# ==============================================================

def get_full_company_data(ticker: str, cfg,
                           daily_cache: Optional[dict] = None,
                           cache_dir: Optional[Path] = None) -> dict:
    """
    1銘柄の全データを取得する。
    - EdinetDB: analysis（1コール）+ financials（1コール）= 最大2コール
    - 日次キャッシュ: 同日中は0コール

    Args:
        ticker      : "8306.T" 形式
        cfg         : config モジュール
        daily_cache : _load_daily_cache() で読み込んだ辞書（共有して予算管理）
        cache_dir   : キャッシュディレクトリ

    Returns:
        dict:
          {
            "ticker":       str,
            "edinet_code":  str,
            "company_name": str,
            "analysis":     dict | None,   # credit_score, risk_factors, ...
            "financials":   dict | None,   # 6年間財務トレンド
            "gov_meta":     None,          # 後方互換（W4で直接取得停止）
            "from_cache":   bool,
          }
    """
    db_api_key = getattr(cfg, "EDINETDB_API_KEY", "")
    cache_days  = getattr(cfg, "EDINET_CACHE_DAYS", 100)

    sec_code = ticker.replace(".T", "").replace(".OS", "").strip()

    result = {
        "ticker":      ticker,
        "sec_code":    sec_code,
        "edinet_code": "",
        "company_name": ticker,
        "analysis":    None,
        "financials":  None,
        "gov_meta":    None,
        "from_cache":  False,
        "earnings": None,   # TDNet決算短信（最新1件）
    }

    if not db_api_key:
        return result

    if daily_cache is None:
        daily_cache = {"date": datetime.now().strftime("%Y-%m-%d"), "calls_used": 0, "companies": {}}

    # ── 日次キャッシュ確認 ────────────────────────────────────
    if sec_code in daily_cache.get("companies", {}):
        cached_co = daily_cache["companies"][sec_code]
        result.update({
            "edinet_code":  cached_co.get("edinet_code", ""),
            "company_name": cached_co.get("company_name", ticker),
            "analysis":     cached_co.get("analysis"),
            "financials":   cached_co.get("financials"),
            "gov_meta":     cached_co.get("gov_meta"),
            "from_cache":   True,
        })
        print(f"      💾 {ticker}: 日次キャッシュから取得（本日の API コール節約）")
        return result

    # ── 企業検索（予算カウントなし） ──────────────────────────
    company = search_company(sec_code, db_api_key, cache_dir, cache_days)
    if not company:
        print(f"      ⚠️  {ticker}: EdinetDB に企業が見つかりませんでした")
        return result

    edinet_code   = company.get("edinet_code", "")
    company_name  = company.get("name", ticker)
    result["edinet_code"]  = edinet_code
    result["company_name"] = company_name

    if not edinet_code:
        return result

    print(f"      🏢 {company_name} (EDINET: {edinet_code}) | 本日コール数: {daily_cache.get('calls_used', 0)}/{DAILY_BUDGET}")

    # ── analysis 取得（1コール） ──────────────────────────────
    if _budget_ok(daily_cache):
        analysis = _fetch_analysis(edinet_code, db_api_key)
        _increment_budget(daily_cache)
        if analysis:
            result["analysis"] = analysis
            score  = analysis.get("credit_score", 0)
            rating = analysis.get("credit_rating", "")
            print(f"        📊 信用スコア: {score}/100 ({rating}) | リスク {len(analysis.get('risk_factors', []))}件")
        time.sleep(0.5)
    else:
        print(f"      ⚠️  予算上限({DAILY_BUDGET})到達 - analysis スキップ")

    # ── financials 取得（1コール） ────────────────────────────
    if _budget_ok(daily_cache):
        financials = _fetch_financials(edinet_code, db_api_key)
        _increment_budget(daily_cache)
        if financials:
            result["financials"] = financials
            yrs = financials.get("years", [])
            rev_cagr = financials.get("revenue_cagr")
            cagr_str = f"{rev_cagr*100:+.1f}%/年" if rev_cagr is not None else "N/A"
            print(f"        📈 財務トレンド {len(yrs)}年分 | 売上CAGR: {cagr_str}")
        time.sleep(0.5)
    else:
        print(f"      ⚠️  予算上限({DAILY_BUDGET})到達 - financials スキップ")

    # ── earnings（TDNet決算短信）取得（1コール）────────────────
    if _budget_ok(daily_cache):
        earnings = _fetch_earnings(edinet_code, db_api_key)
        _increment_budget(daily_cache)
        if earnings:
            result["earnings"] = earnings
            print(f"        📣 決算短信: {earnings.get('disclosure_date','N/A')} 開示")
        time.sleep(0.5)
    
    # ── 日次キャッシュに保存 ──────────────────────────────────
    if "companies" not in daily_cache:
        daily_cache["companies"] = {}
    daily_cache["companies"][sec_code] = {
        "edinet_code":  edinet_code,
        "company_name": company_name,
        "analysis":     result["analysis"],
        "financials":   result["financials"],
        "gov_meta":     result["gov_meta"],
    }

    return result

def _fetch_earnings(edinet_code: str, api_key: str) -> Optional[dict]:
    """TDNet決算短信データを取得する（最新1件）"""
    try:
        import requests
        resp = requests.get(
            f"{EDINETDB_BASE}/companies/{edinet_code}/earnings",
            headers=_headers_db(api_key),
            params={"limit": 1},
            timeout=20,
        )
        if resp.status_code != 200:
            return None
        data = resp.json().get("data", [])
        if not data or not isinstance(data, list):
            return None
        latest = data[0] if data else None
        if latest is None:
            return None
        return {
            "disclosure_date":  latest.get("disclosure_date"),
            "disclosure_time":  latest.get("disclosure_time"),
            "fiscal_year":      latest.get("fiscal_year"),
            "fiscal_quarter":   latest.get("fiscal_quarter"),
            "revenue":          latest.get("revenue"),         # 百万円
            "operating_income": latest.get("operatingIncome"), # 百万円
            "net_income":       latest.get("netIncome"),       # 百万円
            "revenue_change":   latest.get("revenueChange"),   # 前年比%
            "ni_change":        latest.get("netIncomeChange"), # 前年比%
            "pdf_url":          latest.get("pdfUrl"),
        }
    except Exception as e:
        print(f"      ⚠️  earnings 取得エラー ({edinet_code}): {str(e)[:60]}")
        return None

def get_sector_data(tickers: list, cfg,
                    max_companies: int = 2,
                    cache_dir: Optional[Path] = None) -> dict:
    """
    セクター分析用: 複数銘柄を一括取得。
    日次キャッシュを共有して予算を管理する。

    Returns:
        dict: {
          "ticker_data": {ticker: full_company_data},
          "calls_used":  int,
          "daily_cache": dict,  # 呼び出し元で保存すること
        }
    """
    if cache_dir is None:
        data_dir = getattr(cfg, "DATA_DIR", None)
        if data_dir:
            cache_dir = Path(data_dir) / "edinet_cache"

    daily_cache = _load_daily_cache(cache_dir) if cache_dir else {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "calls_used": 0,
        "companies": {},
    }

    ticker_data = {}
    for ticker in tickers[:max_companies]:
        try:
            data = get_full_company_data(ticker, cfg, daily_cache, cache_dir)
            ticker_data[ticker] = data
        except Exception as e:
            print(f"      ⚠️  {ticker} 取得エラー: {str(e)[:60]}")

    # ── ランキング取得（2コール固定）────────────────────────────
    if _budget_ok(daily_cache) and cache_dir:
        _db_api_key = getattr(cfg, "EDINETDB_API_KEY", "")
        for metric in ["roe", "dividend-yield"]:
            fetch_rankings(metric, _db_api_key, limit=50, cache_dir=cache_dir)
            _increment_budget(daily_cache)
            print(f"    📊 rankings/{metric}: 上位50社取得完了")
            time.sleep(0.3)
    
    if cache_dir:
        _save_daily_cache(cache_dir, daily_cache)

    return {
        "ticker_data": ticker_data,
        "calls_used":  daily_cache.get("calls_used", 0),
        "daily_cache": daily_cache,
    }


# ==============================================================
# BLOCK 8: リスクテキスト整形
# ==============================================================

def _parse_risk_items(risk_text: str, max_items: int = 7) -> list:
    if not risk_text:
        return []
    import re
    patterns = [
        r"(?:①|②|③|④|⑤|⑥|⑦|⑧|⑨|⑩)",
        r"(?:\（[0-9１-９]+\）|\([0-9]+\))",
        r"(?:^[0-9]+[．\.。])",
        r"(?:^[■●▶・])",
    ]
    parts = re.split("|".join(patterns), risk_text, flags=re.MULTILINE)
    items = []
    for part in parts[1:]:
        cleaned = part.strip()
        if len(cleaned) < 10:
            continue
        first = cleaned.split("\n")[0][:120].strip()
        if first:
            items.append(first)
        if len(items) >= max_items:
            break
    if not items:
        lines = [l.strip() for l in risk_text.split("\n") if len(l.strip()) > 15]
        items = [l[:120] for l in lines[:max_items]]
    return items[:max_items]


# ==============================================================
# BLOCK 9: レガシー互換 API（v2.0 呼び出しとの後方互換）
# ==============================================================

# add by gemini20260311
def get_analysis(edinet_code: str, api_key: str) -> Optional[dict]:
    return _fetch_analysis(edinet_code, api_key)

def get_financials(edinet_code: str, api_key: str) -> Optional[dict]:
    return _fetch_financials(edinet_code, api_key)
# add by gemini20260311

def get_risk_factors(ticker: str, cfg) -> list:
    """v2.0 互換: base_agent.py からの既存呼び出しをサポート"""
    data_dir = getattr(cfg, "DATA_DIR", None)
    cache_dir = Path(data_dir) / "edinet_cache" if data_dir else None
    daily_cache = _load_daily_cache(cache_dir) if cache_dir else None

    data = get_full_company_data(ticker, cfg, daily_cache, cache_dir)

    if cache_dir and daily_cache:
        _save_daily_cache(cache_dir, daily_cache)

    analysis = data.get("analysis")
    if not analysis:
        return []

    risk_items = analysis.get("risk_factors", [])
    max_items  = getattr(cfg, "EDINET_MAX_RISK_ITEMS", 7)

    if not risk_items:
        summary = analysis.get("risk_summary", "")
        if summary:
            risk_items = [summary[:120]]

    return risk_items[:max_items]


def get_sector_risk_factors(tickers: list, cfg,
                             max_companies: int = 2) -> dict:
    """v2.0 互換: base_agent.py からの既存呼び出しをサポート"""
    data_dir  = getattr(cfg, "DATA_DIR", None)
    cache_dir = Path(data_dir) / "edinet_cache" if data_dir else None

    result_data = get_sector_data(tickers, cfg, max_companies, cache_dir)

    return {
        ticker: d.get("analysis", {}).get("risk_factors", [])
        for ticker, d in result_data["ticker_data"].items()
        if d.get("analysis", {}).get("risk_factors")
    }


# ==============================================================
# BLOCK 10: Markdown整形ユーティリティ
# ==============================================================

def format_company_report(ticker_data: dict, cfg) -> str:
    """
    get_full_company_data() の結果を Markdown レポートに変換する。
    アラートメールおよび業種レポートへの埋め込み用。
    """
    lines = []
    name  = ticker_data.get("company_name", ticker_data.get("ticker", ""))
    ticker = ticker_data.get("ticker", "")

    # 信用スコア
    analysis = ticker_data.get("analysis")
    if analysis:
        score  = analysis.get("credit_score", 0)
        rating = analysis.get("credit_rating", "")
        rating_icon = {"excellent": "💚", "good": "🟡", "fair": "🟠", "poor": "🔴"}.get(rating, "⚪")
        lines.append(f"**{name}** (`{ticker}`) {rating_icon} 信用スコア: {score}/100")

        biz = analysis.get("business_summary", "")
        if biz:
            lines.append(f"> {biz[:150]}")

        risks = analysis.get("risk_factors", [])
        if risks:
            lines.append("**リスク要因**:")
            for r in risks[:5]:
                lines.append(f"- {r}")

    # 財務トレンド
    fin = ticker_data.get("financials")
    if fin:
        years = fin.get("years", [])
        rev   = fin.get("revenue", [])
        oi    = fin.get("operating_income", [])
        rev_cagr = fin.get("revenue_cagr")
        oi_cagr  = fin.get("op_income_cagr")

        if years and rev:
            rev_str = " → ".join(
                f"{int(v/1e8):,}億" if v else "N/A"
                for v in rev[-3:]
            )
            oi_str = " → ".join(
                f"{int(v/1e8):,}億" if v else "N/A"
                for v in oi[-3:]
            )
            lines.append(f"**売上高** ({years[-3] if len(years)>=3 else ''}〜): {rev_str}")
            lines.append(f"**営業利益** ({years[-3] if len(years)>=3 else ''}〜): {oi_str}")
            if rev_cagr is not None:
                lines.append(f"売上CAGR(3年): {rev_cagr*100:+.1f}% | 営業利益CAGR: {oi_cagr*100:+.1f}%" if oi_cagr else f"売上CAGR(3年): {rev_cagr*100:+.1f}%")

    # 政府EDINET 有報メタ
    gov = ticker_data.get("gov_meta")
    if gov:
        lines.append(f"📋 有報提出: {gov.get('submit_date', 'N/A')} | 対象期末: {gov.get('period_end', 'N/A')}")

    lines.append("")
    return "\n".join(lines)


def format_risk_markdown(sector_name: str, risk_by_ticker: dict,
                          ticker_to_name: dict) -> str:
    """v2.0 互換"""
    if not risk_by_ticker:
        return ""
    lines = ["", "### 有報「事業のリスク」（EdinetDB 自動取得）", ""]
    for ticker, risks in risk_by_ticker.items():
        name = ticker_to_name.get(ticker, ticker)
        lines.append(f"**{name}** (`{ticker}`)")
        for risk in risks:
            lines.append(f"- {risk}")
        lines.append("")
    return "\n".join(lines)


# ==============================================================
# 単体テスト
# ==============================================================

if __name__ == "__main__":
    import importlib.util, sys
    for p in ["/content/config.py", str(Path(__file__).parent / "config.py")]:
        if Path(p).exists():
            spec = importlib.util.spec_from_file_location("config", p)
            cfg  = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cfg)
            break

    print("=== EdinetClient v3.0 単体テスト ===")
    db_key  = getattr(cfg, "EDINETDB_API_KEY", "")
    gov_key = getattr(cfg, "EDINET_API_KEY", "")
    print(f"EdinetDB APIキー: {'あり' if db_key else 'なし'}")
    print(f"政府EDINET APIキー: {'あり' if gov_key else 'なし'}")

    data_dir  = getattr(cfg, "DATA_DIR", Path("/tmp"))
    cache_dir = Path(data_dir) / "edinet_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    daily_cache = _load_daily_cache(cache_dir)
    print(f"本日コール数: {daily_cache.get('calls_used', 0)}/{DAILY_BUDGET}")

    data = get_full_company_data("8306.T", cfg, daily_cache, cache_dir)
    _save_daily_cache(cache_dir, daily_cache)

    print(f"\n--- 取得結果 ---")
    print(f"企業名: {data['company_name']}")
    print(f"EDINETコード: {data['edinet_code']}")
    print(f"キャッシュ使用: {data['from_cache']}")

    if data["analysis"]:
        print(f"信用スコア: {data['analysis']['credit_score']}/100")
        for r in data["analysis"].get("risk_factors", [])[:3]:
            print(f"  リスク: {r[:60]}")

    if data["financials"]:
        fin = data["financials"]
        print(f"財務年度: {fin['years']}")
        print(f"売上CAGR: {(fin['revenue_cagr'] or 0)*100:.1f}%")

    if data["gov_meta"]:
        print(f"有報提出日: {data['gov_meta']['submit_date']}")

# ADD 0310
def fetch_rankings(metric: str, api_key: str,
                   limit: int = 50,
                   cache_dir: Optional[Path] = None) -> list:
    """
    EdinetDB /rankings/{metric} から指標別ランキングを取得する。
    1コールで全上場企業の上位N社を取得できる。

    metric の選択肢:
      roe, roa, operating-margin, net-margin, equity-ratio,
      per, eps, dividend-yield, payout-ratio,
      revenue, health-score,
      revenue-cagr-3y, oi-cagr-3y, ni-cagr-3y, eps-cagr-3y

    Returns:
        list of dict: [{edinet_code, name, sec_code, value, rank}, ...]
    """
    import requests

    # 長期キャッシュ（1日）
    if cache_dir:
        today = datetime.now().strftime("%Y-%m-%d")
        cp = cache_dir / f"ranking_{metric}_{today}.json"
        if cp.exists():
            try:
                return json.loads(cp.read_text(encoding="utf-8"))
            except Exception:
                pass

    try:
        resp = requests.get(
            f"{EDINETDB_BASE}/rankings/{metric}",
            headers=_headers_db(api_key),
            params={"limit": limit},
            timeout=20,
        )
        if resp.status_code != 200:
            return []

        data = resp.json().get("data", [])

        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)
            cp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        return data

    except Exception as e:
        print(f"      ⚠️  rankings 取得エラー ({metric}): {str(e)[:60]}")
        return []
