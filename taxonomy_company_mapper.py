# ==============================================================
# taxonomy_company_mapper.py — T-2A: EDINETセグメント上書き（R2-3）
# ==============================================================
# 【役割】edinet.segments（＋segment_finance の売上比率）を segment_mapper
#   （sentence-transformers→Gemini fallback）で33業種に写像し、
#   size_tier_classifier の 業種→taxonomy_node 橋渡しを経由して
#   taxonomy.company_taxonomy_map に **企業レベル** の行を書き込む。
#   confidence_type="edinet"（confidence=0.7）で、cluster_derived の
#   draft層（sector擬似企業・confidence=0.3）を企業単位で上書きする（決定2: C→A）。
# 【前提】EDINETセグメントの蓄積（R0-1のEDINET安定化）。データが空でも
#   安全にスキップし exit 0（次回蓄積後に再実行すればよい）。
#
# 【語の照合経路（confidence_type="text_match"）】
#   業種経由の経路は _SECTOR_TO_TAXONOMY に載る8業種・約30ノードにしか届かない。
#   それを補うため、有報の「事業の内容」（edinet.text_analysis）とセグメント名
#   （edinet.segments＋segment_finance の売上比率）を、承認済み seed の
#   ノード名・説明から作った語と照合し、会社→ノードの候補行を作る。
#   - 語の作り方・除外語・しきい値は data/taxonomy_keyword_rules.json が正本
#   - 採用条件: セグメント名に語がある、または本文に語が min_text_hits 回以上
#   - 根拠（どの語がどの文書に何回／どのセグメントで売上何%）を rationale に残す
#   - confidence=0.5・verified_flag=False（人の確認前の候補）。読み手は
#     verified → edinet → その他 の順で1行を選ぶので、セグメント経路の行を上書きしない
#   - 今回の照合で出なかった過去の text_match 行は valid_to を前日にして失効させる
#     （除外語を足したときに誤爆行が残り続けないようにするため）
# 使い方: python3 taxonomy_company_mapper.py --dry-run / （本実行）
# ==============================================================

from __future__ import annotations

import json
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

KEYWORD_RULES_PATH = REPO / "data" / "taxonomy_keyword_rules.json"
TEXT_MATCH_TYPE = "text_match"
TEXT_MATCH_CONFIDENCE = 0.5
TEXT_MATCH_SOURCE = "edinet_text_analysis+segments:keyword"


# ── 純粋ロジック（pytest対象・mapper/橋渡しは注入可能） ────────

def build_company_node_rows(
    segments_by_company: dict,
    map_segment_fn,
    sector_to_nodes_fn,
    as_of: str = None,
    min_sim: float = 0.0,
) -> list[dict]:
    """
    企業×セグメント → 企業×taxonomy_node の行を構築する。

    Args:
        segments_by_company: {company_code: [{"segment_name": str, "share": float}]}
                             share は売上比率（欠損時は等分）
        map_segment_fn:      segment_name -> {"sector_code": str, "similarity_score": float} | None
        sector_to_nodes_fn:  sector_code -> [node_id, ...]
        min_sim:             これ未満の類似度の当て方は使わない。業種の配下のノードを全部付けるので、
                             当て方が弱いと無関係なノードがまとめて付く（FPD製造装置 sim=0.57 → イメージセンサー等）
    Returns:
        taxonomy.company_taxonomy_map 実スキーマの行リスト
    """
    as_of = as_of or date.today().isoformat()
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for company, segments in segments_by_company.items():
        if not segments:
            continue
        # share欠損は等分
        n_missing = sum(1 for s in segments if not s.get("share"))
        known = sum(float(s.get("share") or 0) for s in segments)
        default_share = max(0.0, (1.0 - known)) / n_missing if n_missing else 0.0

        node_score: dict[str, float] = {}
        node_share: dict[str, float] = {}
        rationale: dict[str, list] = {}
        for seg in segments:
            name = (seg.get("segment_name") or "").strip()
            if not name:
                continue
            share = float(seg.get("share") or default_share)
            mapped = map_segment_fn(name)
            if not mapped or not mapped.get("sector_code"):
                continue
            sim = float(mapped.get("similarity_score", 0.5) or 0.5)
            if sim < min_sim:
                continue
            for node_id in (sector_to_nodes_fn(mapped["sector_code"]) or []):
                node_score[node_id] = node_score.get(node_id, 0.0) + share * sim
                node_share[node_id] = node_share.get(node_id, 0.0) + share
                rationale.setdefault(node_id, []).append(
                    f"{name}({share:.0%},sim={sim:.2f})")
        total = sum(node_score.values())
        if total <= 0:
            continue
        for node_id, sc in node_score.items():
            rows.append({
                "map_id":            f"{company}:{node_id}:{as_of}",
                "company_id":        str(company),
                "node_id":           node_id,
                "relevance_score":   round(sc / total, 4),
                "confidence":        0.7,
                "revenue_share_est": round(min(1.0, node_share[node_id]), 4),
                "confidence_type":   "edinet",
                "evidence_source":   "edinet_segments+segment_mapper",
                "rationale":         " / ".join(rationale[node_id])[:500],
                "valid_from":        as_of,
                "auto_derived":      True,
                "verified_flag":     False,
                "updated_at":        now,
            })
    return rows


