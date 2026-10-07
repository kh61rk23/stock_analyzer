# ==============================================================
# officer_extractor.py — 有価証券報告書の「役員の状況」から役員一覧と経営トップの就任日を読む
#
# データ元: 金融庁 EDINET の有報PDF（disclosure2dl.edinet-fsa.go.jp・APIキー不要）。
#   EdinetDB の役員一覧（/directors）は個人情報保護のため審査付きレーン限定で、
#   標準プランでは 403 になる。政府 EDINET API はキー未取得。PDFは誰でも取れる。
# 対象: 経営者スコア（ceo_evaluator）の評価対象（スクリーナー通過銘柄）＋保有銘柄。
#   書類（doc_id）が変わった会社だけ読み直す。1日の上限は OFFICER_DAILY_LIMIT。
# 出力: stock_data/data/officers.json（ticker → 経営トップ）と BQ edinet.officers（役員全員）。
# 経営トップの判定: 役職に「代表」を含む人のうち「社長／CEO」を含む人を優先。
# 就任日: その人の略歴のうち、当社（同社・当社・自社名）の「社長／CEO」に就いた最初の年月。
#   無ければ「代表取締役」に就いた最初の年月。年月だけなので日は1日とする。
# ==============================================================
from __future__ import annotations

import io
import unicodedata
import json
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path

PDF_URL = "https://disclosure2dl.edinet-fsa.go.jp/searchdocument/pdf/{doc_id}.pdf"
_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
_BIRTH_RE = re.compile(r"(?P<y>\d{4})年\s*(?P<m>\d{1,2})月\s*(?P<d>\d{1,2})日\s*生")
_TITLE_WORD = re.compile(r"役|長|員|代表|監査|委員|執行|社外|常勤|CEO|COO|CFO|CTO|[）)]")
_CAREER_RE = re.compile(r"^(?P<y>\d{4})年\s*(?P<m>\d{1,2})月\s+(?P<text>.+)$")
_END_RE = re.compile(r"\(注\)|（注）|^\s*[\d,]+\s*$")
_TOP_RE = re.compile(r"(?<!副)社長(?!補佐|室|付|代行)|CEO|最高経営責任者")
_ROLE_RE = re.compile(r"取締役|執行役|会長|専務|常務|副社長|理事")
_OTHER_CO = re.compile(r"\(株\)|株式会社|Inc|INC|Co\.|Ltd|LTD|B\.V\.|GmbH|S\.A\.|Corporation|有限|合同会社")


def _norm(s: str) -> str:
    """全角英数・㈱・（注）などを NFKC で半角にそろえる。"""
    return unicodedata.normalize("NFKC", s or "")


def officers_section(pages: list[str]) -> str:
    """有報の全ページ文字列 → 「役員の状況」の役員一覧部分（「社外役員の状況」「監査の状況」の手前まで）。

    生年月日が「1966年5月」「11日生」のように2行に割れるので、つないでから返す。
    """
    for i, t in enumerate(pages):
        n = _norm(t)
        if "略歴" in n and "生年月日" in n and "役員" in n:
            text = _norm("\n".join(pages[i:i + 12]))
            cut = [k for k in (text.find("社外役員の状況"), text.find("【監査の状況】"), text.find("2 社外役員"))
                   if k > 0]
            text = text[:min(cut)] if cut else text
            text = re.sub(r"(\d{4})年\s*\n\s*(\d{1,2})月", r"\1年\2月", text)
            return re.sub(r"(\d{4})年\s*(\d{1,2})月\s*\n\s*(\d{1,2})日\s*生", r"\1年\2月\3日生", text)
    return ""


def split_title_name(pre: str) -> tuple[str, str]:
    """「（代表取締役） 豊 田 章 男」→ ('（代表取締役）', '豊田章男')（純関数）。

    PDFの氏名は1字ずつ空白で区切られることが多い。末尾から役職語を含まない語を氏名として集める。
    """
    toks = (pre or "").split()
    name: list[str] = []
    while toks and not _TITLE_WORD.search(toks[-1]) and len("".join(name)) < 8:
        name.insert(0, toks.pop())
    if not name and toks:      # 「取締役岡本薫明」のように役職と氏名がくっついている
        m = re.match(r"^(.*(?:役|長|員|）|\)))([^役長員）)]{2,6})$", toks[-1])
        if m:
            toks[-1] = m.group(1)
            name = [m.group(2)]
    return " ".join(toks), "".join(name)


_HEADER = {"役職名", "氏名", "生年月日", "略歴", "任期", "所有株式数", "(千株)", "(株)", "所有", "株式数"}
_BIRTH_LINE = re.compile(r"^(?P<pre>.*?)(?P<y>19[2-9]\d|200\d)年\s*(?P<m>\d{1,2})月\s*(?P<d>\d{1,2})日\s*生?$")
_CAREER_HEAD = re.compile(r"^(?P<y>(?:19|20)\d{2})年\s*(?P<m>\d{1,2})月\s*(?P<text>.*)$")


