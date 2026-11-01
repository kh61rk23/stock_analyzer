# ==============================================================
# us_taxonomy_mapper.py — 米国上場会社に技術マップのノードを付ける（us.company_taxonomy_map）
# ==============================================================
# 【入力】
#   ① us.segment_financials（us_segments.py が SEC の Financial Statement Data Sets から取る、セグメント別の売上）。
#      直近の通期（qtrs=4）の BusinessSegments と ProductOrService の2つの軸。区分名は member_words で単語に分け、
#      data/taxonomy_us_segment_rules.json → 日本の data/taxonomy_segment_en_rules.json の順の規則で1区分1ノードへ。
#      読むのは us.segment_revenue_latest（会社ごとの直近の通期だけを畳んだ表）。元の表が更新されたときだけ作り直す
#      （get_table の更新時刻で判断・作り直しは約60MB、ふだんは数MB）。
#   ② us.company_profile の SIC 業種コード（data/taxonomy_us_sic_map.json：4桁 → ノード、無ければ上2桁 → ノード）。
# 【付け方】日本（segment_en_mapper の gov_segment）と同じく、会社の区分の売上の合計に対する比率で複数ノードを付ける
#   （confidence_type='us_segment'）。軸は2つのうち、ノードに当たった売上の比率が大きい方を使う。
#   区分で説明できない残り（ノードに当たらない区分・製品/サービスのような中身の分からない区分）は SIC のノードに
#   残りの比率で付ける（'us_sic'）。セグメントの無い会社は従来どおり SIC だけ（関与度 1.0）。
# 【出力】us.company_taxonomy_map（location=US。us の表と JOIN できるよう日本側の taxonomy とは分ける）。
#   列は日本の taxonomy.company_taxonomy_map と同じ＋item_no（会社の中の項目番号。売上の比率の大きい順）・items（項目数）。
#   company_id は米国のティッカー。毎回作り直す（load job の WRITE_TRUNCATE・無料）。
#   あわせて us.tax_node（seed のノード名・段階。日本の master.tax_node は asia-northeast1 で JOIN できないため）と
#   ビュー us.v_company_tags（日本の taxonomy.v_company_tags と同じ列）を用意する。
# 【使い方】python us_taxonomy_mapper.py [--dry]
# ==============================================================
from __future__ import annotations

import argparse
import io
import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

MAP_PATH = Path(__file__).parent / "data" / "taxonomy_us_sic_map.json"
US_RULES_PATH = Path(__file__).parent / "data" / "taxonomy_us_segment_rules.json"
TABLE = "us.company_taxonomy_map"
LATEST_TABLE = "us.segment_revenue_latest"
NODE_TABLE = "us.tax_node"
VIEW = "us.v_company_tags"
LOCATION = "US"
CONFIDENCE_TYPE = "us_sic"
CONFIDENCE = 0.5          # 業種コードだけの当たり。事業の中身を読んだ当たりより低くする
SEG_TYPE = "us_segment"
SEG_CONFIDENCE = 0.8      # 会社自身が開示した売上の区分
MIN_SHARE = 0.03
PARENT_TOL = 0.1          # 「製品」などの区分が具体的な区分の合計と1割以内で一致したら親（内訳の重複）とみなす

SQL = """
SELECT ticker, cik, sic, sic_description
FROM `{p}.us.company_profile`
WHERE ticker IS NOT NULL AND sic IS NOT NULL
"""

