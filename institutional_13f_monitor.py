# ==============================================================
# institutional_13f_monitor.py  v1.0  (Policy Ph3)
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

import sec_client


# ==============================================================
# BLOCK 1: Tier 定義・スコア定数フォールバック
# ==============================================================

_TIER_BASE_SCORE: dict = {
    1: 15.0,   # Buffett クラス
    2: 10.0,   # 著名アクティビスト
    3:  5.0,   # 大手機関
}


from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'I13F_CHANGE_MULTIPLIER': '_CHANGE_MULTIPLIER',
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
    # CIK は 13F 提出主体のもの。1418819＝Iridium / 1011006＝Altaba / 1113169＝T. Rowe Price Group は
    #   13F を出していない別法人なので使わない。
    "0001791786": {"name": "Elliott Investment Management", "tier": 2},
    "0001040273": {"name": "Third Point LLC", "tier": 2},
    "0002012383": {"name": "BlackRock Inc", "tier": 3},   # 1364742 は提出終了（再編）のため使わない
    "0000102909": {"name": "Vanguard Group", "tier": 3},
    "0000093751": {"name": "State Street", "tier": 3},
    "0000315066": {"name": "Fidelity", "tier": 3},
    "0000080255": {"name": "T. Rowe Price", "tier": 3},
}

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
          "version": 2, "quarter": "2026Q2",
          "by_cik":  {cik: {"acc", "period", "prev_period", "n_holdings", "changes"}},
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

# U-2 / F-2: SECは連絡先を含まないUser-Agentを403で拒否する。
# us_8k_monitor.py:116-122 の _sec_user_agent() と同型（優先順: 環境変数 →
# cfg.SEC_USER_AGENT（cfg未指定時は import config でのフォールバック取得）→ 固定文字列）。
# U-1b: 実体は sec_client.user_agent() に統合済み。本関数は tests/test_sec_user_agent.py
# の既存回帰テストとの互換のために薄いラッパーとして残す。
_SEC_UA_FALLBACK = "StockAnalyzer stockanalyzer@example.com"


def _sec_user_agent(cfg=None) -> str:
    return sec_client.user_agent(cfg)


def _fetch_cik_filings(cik: str, cfg=None) -> Optional[dict]:
    """
    SEC EDGAR submissions API から CIK のファイリング一覧を取得する（sec_client 経由）。
    URL: https://data.sec.gov/submissions/{CIK}.json

    挙動注記: タイムアウトは sec_client 側（HTTP_TIMEOUT 既定15秒 / cfg.SEC_HTTP_TIMEOUT
    で上書き可）に委譲する。

    Returns:
        submissions dict or None
    """
    try:
        data = sec_client.submissions(cik, cfg=cfg)
        if data is None:
            print(f"    ⚠️  [13f] CIK {cik} 取得失敗")
        return data
    except Exception as e:
        print(f"    ⚠️  [13f] CIK {cik} 取得失敗: {str(e)[:50]}")
        return None


def _find_infotable_url(cik: str, acc_clean: str, cfg=None) -> Optional[str]:
    """提出書類の索引（index.json）から保有明細XMLのURLを探す。

    固定名 infotable.json は存在しない（404）。明細XMLの名前は提出ごとに違う
    （例 53405.xml / form13fInfoTable.xml）ので、primary_doc.xml 以外の XML を明細とみなす。
    """
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_clean}/"
    idx = sec_client.get_json(base + "index.json", cfg=cfg)
    items = (((idx or {}).get("directory") or {}).get("item")) or []
    xmls = [i.get("name", "") for i in items if str(i.get("name", "")).lower().endswith(".xml")]
    named = [n for n in xmls if "infotable" in n.lower()]
    rest = [n for n in xmls if n.lower() != "primary_doc.xml"]
    pick = (named or rest or [None])[0]
    return base + pick if pick else None


