# ==============================================================
# name_resolver.py — ティッカー／証券コード／業種コード → 表示名
# 2026-10-05 新設（健太指示「共通修正事項：ティッカーと銘柄名は列を分ける」
#   「ティッカーや証券コード、業種コードのみで出ているものは日本語名も入れる」）
#
# 名前の出どころ（すべてローカルファイル・ネットワークアクセスなし）
#   日本株: data/portfolio_holdings.csv の name → stock_data/data/edinetdb_company_cache.json
#           （EdinetDB 会社マスタ・約4,000社。「株式会社」等は外す）
#   米国株: stock_data/data/us_screener_daily_cache.json の name（米国株名＝英語の社名）
#           → data/portfolio_holdings.csv の name
#   業種  : config.JAPAN_SECTORS[code]["name_ja"] / 米国セクターETF / GICSセクター
#
# 使い方:
#   from name_resolver import name_of, label, sector_name
#   name_of("8058.T") -> "三菱商事" / name_of("NVDA") -> "NVIDIA Corporation"
#   label("8058.T")   -> "三菱商事（8058.T）"   … 文中に1語で書くとき
#   表では「| ティッカー | 銘柄名 |」の2列に分ける（cols(t) -> (ticker, name)）
# ==============================================================
from __future__ import annotations

import csv
import json
import re
from functools import lru_cache
from pathlib import Path

REPO = Path(__file__).resolve().parent
_CORP_RE = re.compile(r"(株式会社|（株）|\(株\))")

US_SECTOR_ETF_JA = {
    "XLK": "情報技術", "XLE": "エネルギー", "XLV": "ヘルスケア", "XLC": "通信サービス",
    "XLF": "金融", "XLI": "資本財", "XLY": "一般消費財", "XLP": "生活必需品",
    "XLU": "公益", "XLRE": "不動産", "XLB": "素材",
}
GICS_JA = {
    "Technology": "情報技術", "Information Technology": "情報技術", "Energy": "エネルギー",
    "Healthcare": "ヘルスケア", "Health Care": "ヘルスケア", "Communication Services": "通信サービス",
    "Financial Services": "金融", "Financials": "金融", "Industrials": "資本財",
    "Consumer Cyclical": "一般消費財", "Consumer Discretionary": "一般消費財",
    "Consumer Defensive": "生活必需品", "Consumer Staples": "生活必需品",
    "Utilities": "公益", "Real Estate": "不動産", "Basic Materials": "素材", "Materials": "素材",
}


def _data_dir() -> Path:
    try:
        import config
        return Path(getattr(config, "DATA_DIR", REPO / "stock_data" / "data"))
    except Exception:
        return REPO / "stock_data" / "data"


def _clean_jp(name: str) -> str:
    name = _CORP_RE.sub("", str(name or "")).strip()
    return name.replace("　", " ").strip()