# ── 語の照合（純粋ロジック・pytest対象） ──────────────────────

_SPLIT_RE = re.compile(r"[・/／、,，\s]+")
_PAREN_RE = re.compile(r"[（(]([^）)]*)[）)]")
_ASCII_RE = re.compile(r"^[\x21-\x7e]+$")
# 説明文の言い回し（助詞・「等」「包含」）を含む断片は語として使わない
_DESC_PHRASE_RE = re.compile(r"[等のをにでと]|する|包含")


def _norm(text) -> str:
    """全角英数・記号を半角に寄せる（有報本文は「ＳｉＣ」「５Ｇ」のような全角が混じる）。"""
    return unicodedata.normalize("NFKC", str(text or ""))


def load_keyword_rules(path: Path = KEYWORD_RULES_PATH) -> dict:
    """照合ルール（除外語・しきい値）を読む。無い・壊れているときは空ルール。"""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  ⚠️ 照合ルール読込失敗（除外語なしで続行）: {str(e)[:60]}")
        return {}


def _valid_term(term: str, min_ja_len: int, min_ascii_len: int) -> bool:
    if not term:
        return False
    if _ASCII_RE.match(term):
        # 「3D」「2.5D」のような記号・数字主体の語は誤爆しかしないので外す
        letters = sum(1 for ch in term if ch.isalpha())
        return len(term) >= min_ascii_len and letters >= 2
    return len(term) >= min_ja_len


def build_node_terms(nodes: list[dict], rules: dict = None) -> dict:
    """
    node_id → 照合語のリスト（長い順）。

    語の出どころ（決定的）:
      1. node_name_ja 全体（括弧注記を除いたもの）と、それを ・ / 空白 で割った各部分
      2. node_name_ja の括弧内（「HBM(広帯域メモリ)」の「広帯域メモリ」）
      3. description の先頭節（最初の括弧より前）を ・ / 、 で割った各部分。
         助詞や「等」「包含」を含む部分は説明文の言い回しなので使わない
    除外: rules["exclude_terms"]（全ノード共通）と rules["exclude_terms_by_node"][node_id]。
    """
    rules = rules or {}
    min_ja = int(rules.get("min_ja_term_len", 2))
    min_ascii = int(rules.get("min_ascii_term_len", 3))
    min_desc = int(rules.get("min_desc_term_len", 3))
    excl_all = {_norm(t) for t in rules.get("exclude_terms", [])}
    excl_node = {k: {_norm(t) for t in v}
                 for k, v in (rules.get("exclude_terms_by_node") or {}).items()}
    use_desc = bool(rules.get("use_description_terms", True))

    out: dict = {}
    for n in nodes:
        nid = n.get("node_id")
        if not nid:
            continue
        cands = []          # (語, 最小長)
        ja = _norm(n.get("node_name_ja"))
        base = _PAREN_RE.sub("", ja).strip()
        cands.append((base, min_ja))
        cands.extend((t, min_ja) for t in _SPLIT_RE.split(base))
        for inner in _PAREN_RE.findall(ja):
            cands.extend((t, min_ja) for t in _SPLIT_RE.split(inner))
        if use_desc:
            desc = _norm(n.get("description"))
            head = re.split(r"[（(]", desc, maxsplit=1)[0]
            for part in _SPLIT_RE.split(head):
                if _DESC_PHRASE_RE.search(part):
                    continue
                cands.append((part, max(min_ja, min_desc)))
        terms = set()
        bad = excl_all | excl_node.get(nid, set())
        for c, min_len in cands:
            c = c.strip()
            if c in bad or not _valid_term(c, min_len, min_ascii):
                continue
            terms.add(c)
        if terms:
            out[nid] = sorted(terms, key=lambda t: (-len(t), t))
    return out