# 会社ごとに直近の通期の売上を、軸・区分ごとに1行。同じ値が後の提出に再掲されるので提出日の新しい行を採る。
# 会社の中で売上のタグが2つ以上あれば、軸ごとに合計の大きいタグ（いちばん網羅的な開示）だけを残す。
LATEST_SQL = """
CREATE OR REPLACE TABLE `{p}.us.segment_revenue_latest`
OPTIONS (description = '会社ごとの直近の通期のセグメント別売上（us_taxonomy_mapper が us.segment_financials から作る）') AS
WITH a AS (
  SELECT cik, axis, member, tag, period_end, val
  FROM `{p}.us.segment_financials`
  WHERE qtrs = 4 AND tag != 'OperatingIncomeLoss' AND axis IN ('BusinessSegments', 'ProductOrService')
    AND period_end >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 MONTH) AND val > 0
  QUALIFY ROW_NUMBER() OVER (PARTITION BY cik, axis, member, tag, period_end ORDER BY filed DESC) = 1
), b AS (
  SELECT * FROM a QUALIFY DENSE_RANK() OVER (PARTITION BY cik ORDER BY period_end DESC) = 1
), t AS (
  SELECT cik, axis, tag FROM b GROUP BY cik, axis, tag
  QUALIFY ROW_NUMBER() OVER (PARTITION BY cik, axis ORDER BY SUM(val) DESC, tag) = 1
)
SELECT b.cik, b.axis, b.member, b.period_end, b.val FROM b JOIN t USING (cik, axis, tag)
"""

READ_SEG_SQL = "SELECT cik, axis, member, period_end, val FROM `{p}.us.segment_revenue_latest`"

COLUMNS = [
    ("map_id", "STRING"), ("company_id", "STRING"), ("node_id", "STRING"), ("relevance_score", "FLOAT"),
    ("confidence", "FLOAT"), ("revenue_share_est", "FLOAT"), ("confidence_type", "STRING"),
    ("evidence_source", "STRING"), ("evidence_url", "STRING"), ("rationale", "STRING"), ("valid_from", "DATE"),
    ("valid_to", "DATE"), ("last_verified_at", "TIMESTAMP"), ("auto_derived", "BOOLEAN"),
    ("verified_flag", "BOOLEAN"), ("updated_at", "TIMESTAMP"), ("item_no", "INTEGER"), ("items", "INTEGER"),
]

NODE_COLUMNS = [("node_id", "STRING"), ("node_name_ja", "STRING"), ("node_name_en", "STRING"),
                ("node_level", "INTEGER"), ("level_name_ja", "STRING"), ("l1_name_ja", "STRING"),
                ("l2_name_ja", "STRING"), ("path_name_ja", "STRING")]
LEVEL_NAMES = {1: "大分類", 2: "中分類", 3: "小分類"}

# 日本の taxonomy.v_company_tags と同じ列。並びは売上の比率（us_segment）→ 業種（us_sic）、同じ型の中は関与度の高い順
VIEW_SQL = """
CREATE OR REPLACE VIEW `{p}.us.v_company_tags`
OPTIONS (description = '米国の会社ごとの技術ノードを「項目1・項目2…」で並べる。セグメントの売上の比率（us_segment）→業種（us_sic）の順') AS
SELECT m.company_id, m.item_no, m.items, m.node_id, n.node_name_ja, n.level_name_ja, n.l1_name_ja, n.l2_name_ja,
       n.path_name_ja, m.relevance_score, m.revenue_share_est, m.confidence_type, m.rationale
FROM `{p}.us.company_taxonomy_map` m LEFT JOIN `{p}.us.tax_node` n USING (node_id)
"""

# 米国の一次採点向け：ティッカーごとのノードと関与度（screener_universe.theme_value にそのまま渡せる形）。まとめて1本。
US_THEMES_SQL = """
SELECT company_id AS ticker, ARRAY_AGG(STRUCT(node_id, relevance_score AS relevance) ORDER BY item_no) AS themes
FROM `{p}.us.company_taxonomy_map`
WHERE relevance_score IS NOT NULL
GROUP BY company_id
"""


# ── 純関数 ────────────────────────────────────────────────

def load_map(path: Path = MAP_PATH) -> tuple[dict, dict]:
    d = json.loads(path.read_text(encoding="utf-8"))
    return d.get("sic_to_node") or {}, d.get("major_to_node") or {}


def node_for_sic(sic, by_code: dict, by_major: dict) -> str | None:
    s = str(sic or "").strip().zfill(4)
    if not s.isdigit() or s == "0000":
        return None
    return by_code.get(s) or by_major.get(s[:2])


