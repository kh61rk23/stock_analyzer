# ==============================================================
# edinet_gov_xbrl.py
# 政府 EDINET API v2 から有報の XBRL を取り、IFRS 会社の投資不動産（公正価値の注記を含む）の要素を調べる
# ==============================================================
# 【なぜ】
#   賃貸等不動産の時価は EdinetDB real-estate で取れるが、IFRS 会社の「投資不動産の公正価値」は
#   EdinetDB が抽出していない（10/17 に IFRS 5社で確認：時価の付いた年度なし）。
#   政府 EDINET API（無料・キー登録制）の XBRL なら、注記の要素をそのまま読める。
# 【データの持ち方】
#   帳簿価額は数値要素 jpigp_cor:InvestmentPropertyIFRS（CurrentYearInstant / Prior1YearInstant・円）。
#   公正価値は数値要素が無く、注記のテキストブロックの文中・表にしか無い。書き方は会社ごとに違う
#   （帳簿価額と公正価値が横に並ぶ表／「公正価値」の行に前期・当期の2値だけ）ので fair_value_from_text が両方を読む。
# 【取り込み】
#   対象は company_master の accounting_standard='IFRS' の上場会社。各社の最新の有報（doc_index）を取り、
#   当期の帳簿価額・公正価値を real_estate.rental_property_fv（source='edinet_gov_xbrl'）へ1本の MERGE で書く。
#   台帳 stock_data/data/edinet_gov_fv_state.json に会社ごとの結果（ok / no_property＝投資不動産なし＝対象外 /
#   no_fv / error）と処理した doc_id を残し、同じ doc_id は飛ばす（新しい有報が出たら取り直す）。
#   1日の社数は EDINET_GOV_FV_DAILY_LIMIT、政府 API への連続アクセスは EDINET_GOV_MIN_INTERVAL_SEC 以上空ける。
#   EdinetDB 側の rental_property_ingestor は、ここで ok になった会社を対象から外す
#   （EdinetDB の行は公正価値が空で、同じキーの MERGE が公正価値を消すため）。
# 【キー】GitHub Secret EDINET_GOV_API_KEY（健太が登録して設定する）。無ければ何もしない。
#   要求 URL にキーが入るので、例外の本文は出さず型名だけを出す。
# 【実行】python3 edinet_gov_xbrl.py --probe E03516 [E02513 ...]（manual_task edinet_gov_probe）
#         python3 edinet_gov_xbrl.py --ingest [--limit N]（manual_task edinet_gov_fv_ingest・日次処理からも呼ぶ）
# ==============================================================
from __future__ import annotations

import argparse
import html as _html
import io
import json
import os
import re
import sys
import time
import unicodedata
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

API = "https://api.edinet-fsa.go.jp/api/v2"
MATCH = re.compile(r"InvestmentProperty|InvestmentProperties|RealEstateForLease", re.I)

DOC_SQL = """
SELECT doc_id, fiscal_year_end, disclosure_date
FROM `{project}.edinet.doc_index`
WHERE edinet_code = @code AND doc_type = '有価証券報告書'
ORDER BY disclosure_date DESC LIMIT 1
"""


def facts_from_xbrl(xml: str) -> list[dict]:
    """XBRL インスタンスから、要素名が MATCH に当たる要素を取り出す（純関数）。値は先頭600字。"""
    out = []
    # 事実の要素は子要素を持たない（テキストブロックの HTML はエスケープされている）ので、中身は < を含まない
    pat = re.compile(r"<([A-Za-z0-9_\-]+):([A-Za-z0-9_]+)\b([^>]*)>([^<]*)</\1:\2>", re.S)
    for m in pat.finditer(xml or ""):
        prefix, name, attrs, val = m.groups()
        if not MATCH.search(name):
            continue
        a = dict(re.findall(r'(\w+)="([^"]*)"', attrs))
        text = re.sub(r"<[^>]+>", " ", _html.unescape(val))
        text = re.sub(r"\s+", " ", text).strip()
        out.append({"element": f"{prefix}:{name}", "contextRef": a.get("contextRef"), "unitRef": a.get("unitRef"),
                    "decimals": a.get("decimals"), "value": text[:600], "is_text_block": name.endswith("TextBlock"),
                    "text": text if name.endswith("TextBlock") else None})
    return out