def _compile_terms(terms: list[str]):
    """長い語を先に試す非重複の照合パターン。

    英字語は前後が英数字でないことを要求し、大文字小文字も区別する（頭字語が
    「Sdn. Bhd.」のような社名の一部に一致しないようにするため）。
    """
    alts = []
    for t in sorted(terms, key=lambda t: (-len(t), t)):
        esc = re.escape(t)
        if _ASCII_RE.match(t):
            alts.append(rf"(?<![A-Za-z0-9]){esc}(?![A-Za-z0-9])")
        else:
            alts.append(esc)
    return re.compile("|".join(alts)) if alts else None


def mask_phrases(text: str, phrases) -> str:
    """語の境目をまたいで誤って一致する言い回し（「海外食料品」の「外食」等）を空白で潰す。"""
    t = _norm(text)
    for ph in phrases or ():
        ph = _norm(ph)
        if ph:
            t = t.replace(ph, " " * len(ph))
    return t


def count_terms(text: str, pattern) -> dict:
    """本文中の語の出現回数 {語: 回数}（重なる語は長い方だけ数える）。"""
    counts: dict = {}
    if pattern is None or not text:
        return counts
    for m in pattern.finditer(_norm(text)):
        key = m.group(0)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _segment_shares(segments: list[dict]) -> list[tuple[str, float]]:
    """[(セグメント名, 売上比率)]。比率欠損は残りを等分（build_company_node_rows と同じ扱い）。"""
    segs = [s for s in (segments or []) if (s.get("segment_name") or "").strip()]
    n_missing = sum(1 for s in segs if not s.get("share"))
    known = sum(float(s.get("share") or 0) for s in segs)
    default = max(0.0, 1.0 - known) / n_missing if n_missing else 0.0
    return [(_norm(s["segment_name"]).strip(), float(s.get("share") or default))
            for s in segs]


def match_company_nodes(
    texts_by_company: dict,
    segments_by_company: dict,
    node_terms: dict,
    min_text_hits: int = 3,
    min_single_term_hits: int = 2,
    ignore_phrases=(),
) -> dict:
    """
    会社ごとに、各ノードの語が本文・セグメント名にどれだけ現れるかを数える。

    Args:
        texts_by_company:    {company_code: {section_type: raw_text}}
        segments_by_company: {company_code: [{"segment_name": str, "share": float|None}]}
        node_terms:          build_node_terms の出力
        min_text_hits:       セグメント名に出ないとき、本文で必要な出現回数の合計
        min_single_term_hits: 同じく、どれか1語が必要な出現回数（商社の取扱品目の羅列の
                             ように、別々の語が1回ずつ並ぶだけの文を拾わないため）
        ignore_phrases:      照合前に本文・セグメント名から潰す言い回し（mask_phrases）
    Returns:
        {company_code: {node_id: {"text_hits": int, "text_terms": {section: {語: 回数}},
                                  "seg_share": float, "seg_hits": [(セグメント名, 比率, 語)]}}}
        採用条件（セグメント名ヒット、または本文で合計 min_text_hits 回以上かつ
        1語が min_single_term_hits 回以上）を満たすものだけ。
    """
    patterns = {nid: _compile_terms(terms) for nid, terms in node_terms.items()}
    companies = {str(c) for c in (set(texts_by_company) | set(segments_by_company))
                 if c not in (None, "", "None")}
    out: dict = {}
    for company in sorted(companies):
        sections = {sec: mask_phrases(raw, ignore_phrases)
                    for sec, raw in (texts_by_company.get(company) or {}).items()}
        segs = [(name, mask_phrases(name, ignore_phrases), share) for name, share in
                _segment_shares(segments_by_company.get(company) or [])]
        found: dict = {}
        for nid, pat in patterns.items():
            if pat is None:
                continue
            text_terms, total, best = {}, 0, 0
            per_term: dict = {}
            for sec, raw in sections.items():
                c = count_terms(raw, pat)
                if c:
                    text_terms[sec] = c
                    total += sum(c.values())
                    for t, k in c.items():
                        per_term[t] = per_term.get(t, 0) + k
            best = max(per_term.values()) if per_term else 0
            seg_hits = []
            for name, masked, share in segs:
                m = pat.search(masked)
                if m:
                    seg_hits.append((name, share, m.group(0)))
            seg_share = min(1.0, sum(s for _, s, _ in seg_hits))
            if seg_hits or (total >= min_text_hits and best >= min_single_term_hits):
                found[nid] = {"text_hits": total, "text_terms": text_terms,
                              "seg_share": round(seg_share, 4), "seg_hits": seg_hits}
        if found:
            out[company] = found
    return out