def parse_officers(text: str) -> list[dict]:
    """役員一覧の文字列 → [{title, name, birth, careers: [[YYYY-MM, 文]]}]（純関数）。

    表は「役職名（複数行）→ 氏名 → 生年月日 → 略歴（年月 と 文。折り返しあり）→ (注)n → 株数」の順。
    PDFの読み方によって氏名と生年月日が同じ行にも別の行にもなるので両方受ける。
    """
    lines = [_norm(x).strip() for x in text.split("\n")]
    out: list[dict] = []
    buf: list[str] = []
    cur: dict | None = None
    for ln in lines:
        if ln == "役職名":
            buf = []                       # 表の見出し。前に並んだ氏名の一覧などを捨てる
            continue
        if (not ln or ln in _HEADER or ln in ("普通株式", "(他の会社の代表状況)") or ln.startswith(("EDINET提出書類", "有価証券報告書"))
                or re.match(r"^\d+/\d+$", ln) or re.search(r"\(E\d{5}\)", ln)):
            continue
        m = _BIRTH_LINE.match(ln)
        if m and (cur is None or not cur["careers"] or m.group("pre")):
            pre = m.group("pre").strip()
            if pre:
                t_part, name = split_title_name(pre)
            else:
                t_part, name = "", re.sub(r"\s+", "", buf.pop()) if buf else ""
            title = " ".join(buf + ([t_part] if t_part else []))
            title = re.sub(r"^\(?注\)?\s*\d+\s*", "", title)
            cur = {"title": re.sub(r"\s+", " ", title).strip(), "name": name,
                   "birth": f"{m.group('y')}-{int(m.group('m')):02d}-{int(m.group('d')):02d}", "careers": []}
            out.append(cur)
            buf = []
            continue
        if cur is not None:
            if re.match(r"^\(?注\)?\s*\d", ln):
                cur = None                 # この人の欄の終わり。次の株数の行は捨てる
                continue
            c = _CAREER_HEAD.match(ln)
            mo = re.match(r"^(\d{1,2})月\s*(.*)$", ln)
            if c:
                cur["careers"].append([f"{c.group('y')}-{int(c.group('m')):02d}", c.group("text")])
            elif mo and cur["careers"] and 1 <= int(mo.group(1)) <= 12:
                y = cur["careers"][-1][0][:4]          # 「6月」だけの行は直前と同じ年
                cur["careers"].append([f"{y}-{int(mo.group(1)):02d}", mo.group(2)])
            elif cur["careers"]:
                cur["careers"][-1][1] += ln    # 略歴の折り返し
            continue
        if (re.match(r"^[\d,.\-]+$", ln) or re.match(r"^\(?[\d,]+\)?$", ln)
                or re.search(r"(株|株式)\)|^\(注|^\(百株|^\(千株|^\(株", ln)
                or ln.startswith(("男性", "a.", "b.", "1役員", "1 役員", "(2)", "役員一覧"))):
            continue
        buf = (buf + [ln])[-6:]               # 次の人の役職名と氏名
    return [o for o in out if o["name"]]


def _own_company(text: str, company: str) -> bool:
    """略歴の1行が当社での経歴か（同社・当社・自社名を含む／他社名らしい語を含まない）。"""
    short = re.sub(r"(株式会社|\(株\)|\s)", "", _norm(company))[:4]
    return ("同社" in text or "当社" in text or bool(short and short in text)
            or not _OTHER_CO.search(text))


def pick_top(officers: list[dict], company: str = "") -> dict | None:
    """経営トップと就任年月（純関数）。"""
    reps = [o for o in officers if "代表" in o["title"]]
    tops = [o for o in reps if _TOP_RE.search(o["title"])] or \
           [o for o in officers if _TOP_RE.search(o["title"])] or reps
    if not tops:
        return None
    o = dict(tops[0])
    if not o["careers"]:      # 執行役の表は「略歴は取締役の状況を参照」で空のことがある
        o["careers"] = next((x["careers"] for x in officers if x["name"] == o["name"] and x["careers"]), [])
    # 当社での役職だけを見る。最後に「トップ以外の役職」に就いた後で、最初にトップに就いた年月を就任日とする
    # （副社長→社長のように昇格した人は社長になった年月。子会社の社長歴は数えない）。
    own = [(ym, txt) for ym, txt in o["careers"] if _own_company(txt, company)]
    appointed = None
    for ym, txt in own:
        if _TOP_RE.search(txt):
            appointed = appointed or ym
        elif _ROLE_RE.search(txt) and not re.search(r"会長", txt):
            appointed = None
    if not appointed:
        appointed = next((ym for ym, txt in own if "代表" in txt), None)
    return {"name": o["name"], "title": o["title"], "birth": o["birth"],
            "appointed_date": f"{appointed}-01" if appointed else None}