def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "cp932"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def facts_from_ixbrl(htm: str) -> list[dict]:
    """インライン XBRL（ix:nonFraction / ix:nonNumeric）から、name が MATCH に当たる要素を取り出す（純関数）。"""
    out = []
    pat = re.compile(r"<ix:(nonFraction|nonNumeric)\b([^>]*)>(.*?)</ix:\1>", re.S | re.I)
    for m in pat.finditer(htm or ""):
        kind, attrs, inner = m.groups()
        a = dict(re.findall(r'([\w:]+)="([^"]*)"', attrs))
        name = a.get("name", "")
        if not MATCH.search(name):
            continue
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", _html.unescape(inner))).strip()
        out.append({"element": name, "contextRef": a.get("contextRef"), "unitRef": a.get("unitRef"),
                    "decimals": a.get("decimals"), "scale": a.get("scale"), "sign": a.get("sign"),
                    "value": text[:600], "is_text_block": name.endswith("TextBlock"),
                    "text": text if name.endswith("TextBlock") else None})
    return out


def facts_from_zip(blob: bytes) -> tuple[list[dict], str]:
    """ZIP の中の XBRL インスタンス（.xbrl）を読む。無ければ PublicDoc のインライン XBRL（.htm）を全部読む。"""
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names = z.namelist()
        inst = [n for n in names if n.endswith(".xbrl") and "PublicDoc" in n]
        if inst:
            return facts_from_xbrl(_decode(z.read(sorted(inst, key=len)[0]))), "xbrl"
        facts = []
        for n in sorted(n for n in names if "PublicDoc" in n and n.endswith(".htm")):
            facts.extend(facts_from_ixbrl(_decode(z.read(n))))
        return facts, "ixbrl"


# ── 公正価値・帳簿価額の取り出し（純関数） ───────────────────────────

_NUM = re.compile(r"([△▲\-]?)(\d[\d,]*(?:\.\d+)?)")
FV_RATIO_MIN, FV_RATIO_MAX = 0.3, 20.0


def _numbers(text: str) -> list[tuple[int, int, float]]:
    """文中の数値を (開始, 終了, 値) で返す。カンマを外し、△▲は負にする。"""
    out = []
    for m in _NUM.finditer(text):
        v = float(m.group(2).replace(",", ""))
        out.append((m.start(), m.end(), -v if m.group(1) in ("△", "▲", "-") else v))
    return out


def fair_value_from_text(text: str, book_values_million: list) -> list | None:
    """注記テキスト（HTML を外した文字列）から、帳簿価額（百万円・古い期末→新しい期末の順）に対応する公正価値を百万円で拾う。

    戻り値は book_values_million と同じ長さのリスト（取れなかった期末は None）。1つも取れなければ None。
    表の金額が千円・円単位の会社もあるので、帳簿価額×1・1,000・1,000,000 の順に文中の数値と突き合わせ、当たった単位で読む。
    読み方は2通り:
      1) 帳簿価額の数値（カンマ区切り）が「公正価値」という語より後ろにあり、その直後（数字と数字の間に
         6字以内の注記しか挟まない）に次の数値が並ぶなら、それをその期末の公正価値とする（横並びの表）。
         増減表にも同じ帳簿価額が出るため、いちばん後ろの出現を採る（公正価値の表は増減表の後にある）。
      2) 1) で1つも取れなければ、「公正価値」または「投資不動産」という見出しの直後に並ぶ数値
         （前期・当期の2値、帳簿価額が1つなら当期の1値）を期末の順に当てる。帳簿価額の表そのもの
         （値が帳簿価額と同じ）は除く。
    帳簿価額の 0.3〜20 倍の外は捨てる。
    """
    t = unicodedata.normalize("NFKC", text or "")
    first = t.find("公正価値")
    bvs = list(book_values_million or [])
    if first < 0 or not bvs:
        return None
    nums = _numbers(t)
    mults = (1, 1000, 1000000)

    def ok(bv, fv):
        return bv is not None and bv > 0 and fv is not None and FV_RATIO_MIN * bv <= fv <= FV_RATIO_MAX * bv

    def adjacent(i):
        """数値 i の直後の数値（間に6字以内の非数字しか挟まない）。"""
        if i + 1 >= len(nums):
            return None
        gap = t[nums[i][1]:nums[i + 1][0]]
        return nums[i + 1][2] if len(gap) <= 6 and "公正価値" not in gap else None

    out: list = [None] * len(bvs)
    for k, bv in enumerate(bvs):
        if bv is None:
            continue
        for mult in mults:
            hits = [i for i, (a, _b, v) in enumerate(nums) if a > first and round(v) == round(bv * mult)]
            for i in reversed(hits):
                fv = adjacent(i)
                if fv is not None and ok(bv, fv / mult):
                    out[k] = fv / mult
                    break
            if out[k] is not None:
                break
    if any(v is not None for v in out):
        return out
    # 2) 見出しの直後の数値
    anchors = [m.end() for m in re.finditer("公正価値", t)] + \
              [m.end() for m in re.finditer("投資不動産", t) if m.start() > first]
    for end in sorted(anchors):
        vals = []
        for i, (a, _b, v) in enumerate(nums):
            if a < end:
                continue
            if a - (nums[i - 1][1] if vals else end) > 6:
                break
            vals.append(v)
            if len(vals) == 2:
                break
        if not vals:
            continue
        pick = vals[:2] if len(bvs) >= 2 else vals[-1:]
        if len(pick) != len(bvs) or any(b is None for b in bvs):
            continue
        for mult in mults:
            fvs = [v / mult for v in pick]
            if all(ok(b, v) for b, v in zip(bvs, fvs)) and not all(round(v * mult) == round(b * mult) for b, v in zip(bvs, fvs)):
                return fvs
    return None