def _rationale(ev: dict) -> str:
    parts = []
    for name, share, term in ev.get("seg_hits") or []:
        parts.append(f"seg:{name}({share:.0%})[{term}]")
    for sec, c in (ev.get("text_terms") or {}).items():
        terms = ", ".join(f"{t}×{n}" for t, n in
                          sorted(c.items(), key=lambda kv: (-kv[1], kv[0])))
        parts.append(f"text:{sec} {terms}")
    return " / ".join(parts)[:500]


def build_text_match_rows(matches: dict, as_of: str = None,
                          text_hit_cap: int = 10) -> list[dict]:
    """
    match_company_nodes の結果 → company_taxonomy_map の行（confidence_type=text_match）。

    関与度の素点 = セグメント売上比率 + 0.5 × min(本文出現回数, cap)/cap。
    会社内で合計1に正規化する（セグメント経路の行と同じ尺度にそろえる）。
    """
    as_of = as_of or date.today().isoformat()
    now = datetime.now(timezone.utc).isoformat()
    cap = max(1, int(text_hit_cap))
    rows = []
    for company, nodes in matches.items():
        raw = {nid: ev["seg_share"] + 0.5 * min(ev["text_hits"], cap) / cap
               for nid, ev in nodes.items()}
        total = sum(raw.values())
        if total <= 0:
            continue
        for nid, ev in nodes.items():
            rows.append({
                "map_id":            f"{company}:{nid}:{TEXT_MATCH_TYPE}",
                "company_id":        str(company),
                "node_id":           nid,
                "relevance_score":   round(raw[nid] / total, 4),
                "confidence":        TEXT_MATCH_CONFIDENCE,
                "revenue_share_est": ev["seg_share"] if ev["seg_hits"] else None,
                "confidence_type":   TEXT_MATCH_TYPE,
                "evidence_source":   TEXT_MATCH_SOURCE,
                "rationale":         _rationale(ev),
                "valid_from":        as_of,
                "valid_to":          None,
                "auto_derived":      True,
                "verified_flag":     False,
                "updated_at":        now,
            })
    return rows


def coverage_summary(rows: list[dict]) -> dict:
    """行の被覆（社数・ノード数・行数）。ログと見積もり用。"""
    return {"rows": len(rows),
            "companies": len({r["company_id"] for r in rows}),
            "nodes": len({r["node_id"] for r in rows})}


def company_key(value) -> str:
    """会社のキー（証券コード4桁。数字3桁＋数字か英字）。空・NaN・形の違う値は ""（その行は使わない）。"""
    if value is None or (isinstance(value, float) and value != value):
        return ""
    v = str(value).strip().upper()
    return v if re.fullmatch(r"[0-9]{3}[0-9A-Z]", v) else ""


# ── I/O層（BQ・segment_mapper） ──────────────────────────────

