# ==============================================================
# risk_text_scorer.py — 有報「事業等のリスク」を前期と比べ、悪化した会社に印を付ける
# ==============================================================
# 【入力】edinet.gov_text_blocks の section_type='事業等のリスク'（edinet_gov_text が今期と前期の有報から取る）。
#   会社ごとに決算期の新しい2期を比べる。
# 【点の付け方】言語モデルは使わない（Actions の時間と無料枠を食うため）。
#   ・重い語（継続企業の前提・債務超過・減損・訴訟 など。重みつき）の出現回数が前期より増えた分を足す（severity）
#   ・前期に無く今期に初めて出た重い語（重み4以上）があれば悪化の印（flag）
#   ・記述の変わり具合（change_ratio＝1−文字3-gram の Jaccard 係数）と、今期に新しく出た文の数
# 【出力】edinet.risk_text_changes（毎回作り直す・load job）。日次メールの注意項目は alert_rows で引く。
# 【読む量】まとめ読み1本（約 0.4GB）。edinet_gov_backfill.yml の本文の取り込みの後に1日1回。
# 【使い方】python risk_text_scorer.py [--dry]
# ==============================================================
from __future__ import annotations

import argparse
import io
import json
import re
import sys
from datetime import datetime, timezone

TABLE = "edinet.risk_text_changes"
SECTION = "事業等のリスク"
SEVERE = {
    "継続企業の前提": 5, "重要な疑義": 5, "債務超過": 5, "上場廃止": 4, "不適切な会計": 4, "不正会計": 4,
    "財務制限条項": 3, "資金繰り": 3, "行政処分": 3, "業務停止": 3, "不正": 2, "減損": 2, "訴訟": 2,
    "リコール": 2, "営業損失": 2, "情報漏洩": 2, "情報漏えい": 2, "赤字": 1, "希薄化": 1, "システム障害": 1,
    "サイバー攻撃": 1, "制裁": 1, "撤退": 1,
}
FLAG_WEIGHT = 4
FLAG_SEVERITY = 5

SQL = """
SELECT edinet_code, ANY_VALUE(sec_code) sec_code, fiscal_year_end, ANY_VALUE(doc_id) doc_id, ANY_VALUE(raw_text) raw_text
FROM `{p}.edinet.gov_text_blocks`
WHERE section_type = '事業等のリスク' AND raw_text IS NOT NULL AND fiscal_year_end IS NOT NULL
GROUP BY edinet_code, fiscal_year_end
QUALIFY ROW_NUMBER() OVER (PARTITION BY edinet_code ORDER BY fiscal_year_end DESC) <= 2
"""

_SPLIT = re.compile(r"[。\n]")


def _grams(text: str, n: int = 3) -> set:
    t = re.sub(r"\s+", "", text or "")
    return {t[i:i + n] for i in range(max(0, len(t) - n + 1))}


def _sentences(text: str) -> set:
    return {re.sub(r"\s+", "", s) for s in _SPLIT.split(text or "") if len(re.sub(r"\s+", "", s)) >= 20}


def compare(prev: str, cur: str) -> dict:
    """前期と今期の本文 → 点（純関数）。"""
    new_terms, severity, flag = {}, 0, False
    for term, w in SEVERE.items():
        a, b = (prev or "").count(term), (cur or "").count(term)
        if b > a:
            new_terms[term] = b - a
            severity += w * min(b - a, 3)
            if a == 0 and w >= FLAG_WEIGHT:
                flag = True
    g1, g2 = _grams(prev), _grams(cur)
    sim = len(g1 & g2) / len(g1 | g2) if (g1 | g2) else 1.0
    added = len(_sentences(cur) - _sentences(prev))
    return {"severity": severity, "new_terms": new_terms, "change_ratio": round(1 - sim, 4),
            "new_sentences": added, "flag": flag or severity >= FLAG_SEVERITY}


