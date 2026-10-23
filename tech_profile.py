# ==============================================================
# tech_profile.py — 会社ごとの技術ノード（技術マップ）を分析の各所へ渡す
# ==============================================================
# 【中身】taxonomy.company_taxonomy_map の有効な行を、会社×ノードで1行に絞る（verified → edinet（セグメント経路）
#   → その他（text_match 等）の順。screener_universe の theme 観点と同じ選び方）。ノードの名前・種類・サプライチェーン上の
#   位置は taxonomy.taxonomy_node から、熱度（前日までの node_hotness 履歴の最新日）は screener_universe.load_hotness から。
# 【使い手】
#   ・view taxonomy.company_tech_profile（ensure_view）… SQL で会社の技術ノードを引く入口
#   ・agent_packet_exporter … 審議パケットの「⑬ 技術ノード」節（packet_lines）
#   ・stock_analyzer_main → results["tech_map"]（digest）→ alert_notifier の「🧭 技術ノードの熱度」節
# 【費用】digest は候補銘柄ぶんを1クエリ、packet_lines は1社1クエリ（審議は1日数件）。ループ内で BQ を引かない。
# ==============================================================
from __future__ import annotations

from datetime import date

VIEW = "company_tech_profile"

BEST_ROWS = """
SELECT m.company_id, m.node_id, m.relevance_score, m.confidence_type, m.verified_flag, m.rationale,
       n.node_name_ja, n.node_type, n.supply_chain_pos, n.parent_id
FROM `{p}.taxonomy.company_taxonomy_map` m
LEFT JOIN `{p}.taxonomy.taxonomy_node` n USING (node_id)
WHERE m.relevance_score IS NOT NULL AND (m.valid_to IS NULL OR m.valid_to >= CURRENT_DATE('Asia/Tokyo'))
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY m.company_id, m.node_id
  ORDER BY CASE WHEN m.verified_flag THEN 0 WHEN m.confidence_type = 'edinet' THEN 1 ELSE 2 END, m.updated_at DESC) = 1
"""


def ensure_view(client) -> None:
    client.query(f"CREATE OR REPLACE VIEW `{client.project}.taxonomy.{VIEW}` AS {BEST_ROWS.format(p=client.project)}",
                 location="asia-northeast1").result()


def fetch_profiles(client, codes: list[str]) -> dict:
    """{会社コード4桁: [ノードの dict（関与度の高い順）]}。codes が空なら {}。"""
    from google.cloud import bigquery
    codes = sorted({str(c).split(".")[0] for c in codes if c})
    if not codes:
        return {}
    jc = bigquery.QueryJobConfig(query_parameters=[bigquery.ArrayQueryParameter("codes", "STRING", codes)])
    sql = f"SELECT * FROM ({BEST_ROWS.format(p=client.project)}) WHERE company_id IN UNNEST(@codes)"
    out: dict = {}
    for r in client.query(sql, job_config=jc, location="asia-northeast1").result():
        out.setdefault(r.company_id, []).append(dict(r.items()))
    for v in out.values():
        v.sort(key=lambda x: -(x.get("relevance_score") or 0))
    return out


def annotate(profile: list[dict], hotness: dict, leading: list) -> list[dict]:
    """ノードに熱度（0-100）と「先行だけ点灯」の印を付ける（純関数）。"""
    lead = set(leading or [])
    out = []
    for p in profile or []:
        h = (hotness or {}).get(p["node_id"]) or {}
        score = h.get("combined") if isinstance(h, dict) else h   # 熱度は combined（準先行＋先行・0〜100）
        out.append({**p, "hotness": round(float(score), 1) if isinstance(score, (int, float)) else None,
                    "leading_only": p["node_id"] in lead})
    return out