@lru_cache(maxsize=1)
def _maps() -> tuple[dict, dict]:
    jp: dict[str, str] = {}
    us: dict[str, str] = {}
    d = _data_dir()
    try:
        cache = json.loads((d / "edinetdb_company_cache.json").read_text(encoding="utf-8"))
        for c in cache.get("companies", []):
            code = str(c.get("sec_code") or "")[:4].upper()
            if code and (c.get("name") or c.get("name_ja")):
                jp[code] = _clean_jp(c.get("name") or c.get("name_ja"))
    except (OSError, ValueError):
        pass
    try:
        res = json.loads((d / "us_screener_daily_cache.json").read_text(encoding="utf-8")).get("results") or {}
        for t, r in res.items():
            if isinstance(r, dict) and r.get("name"):
                us[str(t).upper()] = str(r["name"])
    except (OSError, ValueError, AttributeError):
        pass
    try:
        with open(REPO / "data" / "portfolio_holdings.csv", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                t, n = str(row.get("ticker") or "").upper(), row.get("name") or ""
                if not t or not n:
                    continue
                if t.endswith(".T"):
                    jp[t[:-2]] = _clean_jp(n)          # 保有銘柄の呼び名を優先
                else:
                    us.setdefault(t, n)
    except OSError:
        pass
    return jp, us


def name_of(ticker, default: str = "") -> str:
    """銘柄名（日本株＝日本語名・米国株＝米国株名）。分からなければ default。"""
    t = str(ticker or "").strip().upper()
    if not t:
        return default
    jp, us = _maps()
    if t.endswith(".T"):
        return jp.get(t[:-2], default)
    if re.fullmatch(r"[0-9][0-9A-Z]{3}", t):        # 証券コードだけ（'8058' / '285A'）
        return jp.get(t, default)
    return us.get(t, default)


def label(ticker, fallback_name: str = "") -> str:
    """文中用「名前（ティッカー）」。名前が分からなければティッカーだけ。"""
    n = name_of(ticker) or fallback_name
    return f"{n}（{ticker}）" if n and n != str(ticker) else str(ticker)


def cols(ticker, fallback_name: str = "") -> tuple[str, str]:
    """表用（ティッカー, 銘柄名）の2列。"""
    return str(ticker), (name_of(ticker) or fallback_name or "—")


def sector_name(code, default: str = "") -> str:
    """東証33業種コード・米国セクターETF・GICSセクター名 → 日本語業種名。"""
    c = str(code or "").strip()
    try:
        import config
        sec = getattr(config, "JAPAN_SECTORS", {}).get(c)
        if sec:
            return sec.get("name_ja", default or c)
    except Exception:
        pass
    return US_SECTOR_ETF_JA.get(c.upper()) or GICS_JA.get(c) or default or c


def sector_label(code) -> str:
    n = sector_name(code)
    return f"{n}（{code}）" if n and n != str(code) else str(code)


_JP_TICKER_RE = re.compile(r"(?<![\w.])([0-9][0-9A-Z]{3}\.T)(?![\w])")
_SECTOR_BRACKET_RE = re.compile(r"\[(10[0-3][0-9])\]")
_SECTOR_LIST_RE = re.compile(r"(対象業種|業種)[:：]\s*((?:10[0-3][0-9](?:\s*[,、/]\s*)?)+)")


def enrich_text(text: str) -> str:
    """メール等の文中で、コードだけの所に名前を補う（2026-10-05）。

    - 日本株のティッカー（7203.T）: 同じ行にまだ名前が無ければ「7203.T トヨタ自動車」
    - 角括弧の業種コード（[1028]）: 「[1028 銀行業]」
    - 「対象業種: 1016, 1015」: 「対象業種: 電気機器(1016), 機械(1015)」
    米国株のティッカーは一般の英単語と区別できないので、ここでは触らない（各節で付ける）。
    """
    if not text:
        return text
    out = []
    for line in str(text).split("\n"):
        # 機械読み取り用の行（例 DELIBERATION_QUEUE_V1: 7203.T,6674.T）は変えない
        if re.match(r"^\s*[A-Z][A-Z0-9_]+:\s", line):
            out.append(line)
            continue
        def _jp(m):
            t = m.group(1)
            n = name_of(t)
            return f"{t} {n}" if n and n not in line else t
        line = _JP_TICKER_RE.sub(_jp, line)
        line = _SECTOR_BRACKET_RE.sub(
            lambda m: f"[{m.group(1)} {sector_name(m.group(1))}]"
            if sector_name(m.group(1)) != m.group(1) else m.group(0), line)
        line = _SECTOR_LIST_RE.sub(
            lambda m: m.group(1) + ": " + ", ".join(
                f"{sector_name(c)}({c})" for c in re.findall(r"10[0-3][0-9]", m.group(2))), line)
        out.append(line)
    return "\n".join(out)


@lru_cache(maxsize=1)
def _edinet_map() -> dict:
    out = {}
    try:
        cache = json.loads((_data_dir() / "edinetdb_company_cache.json").read_text(encoding="utf-8"))
        for c in cache.get("companies", []):
            if c.get("edinet_code"):
                out[c["edinet_code"]] = (str(c.get("sec_code") or "")[:4], _clean_jp(c.get("name") or ""))
    except (OSError, ValueError):
        pass
    return out


def edinet_cols(edinet_code: str) -> tuple[str, str]:
    """EDINETコード → (証券コード, 社名)。分からなければ (EDINETコード, '—')。"""
    sec, nm = _edinet_map().get(str(edinet_code or ""), ("", ""))
    return (sec or str(edinet_code), nm or "—")