def load_segment_rules(us_path: Path = US_RULES_PATH) -> dict:
    """米国の規則と日本の英語の規則を読む。{"us": [(pat, node, kind)], "jp": (exact, rules)}"""
    import segment_en_mapper as sm
    d = json.loads(us_path.read_text(encoding="utf-8"))
    us = []
    for r in d.get("rules") or []:
        kind = "skip" if r.get("skip") else ("node" if r.get("node") else "generic")
        us.append((re.compile(r["pattern"], re.I), r.get("node"), kind))
    return {"us_exact": d.get("exact") or {}, "us": us, "jp": sm.load_rules()}


def member_words(member: str) -> str:
    """XBRL の区分名 → 単語に分けた名前（純関数）。例 DataCenterHostingMember → Data Center Hosting、
    AIServices → AI Services、AmericasSegment → Americas。末尾の連番（AssetManagement1）も外す。"""
    m = str(member or "").split(":")[-1]
    m = re.sub(r"Member$", "", m)
    m = re.sub(r"(Reportable|Operating|Reporting)?Segments?$", "", m) or m
    m = re.sub(r"\d+$", "", m)
    m = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", m)
    m = re.sub(r"(?i)\band\b", "And", m)
    return re.sub(r"\s+", " ", m.replace("_", " ")).strip()


def classify_member(name: str, rules: dict) -> tuple[str, str | None]:
    """区分名 → (種類, ノード)（純関数）。種類は node / skip / generic（事業だが中身の分からない区分）/ none。"""
    n = str(name or "").strip()
    if not n:
        return "skip", None
    low = n.lower()
    if n in rules["us_exact"]:
        v = rules["us_exact"][n]
        return ("node", v) if v else ("skip", None)
    for pat, node, kind in rules["us"]:
        if pat.search(low):
            return kind, node
    import segment_en_mapper as sm
    node, skip = sm.node_for(n, *rules["jp"])
    if skip:
        return "skip", None
    return ("node", node) if node else ("none", None)


def segment_shares(records: list[dict], rules: dict, known: set | None = None) -> dict:
    """1社の区分の行 → その社の採用した軸と、ノードごとの売上比率（純関数）。

    records: [{"axis", "member", "val"}]。返り値 {"axis", "nodes": {node: share}, "names": {node: [区分名]},
    "residual": ノードに当たらない比率, "segments": 区分の数}。区分が無い・分母が0なら空の dict。
    """
    by_axis: dict = {}
    for r in records:
        by_axis.setdefault(r["axis"], []).append(r)
    best = None
    for axis, rows in sorted(by_axis.items()):
        items = []
        for r in rows:
            name = member_words(r["member"])
            kind, node = classify_member(name, rules)
            if kind == "node" and known is not None and node not in known:
                kind, node = "none", None
            if kind != "skip":
                items.append((name, kind, node, float(r["val"] or 0)))
        specific = sum(v for _, k, _, v in items if k in ("node", "none"))
        # 「製品」「サービス」のような区分が具体的な区分の合計と一致するなら親（内訳と重複）なので外す
        items = [x for x in items if not (x[1] == "generic" and specific > 0
                                          and abs(specific - x[3]) <= PARENT_TOL * x[3])]
        total = sum(v for *_, v in items)
        if total <= 0:
            continue
        nodes: dict = {}
        names: dict = {}
        for name, kind, node, v in items:
            if kind == "node":
                nodes[node] = nodes.get(node, 0.0) + v / total
                names.setdefault(node, []).append(name)
        mapped = sum(nodes.values())
        cand = {"axis": axis, "nodes": nodes, "names": names, "residual": max(0.0, 1.0 - mapped),
                "segments": len(items), "mapped": mapped}
        if best is None or (mapped, axis == "BusinessSegments") > (best["mapped"], best["axis"] == "BusinessSegments"):
            best = cand
    return best or {}