def digest(candidates: list[dict], profiles: dict, hotness: dict, leading: list,
           top_nodes: int = 5, per_node: int = 5) -> dict:
    """スクリーナー候補 × 技術ノード → 熱度の高いノードと、そこに属する候補（純関数）。

    Returns: {"nodes": [{node_id, name, hotness, leading_only, candidates: [{ticker, name, relevance}]}],
              "covered": 技術ノードが付いた候補の数, "candidates": 候補の数}
    """
    by_node: dict = {}
    covered = 0
    for c in candidates or []:
        code = str(c.get("ticker", "")).split(".")[0]
        prof = annotate(profiles.get(code) or [], hotness, leading)
        if prof:
            covered += 1
        for p in prof:
            e = by_node.setdefault(p["node_id"], {"node_id": p["node_id"], "name": p.get("node_name_ja") or p["node_id"],
                                                 "hotness": p["hotness"], "leading_only": p["leading_only"],
                                                 "candidates": []})
            e["candidates"].append({"ticker": c.get("ticker"), "name": c.get("name", ""),
                                    "relevance": round(float(p.get("relevance_score") or 0), 2)})
    nodes = sorted(by_node.values(), key=lambda e: (-(e["hotness"] or -1), -len(e["candidates"])))
    for e in nodes:
        e["candidates"] = sorted(e["candidates"], key=lambda x: -x["relevance"])[:per_node]
    return {"nodes": nodes[:top_nodes], "covered": covered, "candidates": len(candidates or [])}


def run_digest(cfg, results: dict, client=None) -> dict:
    """results のスクリーナー通過銘柄（日本株）で digest を作る。"""
    import screener_universe
    cands = [c for c in ((results.get("screener") or {}).get("candidates") or [])
             if isinstance(c, dict) and c.get("pass_filters")]
    hotness, leading = screener_universe.load_hotness(cfg)
    if client is None:
        import state_store
        client = state_store.make_client(cfg)
    ensure_view(client)
    profiles = fetch_profiles(client, [c.get("ticker") for c in cands])
    return {**digest(cands, profiles, hotness, leading,
                     int(getattr(cfg, "TECH_MAP_DIGEST_NODES", 5)), int(getattr(cfg, "TECH_MAP_DIGEST_PER_NODE", 5))),
            "as_of": date.today().isoformat()}


def packet_lines(cfg, ticker: str, client=None) -> list[str]:
    """審議パケットの「⑬ 技術ノード」節。データが無ければその旨を出す（欠損を隠さない）。"""
    code = str(ticker).split(".")[0]
    out = ["", "## ⑬ 技術ノード（技術マップ）", "*鮮度: BQ taxonomy.company_taxonomy_map（週次の照合）・熱度は前日までの履歴*", ""]
    try:
        import screener_universe
        if client is None:
            import state_store
            client = state_store.make_client(cfg)
        prof = annotate(fetch_profiles(client, [code]).get(code) or [], *screener_universe.load_hotness(cfg))
    except Exception as e:
        return out + [f"- ⚠️ 技術ノードを読めない（{type(e).__name__}）: **データなし**"]
    if not prof:
        return out + ["- この会社に付いた技術ノードは無い（**データなし**。照合の材料＝有報の本文・セグメント名に語が出ていない）"]
    out += ["| ノード | 種類 | 位置 | 関与度 | 熱度 | 付け方 | 根拠 |", "|---|---|---|---|---|---|---|"]
    for p in prof[:10]:
        how = "確認済み" if p.get("verified_flag") else (p.get("confidence_type") or "")
        heat = "-" if p["hotness"] is None else f"{p['hotness']:.0f}" + ("（先行のみ点灯）" if p["leading_only"] else "")
        out.append(f"| {p.get('node_name_ja') or p['node_id']} | {p.get('node_type') or ''} | {p.get('supply_chain_pos') or ''} "
                   f"| {float(p.get('relevance_score') or 0):.2f} | {heat} | {how} | {(p.get('rationale') or '')[:60]} |")
    out.append("- 付け方 text_match は語の照合（人の確認前の候補）。関与度・熱度の重みは審議で判断する")
    return out