def book_values_from_facts(facts: list[dict]) -> tuple[float | None, float | None]:
    """InvestmentPropertyIFRS の前期末・当期末の帳簿価額を百万円で返す（純関数）。無い・0 は None。"""
    cur = {"Prior1YearInstant": None, "CurrentYearInstant": None}
    for f in facts:
        if str(f.get("element", "")).split(":")[-1] != "InvestmentPropertyIFRS" or f.get("contextRef") not in cur:
            continue
        try:
            v = float(str(f["value"]).replace(",", "")) * 10 ** int(f.get("scale") or 0)
        except (TypeError, ValueError):
            continue
        cur[f["contextRef"]] = (-v if f.get("sign") == "-" else v) / 1e6
    pick = lambda x: x if x and x > 0 else None
    return pick(cur["Prior1YearInstant"]), pick(cur["CurrentYearInstant"])


def build_row(code: str, doc_id: str, fiscal_year_end, facts: list[dict], now: str | None = None) -> tuple[str, dict | None]:
    """1社の有報の要素 → (状態, rental_property_fv の行)。状態は ok / no_property（投資不動産なし）/ no_fv。"""
    prior, cur = book_values_from_facts(facts)
    if cur is None:
        return "no_property", None
    text = " ".join(f["text"] for f in facts if f.get("text"))
    got = fair_value_from_text(text, [prior, cur] if prior else [cur])
    fv = got[-1] if got else None
    if fv is None:
        return "no_fv", None
    now = now or datetime.now(timezone.utc).isoformat()
    return "ok", {
        "edinet_code": code, "fiscal_year": int(str(fiscal_year_end)[:4]), "doc_id": doc_id,
        "book_value_m_yen": cur, "fair_value_m_yen": fv, "fv_bv_ratio": round(fv / cur, 4),
        "book_value_gap_m_yen": fv - cur, "bs_investment_property_m_yen": cur,
        "extraction_status": "ok", "source": "edinet_gov_xbrl", "updated_at": now,
    }


def pick_targets(latest: dict, state: dict, limit: int, retry_before: str = "") -> list[str]:
    """処理する会社（純関数）。latest は {code: doc_id}。台帳に同じ doc_id が無い会社だけを、
    未処理 → 新しい有報が出た会社、の順に limit 社。retry_before（日付文字列）以前に no_fv だった会社も取り直す。"""
    never = [c for c in sorted(latest) if c not in state]
    # error は次回また取る。no_fv（注記の書き方を読めなかった）は読み方の改善を拾うため 30 日後に取り直す
    stale = [c for c in sorted(latest) if c in state and (
        state[c].get("doc_id") != latest[c] or state[c].get("status") == "error"
        or (state[c].get("status") == "no_fv" and str(state[c].get("fetched", "")) <= retry_before))]
    return (never + stale)[:max(0, int(limit))]


