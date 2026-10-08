# ==============================================================
# name_resolver.py — ティッカー／証券コード／業種コード → 表示名
# 出力の共通ルール: ティッカーと銘柄名は列を分ける。ティッカー・証券コード・業種コード
#   だけで出ている所には日本語名も入れる。
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


_NET_CACHE: dict[str, str] = {}


def _net_name(t: str) -> str:
    """ローカルに無い銘柄だけ yfinance の社名で補う（週次レポートの表用・1銘柄1回）。"""
    if t in _NET_CACHE:
        return _NET_CACHE[t]
    n = ""
    try:
        import yf_cache as yf
        info = yf.Ticker(t).info or {}
        n = str(info.get("longName") or info.get("shortName") or "")
    except Exception:
        n = ""
    _NET_CACHE[t] = n
    return n


def name_of(ticker, default: str = "", network: bool = False) -> str:
    """銘柄名（日本株＝日本語名・米国株＝米国株名）。分からなければ default。

    network=True のときだけ、ローカルに無い銘柄を yfinance の社名で補う。
    """
    t = str(ticker or "").strip().upper()
    if not t:
        return default
    jp, us = _maps()
    if t.endswith(".T"):
        return jp.get(t[:-2]) or (_net_name(t) if network else "") or default
    if re.fullmatch(r"[0-9][0-9A-Z]{3}", t):        # 証券コードだけ（'8058' / '285A'）
        return jp.get(t, default)
    return us.get(t) or (_net_name(t) if network else "") or default


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
    """メール等の文中で、コードだけの所に名前を補う。

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


_TICKER_CELL_RE = re.compile(r"^[\*`\[]*((?:[0-9][0-9A-Z]{3}\.T)|(?:[A-Z][A-Z0-9]{0,5}(?:[.-][A-Z0-9]{1,3})?))[\*`\]]*$")
_NAME_TICKER_CELL_RE = re.compile(r"^(?P<name>.+?)\s*[（(]`?(?P<t>[0-9][0-9A-Z]{3}\.T|[A-Z][A-Z0-9.\-]{0,8})`?[）)]\s*(?P<rest>.*)$")
_TICKER_HEADERS = {"銘柄", "ticker", "Ticker", "銘柄コード", "コード", "ティッカー"}


def _cells(row: str) -> list[str]:
    return [c.strip() for c in row.strip().strip("|").split("|")]


def _parse_ticker_cell(cell: str):
    m1 = _TICKER_CELL_RE.match(cell)
    if m1:
        return m1.group(1), ""
    m2 = _NAME_TICKER_CELL_RE.match(cell)
    if m2:
        return m2.group("t"), (m2.group("rest") or "")
    return None, ""


def split_ticker_columns(md: str) -> str:
    """Markdown の表で、銘柄列を「ティッカー | 銘柄名」の2列に分ける。

    見出しが「銘柄」「Ticker」「コード」等の列のうち、値の過半がティッカー（7203.T / NVDA / **DG**）か
    「名前(ティッカー)」の列を選ぶ。右隣が既に「銘柄名」なら列は増やさず名前だけ日本語名に直す。
    ティッカー列とは別に英語名の「銘柄」列がある表（審議キュー等）は、その列を銘柄名に置き換える。
    """
    lines = md.split("\n")
    out, i = [], 0
    while i < len(lines):
        ln = lines[i]
        if not (ln.strip().startswith("|") and i + 1 < len(lines)
                and re.match(r"^\s*\|?\s*:?-{2,}", lines[i + 1])):
            out.append(ln)
            i += 1
            continue
        head = _cells(ln)
        j = i + 2
        body = []
        while j < len(lines) and lines[j].strip().startswith("|"):
            body.append(_cells(lines[j]))
            j += 1
        best, best_ok = None, 0
        for k, h in enumerate(head):
            if h not in _TICKER_HEADERS:
                continue
            ok = sum(1 for r in body if k < len(r) and _parse_ticker_cell(r[k])[0])
            if ok > best_ok:
                best, best_ok = k, ok
        if best is None or best_ok < max(1, len(body) // 2):
            out.extend(lines[i:j])
            i = j
            continue
        col = best
        name_col = col + 1 if col + 1 < len(head) and head[col + 1] in ("銘柄名", "名称", "社名") else None
        # ティッカー列の左に英語名の「銘柄」列がある形（審議キュー）
        if name_col is None and col - 1 >= 0 and head[col - 1] == "銘柄":
            name_col = col - 1
        rows2 = []
        for r in body:
            r = r + [""] * (len(head) - len(r))
            t, rest = _parse_ticker_cell(r[col])
            nm = name_of(t, network=True) if t else ""
            if name_col is not None:
                if nm:
                    r[name_col] = nm if name_col > col else f"{nm}"
                if t and rest:
                    r[col] = f"{t} {rest}"
                rows2.append(r)
            else:
                rows2.append(r[:col] + [(f"{t} {rest}".strip() if t else r[col]), nm or (r[col] if not t else "—")]
                             + r[col + 1:])
        if name_col is None:
            head2 = head[:col] + ["ティッカー", "銘柄名"] + head[col + 1:]
        else:
            head2 = list(head)
            head2[col] = "ティッカー"
            head2[name_col] = "銘柄名"
            if name_col < col:   # 「銘柄名 | ティッカー」→「ティッカー | 銘柄名」の順にそろえる
                order = list(range(len(head2)))
                order[name_col], order[col] = order[col], order[name_col]
                head2 = [head2[k] for k in order]
                rows2 = [[r[k] for k in order] for r in rows2]
        out.append("| " + " | ".join(head2) + " |")
        out.append("|" + "|".join(["---"] * len(head2)) + "|")
        for r in rows2:
            out.append("| " + " | ".join(r) + " |")
        i = j
    return "\n".join(out)


def enrich_report(md: str) -> str:
    """週次レポート全体：表は銘柄列を2列に分け、表以外の文中はコードに名前を補う。"""
    md = split_ticker_columns(md)
    out = []
    for ln in md.split("\n"):
        if ln.strip().startswith("|"):
            # 表の中の業種コード（| [1005] | など）にも業種名を付ける
            ln = _SECTOR_BRACKET_RE.sub(
                lambda m: f"{sector_name(m.group(1))}（{m.group(1)}）"
                if sector_name(m.group(1)) != m.group(1) else m.group(0), ln)
            out.append(ln)
        else:
            out.append(enrich_text(ln))
    return "\n".join(out)