def build_rows(profiles: list[dict], by_code: dict, by_major: dict, known_nodes: set | None = None,
               as_of: str | None = None, segments: dict | None = None, rules: dict | None = None) -> list[dict]:
    """company_profile の行（＋cik ごとのセグメントの行）→ 対応表の行（純関数）。

    segments: {cik(int): [{"axis", "member", "val"}]}。無い会社は SIC の1ノード・関与度 1.0。
    ある会社は区分の売上比率で複数ノード（us_segment）＋ 説明できない残りを SIC のノードへ（us_sic）。
    """
    as_of = as_of or date.today().isoformat()
    now = datetime.now(timezone.utc).isoformat()
    out, seen = [], set()
    seg_cache: dict = {}
    for p in profiles:
        t = str(p.get("ticker") or "").strip()
        if not t or t in seen:
            continue
        sic_node = node_for_sic(p.get("sic"), by_code, by_major)
        if known_nodes is not None and sic_node not in known_nodes:
            sic_node = None
        try:
            cik = int(p.get("cik"))
        except (TypeError, ValueError):
            cik = None
        share = {}
        if segments and rules and cik in segments:
            if cik not in seg_cache:
                seg_cache[cik] = segment_shares(segments[cik], rules, known_nodes)
            share = seg_cache[cik]
        url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={p.get('cik')}"
        rows = []
        for node, s in sorted((share.get("nodes") or {}).items(), key=lambda x: -x[1]):
            s = round(s, 4)
            if s < MIN_SHARE:
                continue
            rows.append({
                "map_id": f"{t}:{node}:{SEG_TYPE}", "company_id": t, "node_id": node, "relevance_score": s,
                "confidence": SEG_CONFIDENCE, "revenue_share_est": s, "confidence_type": SEG_TYPE,
                "evidence_source": f"sec_segment:{share['axis']}", "evidence_url": url,
                "rationale": f"SEC のセグメント別売上（{share['axis']}）: {'・'.join(share['names'][node])} {s:.1%}"[:500],
            })
        seg_nodes = {r["node_id"] for r in rows}
        if sic_node and sic_node not in seg_nodes:
            residual = round(share["residual"], 4) if share else None
            if residual is None or residual >= MIN_SHARE:
                rows.append({
                    "map_id": f"{t}:{sic_node}:{CONFIDENCE_TYPE}", "company_id": t, "node_id": sic_node,
                    "relevance_score": 1.0 if residual is None else residual, "confidence": CONFIDENCE,
                    "revenue_share_est": residual, "confidence_type": CONFIDENCE_TYPE,
                    "evidence_source": f"sec_sic:{str(p.get('sic')).zfill(4)}", "evidence_url": url,
                    "rationale": (f"SIC {str(p.get('sic')).zfill(4)} {p.get('sic_description') or ''}"
                                  + ("" if residual is None else f"（セグメントで説明できない残り {residual:.1%}）"))[:500],
                })
        if not rows:
            continue
        seen.add(t)
        for i, r in enumerate(rows, 1):
            r.update({"valid_from": as_of, "valid_to": None, "last_verified_at": None, "auto_derived": True,
                      "verified_flag": False, "updated_at": now, "item_no": i, "items": len(rows)})
        out.extend(rows)
    return out


def summarize(rows: list[dict]) -> dict:
    """社数・1社あたりの項目数・型ごとの社数（純関数）。"""
    by_co: dict = {}
    for r in rows:
        by_co.setdefault(r["company_id"], set()).add(r["confidence_type"])
    n = len(by_co)
    types: dict = {}
    for ts in by_co.values():
        key = "+".join(sorted(ts))
        types[key] = types.get(key, 0) + 1
    return {"companies": n, "rows": len(rows), "items_per_company": round(len(rows) / n, 2) if n else 0,
            "multi_item_companies": sum(1 for r in rows if r.get("item_no") == 2),
            "companies_by_type": dict(sorted(types.items())), "nodes": len({r["node_id"] for r in rows})}


def node_rows(nodes: list[dict]) -> list[dict]:
    """承認済み seed のノード → us.tax_node の行（純関数）。上の段の名前と経路を付ける。"""
    by_id = {n["node_id"]: n for n in nodes}
    out = []
    for n in nodes:
        chain, cur, guard = [], n, 0
        while cur and guard < 10:
            chain.append(cur)
            cur = by_id.get(cur.get("parent_id"))
            guard += 1
        chain.reverse()
        lv = int(n.get("node_level") or len(chain))
        out.append({"node_id": n["node_id"], "node_name_ja": n.get("node_name_ja"), "node_name_en": n.get("node_name_en"),
                    "node_level": lv, "level_name_ja": LEVEL_NAMES.get(lv, f"第{lv}段"),
                    "l1_name_ja": chain[0].get("node_name_ja") if chain else None,
                    "l2_name_ja": chain[1].get("node_name_ja") if len(chain) > 1 else None,
                    "path_name_ja": " > ".join(str(c.get("node_name_ja") or c["node_id"]) for c in chain)})
    return out