def ok_codes(cfg) -> set[str]:
    """台帳で公正価値が取れている会社。EdinetDB 側の取り込みが同じキーを空の公正価値で上書きしないよう外す。"""
    try:
        st = json.loads(_state_path(cfg).read_text(encoding="utf-8"))
    except Exception:
        return set()
    return {c for c, v in st.items() if isinstance(v, dict) and v.get("status") == "ok"}


# ── I/O ─────────────────────────────────────────────────────

def _state_path(cfg) -> Path:
    return Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / "edinet_gov_fv_state.json"


IFRS_DOC_SQL = """
SELECT m.edinet_code, d.doc_id, d.fiscal_year_end
FROM `{project}.edinet.company_master` AS m
JOIN (
  SELECT edinet_code, doc_id, fiscal_year_end
  FROM `{project}.edinet.doc_index`
  WHERE doc_type = '有価証券報告書'
  QUALIFY ROW_NUMBER() OVER (PARTITION BY edinet_code ORDER BY disclosure_date DESC, doc_id DESC) = 1
) AS d USING (edinet_code)
WHERE m.accounting_standard = 'IFRS' AND NOT IFNULL(m.is_delisted, FALSE)
"""


def fetch_zip(requests, key: str, doc_id: str) -> tuple[bytes | None, str]:
    """有報 ZIP を取る。本体は ZIP だが Content-Type は octet-stream で返るので、先頭の PK で見分ける。"""
    try:
        r = requests.get(f"{API}/documents/{doc_id}", params={"type": 1, "Subscription-Key": key}, timeout=120)
    except Exception as e:  # 例外の本文には要求 URL（キー入り）が入るので型名だけ
        return None, type(e).__name__
    if r.status_code != 200 or not r.content.startswith(b"PK"):
        return None, f"HTTP {r.status_code}"
    return r.content, ""