def fetch_segments_from_bq(cfg) -> dict:
    """edinet.segments × segment_finance(revenue_ratio) を企業別に取得。空なら {}。

    company_code が空の行（英数字の証券コードに対応する前に書かれた行など）は、segment_id の先頭の
    EDINET コードから edinet.company の証券コードを引き直す。引けない行は使わない
    （空のまま str() すると "nan" という1社に複数の会社がまとまってしまう）。"""
    try:
        from edinet_bq_client import EDINETBQClient
        bq = EDINETBQClient()
        project = (
            str(getattr(cfg, "BQ_PROJECT_ID", "") or "").strip()
            or bq.project
        )
        sql = f"""
        WITH s AS (
          SELECT COALESCE(s.company_code, LEFT(c.sec_code, 4)) AS company_code, s.segment_id, s.segment_name
          FROM `{project}.edinet.segments` s
          LEFT JOIN `{project}.edinet.company` c
            ON s.company_code IS NULL AND c.edinet_code = REGEXP_EXTRACT(s.segment_id, r'^(E[0-9]{{5}})')
          WHERE s.segment_name IS NOT NULL AND s.segment_name != ''
        )
        SELECT s.company_code, s.segment_name,
               MAX(f.revenue_ratio) AS revenue_ratio
        FROM s
        LEFT JOIN `{project}.edinet.segment_finance` f
          ON f.segment_id = s.segment_id
        WHERE REGEXP_CONTAINS(IFNULL(s.company_code, ''), r'^[0-9A-Z]{{4}}$')
        GROUP BY s.company_code, s.segment_name
        """
        df = bq.query_to_df(sql)
        out: dict = {}
        for _, r in df.iterrows():
            code = company_key(r.get("company_code"))
            if not code:
                continue
            share = r.get("revenue_ratio")
            out.setdefault(code, []).append({
                "segment_name": str(r["segment_name"]),
                "share": float(share) if share is not None and share == share else None,
            })
        return out
    except Exception as e:
        print(f"  ⚠️ segments取得失敗（EDINET蓄積待ちの可能性）: {str(e)[:80]}")
        return {}


def fetch_texts_from_bq(cfg, sections: list[str]) -> dict:
    """有報の本文の指定節（既定は「事業の内容」）を会社ごと最新年度1件ずつ取得。

    edinet.text_analysis と edinet.gov_text_blocks の両方から。raw_text 列の走査は合わせて数百MB
    （無料枠 1TB/月 に対して週1本）。空・失敗なら {}。
    """
    secs = [str(x).replace("'", "") for x in (sections or []) if str(x).strip()]
    if not secs:
        return {}
    try:
        from edinet_bq_client import EDINETBQClient
        bq = EDINETBQClient()
        project = (
            str(getattr(cfg, "BQ_PROJECT_ID", "") or "").strip()
            or bq.project
        )
        in_list = ", ".join(f"'{x}'" for x in secs)
        # 本文は2か所から: EdinetDB の text_analysis（約580社）と、政府 EDINET の gov_text_blocks（全上場・edinet_gov_text.py）。
        # 同じ会社・節は決算期の新しい方を使う
        sql = f"""
        WITH t AS (
          SELECT company_code, section_type, raw_text, fiscal_year_end, updated_at AS ts
          FROM `{project}.edinet.text_analysis`
          WHERE section_type IN ({in_list}) AND raw_text IS NOT NULL
          UNION ALL
          SELECT LEFT(c.sec_code, 4), g.section_type, g.raw_text, g.fiscal_year_end, g.fetched_at AS ts
          FROM `{project}.edinet.gov_text_blocks` g
          JOIN `{project}.edinet.company` c USING (edinet_code)
          WHERE g.section_type IN ({in_list}) AND g.raw_text IS NOT NULL AND c.sec_code IS NOT NULL
        )
        SELECT company_code, section_type, raw_text FROM t
        WHERE REGEXP_CONTAINS(company_code, r'^[0-9A-Z]{{4}}$')
        QUALIFY ROW_NUMBER() OVER (
          PARTITION BY company_code, section_type
          ORDER BY fiscal_year_end DESC, ts DESC) = 1
        """
        df = bq.query_to_df(sql)
        out: dict = {}
        for _, r in df.iterrows():
            code = company_key(r.get("company_code"))
            if code:
                out.setdefault(code, {})[str(r["section_type"])] = str(r["raw_text"] or "")
        return out
    except Exception as e:
        print(f"  ⚠️ 本文取得失敗: {str(e)[:80]}")
        return {}