def pdf_pages(data: bytes) -> list[str]:
    """PDF → ページごとの文字列。PyMuPDF（1冊1秒前後）、無ければ pypdf（1冊数十秒）。"""
    try:
        import pymupdf
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            return [p.get_text() for p in doc]
    except ImportError:
        import pypdf
        out = []
        for p in pypdf.PdfReader(io.BytesIO(data)).pages:
            try:
                out.append(p.extract_text() or "")
            except Exception:
                out.append("")
        return out


def _latest_yuho(bq, codes: list[str]) -> dict:
    """証券コード → 最新の有報（doc_id, edinet_code, 会社名, 決算期末）。BQ 1本。"""
    if not codes:
        return {}
    q = (f"SELECT company_code, edinet_code, company_name, doc_id, fiscal_year_end FROM ("
         f" SELECT *, ROW_NUMBER() OVER (PARTITION BY company_code ORDER BY fiscal_year_end DESC, "
         f"disclosure_date DESC) rn FROM `{bq.project}.edinet.doc_index`"
         f" WHERE doc_type = '有価証券報告書' AND company_code IN UNNEST(@codes)) WHERE rn = 1")
    from google.cloud import bigquery
    job = bq.client.query(q, job_config=bigquery.QueryJobConfig(
        query_parameters=[bigquery.ArrayQueryParameter("codes", "STRING", codes)]))
    return {r.company_code: dict(r.items()) for r in job.result()}


def run(cfg, tickers: list[str], limit: int | None = None) -> dict:
    """対象銘柄の役員を必要な分だけ読み、officers.json と BQ edinet.officers を更新する。"""
    import requests
    from edinet_bq_client import EDINETBQClient
    data_dir = Path(getattr(cfg, "DATA_DIR", "stock_data/data"))
    path = data_dir / "officers.json"
    try:
        store = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        store = {}
    limit = limit if limit is not None else int(getattr(cfg, "OFFICER_DAILY_LIMIT", 40))
    jp = sorted({t for t in tickers if str(t).endswith(".T")})
    bq = EDINETBQClient()
    docs = _latest_yuho(bq, [t[:-2] for t in jp])
    todo = [t for t in jp if t[:-2] in docs and (store.get(t) or {}).get("doc_id") != docs[t[:-2]]["doc_id"]]
    stats = {"targets": len(jp), "todo": len(todo), "ok": 0, "no_top": 0, "error": 0}
    rows, now = [], datetime.now(timezone.utc).isoformat()
    for t in todo[:limit]:
        d = docs[t[:-2]]
        try:
            r = requests.get(PDF_URL.format(doc_id=d["doc_id"]), headers=_UA, timeout=60)
            r.raise_for_status()
            officers = parse_officers(officers_section(pdf_pages(r.content)))
        except Exception as exc:
            stats["error"] += 1
            print(f"    ⚠️ [officers] {t} {d['doc_id']}: {type(exc).__name__}: {str(exc)[:60]}")
            continue
        top = pick_top(officers, d.get("company_name") or "")
        store[t] = {"doc_id": d["doc_id"], "fiscal_year_end": str(d.get("fiscal_year_end") or ""),
                    "checked": date.today().isoformat(), "n_officers": len(officers), "top": top}
        stats["ok" if top else "no_top"] += 1
        for i, o in enumerate(officers):
            rows.append({"officer_id": f"{d['doc_id']}_{i:02d}", "doc_id": d["doc_id"],
                         "company_code": t[:-2], "edinet_code": d.get("edinet_code"),
                         "fiscal_year_end": str(d.get("fiscal_year_end") or "") or None,
                         "seq": i, "title": o["title"], "name": o["name"], "birth_date": o["birth"],
                         "is_top": bool(top and o["name"] == top["name"]),
                         "appointed_top": top["appointed_date"] if top and o["name"] == top["name"] else None,
                         "career_json": json.dumps(o["careers"], ensure_ascii=False), "updated_at": now})
        time.sleep(1.0)
    path.write_text(json.dumps(store, ensure_ascii=False, indent=1), encoding="utf-8")
    if rows:
        try:
            bq.merge_rows("officers", rows, ["officer_id"])
        except Exception as exc:
            stats["error"] += 1
            print(f"    ⚠️ [officers] BQ MERGE失敗: {type(exc).__name__}: {str(exc)[:80]}")
    stats["rows"] = len(rows)
    print(f"    👔 [officers] 対象{stats['targets']}社 / 要更新{stats['todo']}社 → 読取{stats['ok']}社"
          f"（トップ不明{stats['no_top']}・失敗{stats['error']}）/ 役員{len(rows)}行")
    return stats


def load_top(cfg, ticker: str) -> dict | None:
    """officers.json から経営トップ（ceo_evaluator 用）。"""
    try:
        store = json.loads((Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / "officers.json")
                           .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return (store.get(ticker) or {}).get("top")