def ingest(cfg, limit: int | None = None) -> dict:
    """IFRS 会社の最新の有報から投資不動産の帳簿価額・公正価値を取り、rental_property_fv へ MERGE で書く。"""
    key = (os.environ.get("EDINET_GOV_API_KEY") or str(getattr(cfg, "EDINET_GOV_API_KEY", "") or "")).strip()
    if not key:
        return {"skipped": True, "reason": "EDINET_GOV_API_KEY missing"}
    import requests
    from edinet_bq_client import EDINETBQClient
    import rental_property_ingestor as rpi
    limit = int(limit if limit is not None else getattr(cfg, "EDINET_GOV_FV_DAILY_LIMIT", 30))
    gap = float(getattr(cfg, "EDINET_GOV_MIN_INTERVAL_SEC", 1.0))
    bq = EDINETBQClient()
    rpi.ensure_table(bq)
    latest_rows = {r.edinet_code: r for r in bq.client.query(IFRS_DOC_SQL.format(project=bq.project)).result()}
    path = _state_path(cfg)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        state = {}
    retry_before = (date.today() - timedelta(days=30)).isoformat()
    targets = pick_targets({c: r.doc_id for c, r in latest_rows.items()}, state, limit, retry_before)
    today = date.today().isoformat()
    rows, stat = [], {"ok": 0, "no_property": 0, "no_fv": 0, "error": 0}
    last = 0.0
    for code in targets:
        r = latest_rows[code]
        wait = gap - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        blob, err = fetch_zip(requests, key, r.doc_id)
        last = time.monotonic()
        if blob is None:
            stat["error"] += 1
            state[code] = {"status": "error", "doc_id": r.doc_id, "fetched": today, "error": err}
            print(f"    ❌ {code} {r.doc_id}: {err}")
            if err == "HTTP 429":
                break
            continue
        try:
            facts, _fmt = facts_from_zip(blob)
            status, row = build_row(code, r.doc_id, r.fiscal_year_end, facts)
        except Exception as e:
            stat["error"] += 1
            state[code] = {"status": "error", "doc_id": r.doc_id, "fetched": today, "error": type(e).__name__}
            print(f"    ❌ {code} {r.doc_id}: 解析 {type(e).__name__} {str(e)[:100]}")
            continue
        stat[status] += 1
        state[code] = {"status": status, "doc_id": r.doc_id, "fetched": today}
        if row:
            rows.append(row)
            state[code].update(book=row["book_value_m_yen"], fair=row["fair_value_m_yen"])
            print(f"    ✅ {code} {r.doc_id}: 帳簿 {row['book_value_m_yen']:,.0f} 公正 {row['fair_value_m_yen']:,.0f}（百万円）")
        elif status == "no_fv":
            text = " ".join(f["text"] for f in facts if f.get("text"))
            i = text.find("公正価値")
            row_bv = book_values_from_facts(facts)
            print(f"    ⚠️ {code} {r.doc_id}: 帳簿価額 {row_bv} はあるが公正価値を拾えない: {text[max(0, i - 40):i + 700]!r}")
    written = False
    if rows:
        written = bool(bq.merge_rows("rental_property_fv", rows, ["edinet_code", "fiscal_year"], dataset="real_estate"))
        if not written:
            for row in rows:
                state.pop(row["edinet_code"], None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    info = {"candidates": len(latest_rows), "targets": len(targets), "with_fair_value": stat["ok"],
            "not_applicable": stat["no_property"], "no_fair_value": stat["no_fv"], "failed": stat["error"],
            "rows": len(rows), "written": written}
    print(f"  🏢 政府 EDINET の公正価値: {info}")
    if rows and not written:
        info["error"] = "取れたのに BQ に書けなかった"
    return info


def probe(cfg, codes: list[str]) -> int:
    key = os.environ.get("EDINET_GOV_API_KEY", "").strip()
    if not key:
        print("⏩ EDINET_GOV_API_KEY が無い（健太が政府 EDINET API のキーを登録して Secret に入れると動く）")
        return 0
    import requests
    from google.cloud import bigquery
    from edinet_bq_client import EDINETBQClient
    client = EDINETBQClient().client
    out_dir = Path(getattr(cfg, "DATA_DIR", "stock_data/data"))
    rc = 0
    for code in codes:
        rows = list(client.query(DOC_SQL.format(project=client.project), job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("code", "STRING", code)])).result())
        if not rows:
            print(f"❌ {code}: doc_index に有報が無い")
            rc = 1
            continue
        doc = rows[0]
        r = requests.get(f"{API}/documents/{doc.doc_id}", params={"type": 1, "Subscription-Key": key}, timeout=120)
        # 本体は ZIP だが Content-Type は application/octet-stream で返るので、先頭の PK で見分ける
        if r.status_code != 200 or not r.content.startswith(b"PK"):
            print(f"❌ {code} {doc.doc_id}: HTTP {r.status_code} {r.headers.get('Content-Type')} {r.text[:200]}")
            rc = 1
            continue
        facts, fmt = facts_from_zip(r.content)
        p = out_dir / f"edinet_gov_probe_{code}.json"
        p.write_text(json.dumps({"fetched": date.today().isoformat(), "edinet_code": code, "doc_id": doc.doc_id,
                                 "fiscal_year_end": str(doc.fiscal_year_end), "format": fmt, "facts": facts},
                                ensure_ascii=False, indent=1), encoding="utf-8")
        names = sorted({f["element"] for f in facts})
        print(f"✅ {code} {doc.doc_id}: 投資不動産の要素 {len(facts)}件（{len(names)}種類・{fmt}）→ {p.name}")
        for n in names[:30]:
            print(f"    {n}")
    return rc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", nargs="+", metavar="EDINET_CODE")
    ap.add_argument("--ingest", action="store_true", help="IFRS 会社の公正価値を取り込む")
    ap.add_argument("--limit", type=int, help="--ingest で処理する社数（既定は EDINET_GOV_FV_DAILY_LIMIT）")
    args = ap.parse_args(argv)
    if not args.probe and not args.ingest:
        ap.error("--probe か --ingest のどちらかが要る")
    import config
    if args.probe:
        return probe(config, args.probe)
    info = ingest(config, args.limit)
    return 1 if info.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