def build_merge_sql(project: str, staging: str, confidence_type: str,
                    retire_before: str = None) -> str:
    """
    staging → company_taxonomy_map の MERGE 文（キー: company_id+node_id+confidence_type）。

    retire_before（YYYY-MM-DD）を渡すと、同じ confidence_type で今回 staging に無い
    有効行の valid_to をその日にする（全件を作り直す経路だけが使う）。
    """
    ctype = str(confidence_type).replace("'", "")
    reopen = ", valid_to = NULL" if retire_before else ""
    sql = f"""
        MERGE `{project}.taxonomy.company_taxonomy_map` T
        USING `{staging}` S
        ON T.company_id = S.company_id AND T.node_id = S.node_id
           AND T.confidence_type = '{ctype}'
        WHEN MATCHED THEN UPDATE SET
          relevance_score = S.relevance_score, confidence = S.confidence,
          revenue_share_est = S.revenue_share_est, rationale = S.rationale,
          evidence_source = S.evidence_source,
          valid_from = S.valid_from, updated_at = S.updated_at{reopen}
        WHEN NOT MATCHED THEN INSERT ROW
        """
    if retire_before:
        day = str(retire_before)[:10]
        sql += f"""WHEN NOT MATCHED BY SOURCE
          AND T.confidence_type = '{ctype}' AND T.valid_to IS NULL THEN
          UPDATE SET valid_to = DATE '{day}', updated_at = CURRENT_TIMESTAMP()
        """
    return sql


def upsert_to_bq(rows: list[dict], cfg, confidence_type: str = "edinet",
                 retire_before: str = None) -> tuple[bool, int]:
    """load job で staging に入れて MERGE（ストリーミング挿入は使わない）。冪等。"""
    if not rows:
        return True, 0
    try:
        from google.cloud import bigquery
        from edinet_bq_client import EDINETBQClient
        import io
        bq = EDINETBQClient()
        project = (
            str(getattr(cfg, "BQ_PROJECT_ID", "") or "").strip()
            or bq.project
        )
        suffix = "" if confidence_type == "edinet" else f"_{confidence_type}"
        staging = f"{project}.taxonomy._staging_company_map{suffix}"
        bq.client.query(
            f"CREATE OR REPLACE TABLE `{staging}` "
            f"LIKE `{project}.taxonomy.company_taxonomy_map`").result()
        job_config = bigquery.LoadJobConfig(
            write_disposition="WRITE_TRUNCATE",
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON)
        buf = io.BytesIO("\n".join(
            json.dumps(r, ensure_ascii=False) for r in rows).encode("utf-8"))
        bq.client.load_table_from_file(buf, staging, job_config=job_config).result()
        bq.client.query(build_merge_sql(project, staging, confidence_type,
                                        retire_before)).result()
        bq.client.query(f"DROP TABLE IF EXISTS `{staging}`").result()
        return True, len(rows)
    except Exception as e:
        print(f"  ⚠️ BQ upsert失敗: {str(e)[:100]}")
        return False, 0


def run_segment_path(segments: dict, cfg, dry: bool) -> int:
    """業種経由の経路（segment_mapper → _SECTOR_TO_TAXONOMY）。書いた行数を返す。"""
    from segment_mapper import SegmentMapper
    from size_tier_classifier import get_taxonomy_nodes_for_sector
    mapper = SegmentMapper(cfg)
    rows = build_company_node_rows(
        segments,
        lambda name: mapper.map(name),
        get_taxonomy_nodes_for_sector,
        min_sim=float(getattr(cfg, "TAXONOMY_SEGMENT_MIN_SIM", 0.75)),
    )
    print(f"📊 生成行: {len(rows)}行（confidence_type=edinet）")
    if dry:
        for r in rows[:5]:
            print(f"   {r['company_id']} → {r['node_id']} "
                  f"score={r['relevance_score']} share={r['revenue_share_est']}")
        if len(rows) > 5:
            print(f"   ... 他{len(rows) - 5}行")
        return 0
    # 今回出なかった古い行（基準を上げて外れた行を含む）は失効させる。セグメントの取得が欠けた回に全行を
    # 失効させないよう、十分な社数が取れたときだけ
    min_cos = int(getattr(cfg, "TAXONOMY_SEGMENT_RETIRE_MIN_COMPANIES", 200))
    retire = (date.today() - timedelta(days=1)).isoformat() if len(segments) >= min_cos else None
    ok, n = upsert_to_bq(rows, cfg, retire_before=retire)
    print(f"{'✅ MERGE完了' if ok else '⚠️ 書込失敗（exit 0）'}: {n}行")
    return n