# ── 入出力 ────────────────────────────────────────────────

def _load(client, table: str, rows: list[dict], columns: list) -> int:
    from google.cloud import bigquery
    schema = [bigquery.SchemaField(n, t) for n, t in columns]
    buf = io.BytesIO("\n".join(json.dumps(r, ensure_ascii=False, default=str) for r in rows).encode("utf-8"))
    client.load_table_from_file(buf, f"{client.project}.{table}", location=LOCATION, job_config=bigquery.LoadJobConfig(
        schema=schema, source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE)).result()
    return len(rows)


def write(client, rows: list[dict]) -> int:
    return _load(client, TABLE, rows, COLUMNS)


def refresh_latest(client) -> bool:
    """us.segment_revenue_latest を元の表より古いときだけ作り直す（True=作り直した）。更新時刻は get_table（無料）。"""
    from google.api_core.exceptions import NotFound
    src = client.get_table(f"{client.project}.us.segment_financials")
    try:
        dst = client.get_table(f"{client.project}.{LATEST_TABLE}")
        if dst.modified and src.modified and dst.modified >= src.modified:
            return False
    except NotFound:
        pass
    client.query(LATEST_SQL.format(p=client.project), location=LOCATION).result()
    return True


def fetch_segments(client) -> dict:
    """{cik: [{"axis", "member", "val"}]}（まとめて1本）。"""
    out: dict = {}
    for r in client.query(READ_SEG_SQL.format(p=client.project), location=LOCATION).result():
        out.setdefault(int(r["cik"]), []).append({"axis": r["axis"], "member": r["member"], "val": r["val"]})
    return out


def ensure_nodes_and_view(client, nodes: list[dict]) -> None:
    _load(client, NODE_TABLE, node_rows(nodes), NODE_COLUMNS)
    client.query(VIEW_SQL.format(p=client.project), location=LOCATION).result()


def run(cfg=None, dry: bool = False, client=None) -> dict:
    from taxonomy_loader import load_seed_batches, select_approved_nodes
    if client is None:
        from edinet_bq_client import EDINETBQClient
        client = EDINETBQClient().client
    nodes = select_approved_nodes(load_seed_batches())[0]
    known = {n["node_id"] for n in nodes}
    profiles = [dict(r.items()) for r in client.query(SQL.format(p=client.project), location=LOCATION).result()]
    by_code, by_major = load_map()
    rebuilt = False if dry else refresh_latest(client)
    segments = fetch_segments(client)
    rules = load_segment_rules()
    rows = build_rows(profiles, by_code, by_major, known, segments=segments, rules=rules)
    info = {"profiles": len(profiles), "segment_companies": len(segments), "latest_rebuilt": rebuilt, **summarize(rows)}
    info["top_unmapped"] = top_unmapped(segments, rules, known)
    if not dry:
        info["written"] = write(client, rows)
        ensure_nodes_and_view(client, nodes)
    return info


def top_unmapped(segments: dict, rules: dict, known: set, n: int = 25) -> list:
    """ノードに当たらなかった区分名を社数の多い順に（規則を足すときの手がかり）。"""
    cnt: dict = {}
    for rows in segments.values():
        for name in {member_words(r["member"]) for r in rows}:
            kind, node = classify_member(name, rules)
            if kind == "none" or (kind == "node" and node not in known):
                cnt[name] = cnt.get(name, 0) + 1
    return sorted(cnt.items(), key=lambda x: -x[1])[:n]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="BQ に書かない（us.segment_revenue_latest が無ければ先に作る）")
    a = ap.parse_args(argv)
    print(f"✅ us_taxonomy_mapper: {json.dumps(run(dry=a.dry), ensure_ascii=False, default=str)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
