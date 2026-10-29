# ==============================================================
# segment_en_mapper.py — 有報のセグメント別売上（英語の区分名）から、売上の比率つきで技術マップのタグを付ける
# ==============================================================
# 【入力】edinet.gov_segment_revenue（edinet_gov_text が有報の CSV から取る当期のセグメント別の外部顧客への売上高）。
#   区分名は edinet_gov_text.segment_name で読める形にし、data/taxonomy_segment_en_rules.json の規則で1区分1ノードへ。
# 【出力】taxonomy.company_taxonomy_map（confidence_type='gov_segment'）。関与度・revenue_share_est は
#   その会社の区分の売上の合計に対する比率（同じノードに当たった区分は足す）。地域・合計・その他の区分は使わない。
#   毎回作り直す：今回出なかった gov_segment の行は失効させる（十分な社数が取れたときだけ）。
# 【読む量】まとめ読み1本（gov_segment_revenue と company・数 MB）。
# 【使い方】python segment_en_mapper.py [--dry]
# ==============================================================
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

RULES_PATH = Path(__file__).parent / "data" / "taxonomy_segment_en_rules.json"
CONFIDENCE_TYPE = "gov_segment"
CONFIDENCE = 0.8          # 会社自身の開示した売上の区分。語の照合（本文）より確か
MIN_SHARE = 0.03
RETIRE_MIN_COMPANIES = 500

SQL = """
-- 拾い方を直して取り直した書類は同じ区分が2回入るので、同じ値は1つにする
SELECT DISTINCT LEFT(c.sec_code, 4) code, s.doc_id, s.fiscal_year_end, s.member, s.value
FROM `{p}.edinet.gov_segment_revenue` s
JOIN `{p}.edinet.company` c USING (edinet_code)
WHERE c.sec_code IS NOT NULL AND s.value > 0
QUALIFY DENSE_RANK() OVER (PARTITION BY s.edinet_code ORDER BY s.fiscal_year_end DESC, s.doc_id DESC) = 1
"""


def load_rules(path: Path = RULES_PATH) -> tuple[dict, list]:
    d = json.loads(path.read_text(encoding="utf-8"))
    rules = [(re.compile(r["pattern"], re.I), r.get("node"), bool(r.get("skip"))) for r in d.get("rules") or []]
    return d.get("exact") or {}, rules


def node_for(name: str, exact: dict, rules: list) -> tuple[str | None, bool]:
    """区分名 → (ノード, 事業ではない区分か)（純関数）。当たらなければ (None, False)。"""
    n = str(name or "").strip()
    if n in exact:
        return exact[n], exact[n] is None
    for pat, node, skip in rules:
        if pat.search(n):
            return (None, True) if skip else (node, False)
    return None, False


def build_rows(records: list[dict], exact: dict, rules: list, known: set | None = None,
               as_of: str | None = None) -> tuple[list[dict], dict]:
    """セグメントの行 → 対応表の行と集計（純関数）。"""
    from edinet_gov_text import segment_name
    as_of = as_of or date.today().isoformat()
    now = datetime.now(timezone.utc).isoformat()
    by_co: dict = {}
    stat = {"segments": 0, "mapped": 0, "skipped": 0, "unmapped": 0, "unmapped_names": {}}
    for r in records:
        name = segment_name(r["member"])
        node, skip = node_for(name, exact, rules)
        stat["segments"] += 1
        if skip:
            stat["skipped"] += 1
            continue
        co = by_co.setdefault(r["code"], {"total": 0.0, "nodes": {}, "names": {}})
        co["total"] += float(r["value"])
        if not node or (known is not None and node not in known):
            stat["unmapped"] += 1
            stat["unmapped_names"][name] = stat["unmapped_names"].get(name, 0) + 1
            continue
        stat["mapped"] += 1
        co["nodes"][node] = co["nodes"].get(node, 0.0) + float(r["value"])
        co["names"].setdefault(node, []).append(name)
    rows = []
    for code, co in sorted(by_co.items()):
        if co["total"] <= 0:
            continue
        for node, v in sorted(co["nodes"].items(), key=lambda x: -x[1]):
            share = round(v / co["total"], 4)
            if share < MIN_SHARE:
                continue
            rows.append({
                "map_id": f"{code}:{node}:{CONFIDENCE_TYPE}", "company_id": code, "node_id": node,
                "relevance_score": share, "confidence": CONFIDENCE, "revenue_share_est": share,
                "confidence_type": CONFIDENCE_TYPE, "evidence_source": "edinet_gov:segment_revenue",
                "rationale": f"有報のセグメント別売上: {'・'.join(co['names'][node])} {share:.1%}"[:500],
                "valid_from": as_of, "valid_to": None, "auto_derived": True, "verified_flag": False, "updated_at": now,
            })
    stat["companies"] = len({r["company_id"] for r in rows})
    return rows, stat


def run(cfg=None, dry: bool = False, client=None) -> dict:
    from taxonomy_loader import load_seed_batches, select_approved_nodes
    if client is None:
        from edinet_bq_client import EDINETBQClient
        client = EDINETBQClient().client
    known = {n["node_id"] for n in select_approved_nodes(load_seed_batches())[0]}
    exact, rules = load_rules()
    records = [dict(r.items()) for r in client.query(SQL.format(p=client.project), location="asia-northeast1").result()]
    rows, stat = build_rows(records, exact, rules, known)
    stat["top_unmapped"] = sorted(stat.pop("unmapped_names").items(), key=lambda x: -x[1])[:20]
    if dry or not rows:
        return {**stat, "written": 0}
    import taxonomy_company_mapper as tcm
    retire = (date.today() - timedelta(days=1)).isoformat() if stat["companies"] >= RETIRE_MIN_COMPANIES else None
    ok, n = tcm.upsert_to_bq(rows, cfg, confidence_type=CONFIDENCE_TYPE, retire_before=retire)
    return {**stat, "written": n if ok else 0, "ok": ok}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args(argv)
    try:
        import config as cfg
    except Exception:
        cfg = None
    print(f"✅ segment_en_mapper: {json.dumps(run(cfg, dry=a.dry), ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