def run_text_match_path(segments: dict, cfg, dry: bool) -> int:
    """語の照合の経路（confidence_type=text_match）。書いた行数を返す。"""
    from taxonomy_loader import load_seed_batches, select_approved_nodes
    rules = load_keyword_rules()
    nodes, _ = select_approved_nodes(load_seed_batches())
    node_terms = build_node_terms(nodes, rules)
    texts = fetch_texts_from_bq(cfg, rules.get("sections") or ["事業の内容"])
    print(f"🔤 語の照合: ノード{len(node_terms)}件 / 本文保有{len(texts)}社 "
          f"/ セグメント保有{len(segments)}社")
    if not texts and not segments:
        print("  ⏩ 本文・セグメントとも無し → スキップ")
        return 0
    matches = match_company_nodes(
        texts, segments, node_terms,
        min_text_hits=int(rules.get("min_text_hits", 3)),
        min_single_term_hits=int(rules.get("min_single_term_hits", 2)),
        ignore_phrases=rules.get("ignore_phrases") or (),
    )
    as_of = date.today().isoformat()
    rows = build_text_match_rows(matches, as_of=as_of,
                                 text_hit_cap=int(rules.get("text_hit_cap", 10)))
    cov = coverage_summary(rows)
    print(f"📊 生成行: {cov['rows']}行 / {cov['companies']}社 / {cov['nodes']}ノード"
          f"（confidence_type={TEXT_MATCH_TYPE}）")
    if dry:
        for r in rows[:5]:
            print(f"   {r['company_id']} → {r['node_id']} "
                  f"score={r['relevance_score']} | {r['rationale'][:80]}")
        return 0
    # 本文の取得が欠けた回に全行を失効させないよう、十分な社数が取れたときだけ失効させる
    min_cos = int(rules.get("retire_min_text_companies", 100))
    retire = ((date.today() - timedelta(days=1)).isoformat()
              if len(texts) >= min_cos else None)
    if retire is None:
        print(f"  ⚠️ 本文保有{len(texts)}社 < {min_cos}社 のため、古い text_match 行の失効は見送り")
    ok, n = upsert_to_bq(rows, cfg, confidence_type=TEXT_MATCH_TYPE,
                         retire_before=retire)
    print(f"{'✅ MERGE完了' if ok else '⚠️ 書込失敗（exit 0）'}: {n}行（text_match）")
    return n


def main():
    dry = "--dry-run" in sys.argv
    try:
        import config as cfg
    except Exception:
        cfg = None

    # 定期実行（週次）用の有効フラグ。
    if not dry and not getattr(cfg, "ENABLE_TAXONOMY_COMPANY_MAPPER", True):
        print("⏩ taxonomy_company_mapper: スキップ"
              "（ENABLE_TAXONOMY_COMPANY_MAPPER=False）")
        return

    segments = fetch_segments_from_bq(cfg)
    print(f"🏢 セグメント保有企業: {len(segments)}社")
    if segments:
        try:
            run_segment_path(segments, cfg, dry)
        except Exception as e:
            print(f"  ⚠️ 業種経由の経路で失敗（語の照合は続行）: {str(e)[:100]}")
    else:
        print("  ⏩ セグメントデータなし → 業種経由の経路はスキップ")

    run_text_match_path(segments, cfg, dry)
    if dry:
        print("✅ --dry-run のためBQ書込なし")


if __name__ == "__main__":
    main()