def parse_infotable_xml(text: str, value_in_thousands: bool = False) -> list[dict]:
    """13F 保有明細XML → [{name, cusip, value_usd, shares}]（純関数・名前空間を無視）。

    2023年以降の提出は value がドル単位（それ以前は千ドル単位）。
    """
    import xml.etree.ElementTree as ET
    out = []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return out

    def _local(tag):
        return tag.rsplit("}", 1)[-1]

    for it in root.iter():
        if _local(it.tag) != "infoTable":
            continue
        rec = {}
        for ch in it.iter():
            rec[_local(ch.tag)] = (ch.text or "").strip()
        try:
            value = int(float(rec.get("value", "0")))
            shares = int(float(rec.get("sshPrnamt", "0")))
        except ValueError:
            continue
        out.append({"name": rec.get("nameOfIssuer", ""), "cusip": rec.get("cusip", "").upper(),
                    "value_usd": value * 1000 if value_in_thousands else value,
                    "shares": shares, "ticker": ""})
    # 2023年以降でも千ドル単位で出す提出者がある（T. Rowe Price 等）。1株あたり評価額の中央値が
    # $1 未満なら千ドル単位とみなす。
    if not value_in_thousands:
        px = sorted(r["value_usd"] / r["shares"] for r in out if r["shares"] > 0)
        if px and px[len(px) // 2] < 1.0:
            for r in out:
                r["value_usd"] *= 1000
    # 同一CUSIPの複数行（議決権区分違い等）を合算
    agg: dict = {}
    for r in out:
        a = agg.setdefault(r["cusip"], dict(r, value_usd=0, shares=0))
        a["value_usd"] += r["value_usd"]
        a["shares"] += r["shares"]
    return list(agg.values())


def _fetch_13f_holdings(
    accession_number: str,
    cik: str,
    cfg=None,
    timeout: int = 20,
    resolve: bool = True,
) -> list[dict]:
    """13F ファイリングの保有銘柄一覧を取得する（保有明細XML）。

    Returns:
        [{"ticker": str, "value_usd": int, "shares": int,
          "name": str, "cusip": str}, ...]
    """
    holdings: list = []
    acc_clean = accession_number.replace("-", "")
    try:
        url = _find_infotable_url(cik.lstrip("0") or "0", acc_clean, cfg)
        if not url:
            raise ValueError("保有明細XMLが索引に無い")
        r = sec_client.get(url, cfg=cfg, accept="application/xml", timeout=timeout)
        if r is None or r.status_code != 200:
            raise ValueError(f"HTTP {getattr(r, 'status_code', 'None')}")
        yy = int(accession_number.split("-")[1]) if accession_number.count("-") == 2 else 99
        holdings = parse_infotable_xml(r.text, value_in_thousands=(yy < 23))
        if resolve:
            _resolve_tickers(holdings, cfg)
    except Exception as e:
        print(f"    ⚠️  [13f] 保有リスト取得失敗 ({accession_number[:20]}): "
              f"{str(e)[:60]}")
    return holdings


def _resolve_tickers(holdings: list, cfg=None) -> None:
    """CUSIP → ティッカー（OpenFIGI・キー不要・結果は cusip_ticker.json に保存して使い回す）。"""
    import json as _json
    import urllib.request as _ur
    cache_p = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache")) / "cusip_ticker.json"
    try:
        cache = _json.loads(cache_p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    todo = sorted({h["cusip"] for h in holdings if h.get("cusip") and h["cusip"] not in cache})
    for i in range(0, len(todo), 10):          # キー無しは1リクエスト10件・毎分25回まで
        chunk = todo[i:i + 10]
        try:
            req = _ur.Request("https://api.openfigi.com/v3/mapping",
                              data=_json.dumps([{"idType": "ID_CUSIP", "idValue": c} for c in chunk]).encode(),
                              headers={"Content-Type": "application/json"})
            res = _json.loads(_ur.urlopen(req, timeout=20).read())
            for c, x in zip(chunk, res):
                us = [d for d in (x.get("data") or []) if d.get("exchCode") == "US"]
                cache[c] = (us or x.get("data") or [{}])[0].get("ticker", "") if x.get("data") else ""
        except Exception as exc:
            print(f"    ⚠️  [13f] CUSIP→ticker 変換失敗: {str(exc)[:60]}")
            break
        time.sleep(2.6)
    try:
        cache_p.parent.mkdir(parents=True, exist_ok=True)
        cache_p.write_text(_json.dumps(cache, ensure_ascii=False, indent=0), encoding="utf-8")
    except OSError:
        pass
    for h in holdings:
        h["ticker"] = cache.get(h.get("cusip", ""), "") or h.get("ticker", "")


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


HISTORY_VERSION = 3   # 前回提出との差分方式・ETF除外（古い版の履歴は使わずに取り直す）


def _get_13f_filings(submissions: dict, n: int = 2) -> list[tuple[str, str]]:
    """submissions JSON → 13F-HR の [(accession, 報告基準日)] を新しい順に n 件（基準日が重複しない）（純関数）。

    訂正報告（13F-HR/A）は保有の一部だけのことが多いので差分には使わない。
    """
    out, seen = [], set()
    try:
        r = submissions.get("filings", {}).get("recent", {})
        for f, a, p in zip(r.get("form", []), r.get("accessionNumber", []), r.get("reportDate", [])):
            if f == "13F-HR" and p not in seen:
                out.append((a, p))
                seen.add(p)
                if len(out) >= n:
                    break
    except Exception:
        pass
    return out


def _period_to_quarter(report_date: str) -> str:
    """'2026-06-30' → '2026Q2'（純関数）。"""
    try:
        return f"{report_date[:4]}Q{(int(report_date[5:7]) - 1) // 3 + 1}"
    except (ValueError, IndexError):
        return ""


_FUND_RE = None


def is_fund_name(name: str) -> bool:
    """13F の nameOfIssuer が ETF・投資信託か（例 ISHARES TR / SELECT SECTOR SPDR TR / VANECK ETF TRUST）（純関数）。"""
    import re
    global _FUND_RE
    if _FUND_RE is None:
        _FUND_RE = re.compile(r"\b(ETF|ISHARES|SPDR|INVESCO QQQ|VANGUARD (?:INDEX|WORLD|SPECIALIZED|BD)|"
                              r"PROSHARES|DIREXION|WISDOMTREE|GLOBAL X|ARK ETF|SELECT SECTOR|FD TR|INDEX FD|VANECK)\b",
                              re.I)   # 「… RLTY TR」の REIT は株式なので「TR で終わる」では判定しない
    return bool(_FUND_RE.search(name or ""))


def diff_holdings(curr: list[dict], prev: list[dict], cik_info: dict) -> list[dict]:
    """同じ機関の今回と前回の13F保有明細（CUSIP単位）を比べて変化を返す（純関数）。

    保存データではなく前回提出そのものと比べる（保存が空だと全保有が new_position になるため）。
    増減は株数で判定（評価額は株価で動くため）。株数が無いときだけ評価額で見る。
    """
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cm = {h["cusip"]: h for h in curr if h.get("cusip")}
    pm = {h["cusip"]: h for h in prev if h.get("cusip")}
    out = []
    for cu in set(cm) | set(pm):
        c, p = cm.get(cu), pm.get(cu)
        if is_fund_name((c or p).get("name", "")):
            continue          # ETF・投資信託は銘柄選択のシグナルにならない（L3・新規取得通知の対象外）
        if c and not p:
            ct = "new_position"
        elif p and not c:
            ct = "sold"
        else:
            a, b = (c["shares"], p["shares"]) if (c.get("shares") and p.get("shares")) \
                else (c.get("value_usd", 0), p.get("value_usd", 0))
            ct = "increased" if a > b * 1.10 else "decreased" if a < b * 0.90 else ""
        if not ct:
            continue
        base = c or p
        out.append({
            "ticker": base.get("ticker", ""), "cusip": cu,
            "institution": cik_info.get("name", ""), "tier": cik_info.get("tier", 3),
            "change_type": ct,
            "value_usd": (c or {}).get("value_usd", 0), "prev_value_usd": (p or {}).get("value_usd", 0),
            "value_change_usd": (c or {}).get("value_usd", 0) - (p or {}).get("value_usd", 0),
            "shares": (c or {}).get("shares", 0), "prev_shares": (p or {}).get("shares", 0),
            "name": base.get("name", ""), "detected_at": now_str,
        })
    return out


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
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    print("\n  🏦 [13f] 機関投資家ポジション監視 開始...")
    t0      = datetime.now()
    quarter = _current_quarter()

    # ── キャッシュ確認（四半期フラグ False の場合） ───────────
    history = _load_history(cfg)
    if history.get("version") != HISTORY_VERSION:
        # 旧形式（前四半期の保存データとの比較）は全保有を「新規」と誤検出していたので捨てる
        history = {"version": HISTORY_VERSION, "by_cik": {}, "changes": [], "last_updated": ""}
    if not is_quarter_end:
        cached_changes = history.get("changes", [])
        if history.get("by_cik"):
            quarter = history.get("quarter") or quarter
            print(f"    📦 [13f] キャッシュ利用 (quarter={quarter}): "
                  f"{len(cached_changes)}件の変化")
            # キャッシュからも scored / boosts を再構築
            _scored = _score_changes(cached_changes, cfg)
            _boosts = _build_sector_boosts(_scored, results, cfg)
            return _build_result(
                quarter, cached_changes, _scored, _boosts,
                {}, cached=True,
            )
        print("    📋 [13f] キャッシュなし → 軽量スキャン実行")

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

    # ── 各機関: 最新の13F と その前の13F を比べる ───────────────
    # 提出が前回から変わっていない機関は取り直さない（提出期間中に毎日全件取り直すと長時間かかる）。
    #   ティッカー変換（OpenFIGI）は変化のあった銘柄だけ。大手機関（Tier3）は評価額が
    #   INSTITUTION_MIN_NEW_POSITION_USD 以上の変化だけ残す（数千件の小口変化はノイズ）。
    min_large = float(getattr(cfg, "INSTITUTION_MIN_NEW_POSITION_USD", 1_000_000_000))
    min_active = float(getattr(cfg, "INSTITUTION_MIN_NEW_POSITION_USD_ACTIVE", 100_000_000))
    all_changes: list = []
    fresh_changes: list = []   # 今回取り直した機関の変化だけ（BQ に二重に入れない）
    periods: list = []
    by_cik = history.setdefault("by_cik", {})
    for cik, cik_info in target_ciks.items():
        name = cik_info["name"]
        try:
            submissions = _fetch_cik_filings(cik, cfg)
            if not submissions:
                continue
            filings = _get_13f_filings(submissions, 2)
            if not filings:
                print(f"    ⚠️  [13f] {name}: 13F ファイリングなし")
                continue
            acc, period = filings[0]
            if period < (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d"):
                print(f"    ⚠️  [13f] {name}: 最新の13Fが{period}と古い（提出主体の変更の可能性）→ 対象外")
                continue
            rec = by_cik.get(cik)
            if rec and rec.get("acc") == acc:
                print(f"    📦 [13f] {name}: 新しい提出なし（{period}）→ 前回の差分を使う")
            else:
                print(f"    📄 [13f] {name} ({cik}) {period} を取得中...")
                curr = _fetch_13f_holdings(acc, cik, cfg, resolve=False)
                if not curr:
                    print(f"    ⚠️  [13f] {name}: 保有リスト取得失敗")
                    continue
                prev = _fetch_13f_holdings(filings[1][0], cik, cfg, resolve=False) if len(filings) > 1 else []
                if not prev:
                    print(f"    ⚠️  [13f] {name}: 前回の提出が無い → 差分なし（新規判定しない）")
                    changes = []
                else:
                    changes = diff_holdings(curr, prev, cik_info)
                    if cik_info.get("tier", 3) >= 3:
                        changes = [c for c in changes
                                   if max(c["value_usd"], c["prev_value_usd"]) >= min_large]
                    _resolve_tickers(changes, cfg)
                    changes = [c for c in changes if c.get("ticker")]
                rec = {"acc": acc, "period": period, "prev_period": filings[1][1] if len(filings) > 1 else "",
                       "n_holdings": len(curr), "changes": changes}
                by_cik[cik] = rec
                fresh_changes.extend(changes)
                n_new = sum(1 for c in changes if c["change_type"] == "new_position")
                print(f"    ✅ [13f] {name}: {len(curr)}銘柄保有 / 前回({rec['prev_period']})比 "
                      f"変化{len(changes)}件（新規{n_new}）")
                time.sleep(0.5)   # SEC サーバへの負荷軽減
            all_changes.extend(rec.get("changes", []))
            periods.append(rec.get("period", ""))
        except Exception as e:
            print(f"    ⚠️  [13f] {name} 処理エラー（スキップ）: "
                  f"{str(e)[:60]}")
    if periods:
        quarter = _period_to_quarter(max(periods))   # 報告基準日の四半期（例 2026-06-30 → 2026Q2）
    curr_positions_all: dict = {}

    # ── スコアリング ──────────────────────────────────────────
    scored  = _score_changes(all_changes, cfg)
    boosts  = _build_sector_boosts(scored, results, cfg)

    # ── Tier1 新規取得 / 10億ドル超検出（アラート用）─────────
    # 通知は今回取り直した（＝新しい提出があった）機関の分だけ。提出期間中に毎日同じ通知を出さない。
    new_tier1 = [
        c for c in fresh_changes
        if c.get("tier") == 1 and c.get("change_type") == "new_position"
        and c.get("value_usd", 0) >= min_active
    ]
    new_large = [
        c for c in fresh_changes
        if c.get("change_type") == "new_position"
        and c.get("value_usd", 0) >= min_large
    ]

    # ── history 更新 ──────────────────────────────────────────
    history["changes"] = all_changes
    history["quarter"] = quarter
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
        for _chg in fresh_changes:
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