def build_rows(records: list[dict]) -> list[dict]:
    """会社ごとの新しい2期 → 表の行（2期そろった会社だけ）。"""
    by: dict = {}
    for r in records:
        by.setdefault(r["edinet_code"], []).append(r)
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for code, rs in sorted(by.items()):
        if len(rs) < 2:
            continue
        rs.sort(key=lambda x: str(x["fiscal_year_end"]), reverse=True)
        cur, prev = rs[0], rs[1]
        sc = compare(prev["raw_text"], cur["raw_text"])
        rows.append({"edinet_code": code, "sec_code": cur.get("sec_code"), "fy_latest": str(cur["fiscal_year_end"]),
                     "fy_prev": str(prev["fiscal_year_end"]), "doc_id": cur.get("doc_id"),
                     "severity": sc["severity"], "flag": sc["flag"], "change_ratio": sc["change_ratio"],
                     "new_sentences": sc["new_sentences"],
                     "new_terms": "、".join(f"{k}+{v}" for k, v in sorted(sc["new_terms"].items(), key=lambda x: -SEVERE[x[0]])),
                     "scored_at": now})
    return rows


def write(client, rows: list[dict]) -> int:
    from google.cloud import bigquery
    S = bigquery.SchemaField
    schema = [S("edinet_code", "STRING"), S("sec_code", "STRING"), S("fy_latest", "DATE"), S("fy_prev", "DATE"),
              S("doc_id", "STRING"), S("severity", "INT64"), S("flag", "BOOL"), S("change_ratio", "FLOAT64"),
              S("new_sentences", "INT64"), S("new_terms", "STRING"), S("scored_at", "TIMESTAMP")]
    buf = io.BytesIO("\n".join(json.dumps(r, ensure_ascii=False) for r in rows).encode("utf-8"))
    client.load_table_from_file(buf, f"{client.project}.{TABLE}", location="asia-northeast1", job_config=bigquery.LoadJobConfig(
        schema=schema, source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE)).result()
    return len(rows)


def run(cfg=None, dry: bool = False, client=None) -> dict:
    if client is None:
        from edinet_bq_client import EDINETBQClient
        client = EDINETBQClient().client
    recs = [dict(r.items()) for r in client.query(SQL.format(p=client.project), location="asia-northeast1").result()]
    rows = build_rows(recs)
    n = 0 if dry or not rows else write(client, rows)
    return {"companies_2y": len(rows), "flagged": sum(1 for r in rows if r["flag"]), "written": n}


ALERT_SQL = """
SELECT r.sec_code, c.name, r.fy_latest, r.severity, r.new_terms, r.change_ratio
FROM `{p}.edinet.risk_text_changes` r LEFT JOIN `{p}.edinet.company` c USING (edinet_code)
WHERE r.flag AND r.fy_latest >= DATE_SUB(CURRENT_DATE('Asia/Tokyo'), INTERVAL 400 DAY)
ORDER BY r.severity DESC, r.change_ratio DESC LIMIT 200
"""


def build_alert(cfg, results: dict, client=None) -> dict | None:
    """日次メールの注意項目（保有・スクリーナー通過の会社を先に）。表が無ければ None。"""
    try:
        if client is None:
            from edinet_bq_client import EDINETBQClient
            client = EDINETBQClient().client
        rows = [dict(r.items()) for r in client.query(ALERT_SQL.format(p=client.project), location="asia-northeast1").result()]
    except Exception as e:
        print(f"    ⚠️ 有報リスクの悪化を読めない: {type(e).__name__}")
        return None
    if not rows:
        return None
    watch = set()
    for c in ((results.get("screener") or {}).get("candidates") or []):
        if c.get("ticker"):
            watch.add(str(c["ticker"]).split(".")[0])
    for t in (results.get("holdings_tickers") or []):
        watch.add(str(t).split(".")[0])
    rows.sort(key=lambda r: (str(r.get("sec_code") or "")[:4] not in watch, -int(r["severity"] or 0)))
    lines = [f"{'★ ' if str(r.get('sec_code') or '')[:4] in watch else ''}{r.get('name') or '-'}（{str(r.get('sec_code') or '')[:4]}.T）"
             f": 新しく増えた語 {r['new_terms']}（期末 {r['fy_latest']}・記述の変化 {float(r['change_ratio']):.0%}）" for r in rows[:10]]
    return {"trigger": "risk_text_deterioration", "severity": "warning",
            "title": f"📄 有報「事業等のリスク」が悪化した会社 {len(rows)}社（前期の有報と比べて重い語が新しく出た・★は通過銘柄）",
            "body": "\n".join(lines)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args(argv)
    print(f"✅ risk_text_scorer: {json.dumps(run(dry=a.dry), ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
