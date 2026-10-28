# ==============================================================
# edinet_exposure_extractor.py  v1.0  (G-C2)
# ==============================================================
# 【役割】
#   EDINET 有価証券報告書のテキストから海外権益・海外事業情報を
#   抽出し、geopolitics.company_country_exposure に格納する。
#
# 【データフロー】
#   ① edinet.doc_index（BQ）から最新有報の doc_id を取得
#   ② edinet_bq_client 経由で有報テキストセクションを取得
#      （text_extractor.py が未配置の場合は edinet_client.py 経由）
#   ③ 対象セクションのテキストを正規表現・キーワードマッチングで解析
#   ④ 国別売上比率・拠点情報・資源権益情報を構造化
#   ⑤ geopolitics.company_country_exposure に MERGE
#
# 【抽出対象セクション（config.GEO_EDINET_TARGET_SECTIONS）】
#   - 事業の概況 / 主要な事業内容
#   - 海外事業 / 地域別事業概況
#   - リスク情報 / 重要な会計上の見積り
#
# 【抽出パターン】
#   P1: 地域別売上表（「北米  XXX百万円  XX%」形式）
#   P2: 「海外売上比率 XX%」「〜国売上高 XX%」文章形式
#   P3: 拠点情報（「〜国に XX 拠点を保有」形式）
#   P4: 権益・出資情報（「〜国の〜に出資」形式）
#
# 【依存】
#   edinet_bq_client.py / jogmec_scraper.py（BQ MERGE共有）
#   config.py / country_profile_builder.py
# ==============================================================

from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Any

# ==============================================================
# BLOCK 1: 地域名 → ISO 3166-1 alpha-3 変換マスター（日本語）
# ==============================================================
# 有報では国名・地域名が多様な表記で登場するため、
# 正規化マッピングを網羅的に整備する。

REGION_TO_CODE_JA: dict[str, str] = {
    # 北米
    "北米":               "USA",
    "北アメリカ":          "USA",
    "アメリカ":            "USA",
    "アメリカ合衆国":      "USA",
    "米国":               "USA",
    "米":                 "USA",
    "カナダ":             "CAN",
    "メキシコ":            "MEX",
    # 欧州
    "欧州":               "DEU",
    "ヨーロッパ":          "DEU",
    "ドイツ":             "DEU",
    "フランス":            "FRA",
    "英国":               "GBR",
    "イギリス":            "GBR",
    "オランダ":            "NLD",
    "ポーランド":          "POL",
    "スウェーデン":        "SWE",
    "イタリア":            "ITA",
    "スペイン":            "ESP",
    "ベルギー":            "BEL",
    # 東アジア
    "中国":               "CHN",
    "中華人民共和国":      "CHN",
    "中国大陸":            "CHN",
    "香港":               "CHN",
    "韓国":               "KOR",
    "大韓民国":            "KOR",
    "台湾":               "TWN",
    "日本":               "JPN",
    # 東南アジア
    "東南アジア":          "SGP",
    "アセアン":            "SGP",
    "ASEAN":              "SGP",
    "シンガポール":        "SGP",
    "マレーシア":          "MYS",
    "インドネシア":        "IDN",
    "タイ":               "THA",
    "ベトナム":            "VNM",
    "フィリピン":          "PHL",
    "カンボジア":          "KHM",
    "ミャンマー":          "MMR",
    # 南アジア
    "インド":             "IND",
    "バングラデシュ":      "BGD",
    "パキスタン":          "PAK",
    # 中東
    "中東":               "SAU",
    "サウジアラビア":      "SAU",
    "UAE":                "ARE",
    "アラブ首長国連邦":    "ARE",
    "イスラエル":          "ISR",
    "トルコ":             "TUR",
    "イラン":             "IRN",
    "イラク":             "IRQ",
    # 大洋州
    "オーストラリア":      "AUS",
    "豪州":               "AUS",
    "ニュージーランド":    "NZL",
    # 中南米
    "南米":               "BRA",
    "ブラジル":            "BRA",
    "アルゼンチン":        "ARG",
    "チリ":               "CHL",
    "コロンビア":          "COL",
    "ペルー":             "PER",
    # アフリカ
    "アフリカ":            "ZAF",
    "南アフリカ":          "ZAF",
    "ナイジェリア":        "NGA",
    "モザンビーク":        "MOZ",
    "ザンビア":            "ZMB",
    "コンゴ民主共和国":    "COD",
    # ロシア・中央アジア
    "ロシア":             "RUS",
    "カザフスタン":        "KAZ",
    "ウズベキスタン":      "UZB",
    "モンゴル":            "MNG",
    # その他地域表記
    "アジア":             "CHN",
    "アジア太平洋":        "CHN",
    "アジアパシフィック":  "CHN",
    "その他":             None,    # 「その他」は格納しない
    "海外合計":           None,
    "海外":               None,
    "国内":               "JPN",
}

# 地域名の正規化（表記ゆれ統一）
REGION_NORMALIZE: dict[str, str] = {
    "米州":    "北米",
    "南北米":  "北米",
    "欧州計":  "欧州",
    "欧米":    "欧州",
    "中国計":  "中国",
    "アジア計": "東南アジア",
}

# ==============================================================
# BLOCK 2: 抽出パターン定義
# ==============================================================

# P1: 地域別売上テーブル行
# 例: 「北米　12,345　25.3」「米国  8,500  17.8%」
_REGION_TABLE_PATTERN = re.compile(
    r"(?P<region>北米|北アメリカ|米国|アメリカ|欧州|ヨーロッパ|中国|韓国|台湾|"
    r"東南アジア|アセアン|ASEAN|インド|中東|オーストラリア|豪州|南米|ブラジル|"
    r"アフリカ|ロシア|シンガポール|マレーシア|インドネシア|タイ|ベトナム|"
    r"フィリピン|カナダ|メキシコ|ドイツ|フランス|英国|イギリス|その他海外)"
    r"[\s　\|\t,，。]*"
    r"(?:[\d,，]+[\s　\|\t]*)??"
    r"(?P<pct>\d{1,3}(?:[\.．]\d{1,2})?)\s*[%％]",
    re.MULTILINE,
)

# P2: 文章中の海外売上比率
# 例: 「海外売上比率は35.2%」「海外売上高比率 38%」
_OVERSEAS_RATIO_PATTERN = re.compile(
    r"海外(?:売上|収益|収入|事業)(?:高|額|比率|の比率)?[はが]?\s*"
    r"(?:約\s*)?(?P<pct>\d{1,3}(?:\.\d{1,2})?)\s*[%％]",
    re.MULTILINE,
)

# P3: 拠点情報
# 例: 「中国に5拠点」「米国に製造拠点3か所」
_BASE_PATTERN = re.compile(
    r"(?P<region>中国|米国|アメリカ|欧州|韓国|台湾|東南アジア|インド|"
    r"オーストラリア|ブラジル|ドイツ|フランス|英国|タイ|インドネシア|ベトナム)"
    r"[にで](?:は\s*)?(?:工場|製造拠点|生産拠点|販売拠点|営業所|研究所|拠点)を?"
    r"(?:有し|保有し|設置し|展開し|運営し)?(?:ており|、)?",
    re.MULTILINE,
)

# P4: 権益・出資情報
# 例: 「チリの銅鉱山に出資」「オーストラリアのLNGプロジェクトに参加」
_INTEREST_PATTERN = re.compile(
    r"(?P<region>チリ|ペルー|オーストラリア|豪州|カザフスタン|モンゴル|"
    r"インドネシア|フィリピン|コンゴ民主共和国|ギニア|ザンビア|モザンビーク|"
    r"ブラジル|カナダ|サウジアラビア|UAE|中東|ロシア|アルゼンチン)"
    # (?:\S+){0,8} のような繰り返しは空白の無い日本語の長文で破滅的バックトラックを
    # 起こすため、文字数で上限を切る。
    r"(?:の|で)?\S{0,40}?"
    r"(?P<action>権益|出資|持分|プロジェクト参加|開発参加|鉱山|採掘|oil|LNG)",
    re.MULTILINE,
)

# P5: 国別売上高の文章形式
# 例: 「中国向け売上高は全体の22%」
_COUNTRY_SALES_PATTERN = re.compile(
    r"(?P<region>中国|米国|アメリカ|韓国|台湾|欧州|インド|東南アジア|"
    r"北米|オーストラリア|ブラジル|ドイツ|フランス|英国)"
    r"(?:向け|の|での)?\s*"
    r"(?:売上高|収益|収入|売上|売り上げ)(?:は|が)?"
    r"(?:全体の|連結の|総売上の|売上全体の)?\s*"
    r"(?:約\s*)?(?P<pct>\d{1,3}(?:\.\d{1,2})?)\s*[%％]",
    re.MULTILINE,
)


# ==============================================================
# BLOCK 3: テキスト解析コア
# ==============================================================

def _normalize_region(name: str) -> str:
    """地域名の表記ゆれを正規化する。"""
    n = name.strip()
    return REGION_NORMALIZE.get(n, n)


def _region_to_code(region_name: str) -> str | None:
    """地域名 → ISO 3166-1 alpha-3 を返す。None は格納スキップ。"""
    normalized = _normalize_region(region_name)
    return REGION_TO_CODE_JA.get(normalized)


def _extract_exposure_from_text(
    text: str,
    ticker: str,
    section_type: str,
    min_share: float = 0.03,
) -> list[dict]:
    """
    有報テキスト1セクションから exposure レコードリストを抽出する。

    Args:
        text         : セクションテキスト（生文字列）
        ticker       : JP株ティッカー（例: "7203.T"）
        section_type : セクション種別（config.GEO_EDINET_TARGET_SECTIONS の値）
        min_share    : 格納最低閾値

    Returns:
        list of exposure record dict
    """
    records: list[dict] = []
    seen_codes: dict[str, float] = {}   # {country_code: max_share}
    today_str = date.today().isoformat()

    # テキスト前処理（全角スペース・改行の正規化）
    text_n = text.replace("\u3000", " ").replace("\xa0", " ")
    text_n = re.sub(r"[\r\n]+", "\n", text_n)

    # ── P1: 地域別テーブルパターン ─────────────────────────────
    for m in _REGION_TABLE_PATTERN.finditer(text_n):
        region = m.group("region")
        pct    = float(m.group("pct"))
        if pct <= 0 or pct > 100:
            continue
        code = _region_to_code(region)
        if not code:
            continue
        share = round(pct / 100.0, 4)
        if share < min_share:
            continue
        if share > seen_codes.get(code, 0.0):
            seen_codes[code] = share
            records.append({
                "ticker":            ticker,
                "market":            "JP",
                "country_code":      code,
                "exposure_type":     "SALES_NETWORK",
                "exposure_strength": share,
                "revenue_share_est": share,
                "employee_share_est": None,
                "asset_share_est":   None,
                "commodity_code":    None,
                "source":            "edinet_auto",
                "evidence_url":      None,
                "rationale":         (
                    f"有報テキスト P1テーブル [{section_type}]: "
                    f"{region} {pct:.1f}%"
                ),
                "review_flag":       share < 0.20,
                "valid_from":        today_str,
            })

    # ── P2: 海外売上比率文章パターン ──────────────────────────
    for m in _OVERSEAS_RATIO_PATTERN.finditer(text_n):
        pct   = float(m.group("pct"))
        share = round(pct / 100.0, 4)
        if share < min_share or share > 1.0:
            continue
        # 海外 = JPN以外の合算として USA を代表国に格納（近似）
        # 実態は複数国合算のため review_flag=True
        code = "USA"
        if share > seen_codes.get(code, 0.0):
            seen_codes[code] = share
            records.append({
                "ticker":            ticker,
                "market":            "JP",
                "country_code":      code,
                "exposure_type":     "SALES_NETWORK",
                "exposure_strength": share,
                "revenue_share_est": share,
                "employee_share_est": None,
                "asset_share_est":   None,
                "commodity_code":    None,
                "source":            "edinet_auto",
                "evidence_url":      None,
                "rationale":         (
                    f"有報テキスト P2海外比率 [{section_type}]: "
                    f"海外売上比率 {pct:.1f}%（複数国合算・要確認）"
                ),
                "review_flag":       True,
                "valid_from":        today_str,
            })

    # ── P3: 拠点情報パターン ───────────────────────────────────
    for m in _BASE_PATTERN.finditer(text_n):
        region = m.group("region")
        code   = _region_to_code(region)
        if not code:
            continue
        if code not in seen_codes:
            seen_codes[code] = 0.10   # 拠点のみの場合 exposure_strength=0.10
            records.append({
                "ticker":            ticker,
                "market":            "JP",
                "country_code":      code,
                "exposure_type":     "PRODUCTION_FACILITY",
                "exposure_strength": 0.10,
                "revenue_share_est": None,
                "employee_share_est": None,
                "asset_share_est":   None,
                "commodity_code":    None,
                "source":            "edinet_auto",
                "evidence_url":      None,
                "rationale":         (
                    f"有報テキスト P3拠点情報 [{section_type}]: "
                    f"{region}に生産/販売拠点"
                ),
                "review_flag":       True,
                "valid_from":        today_str,
            })

    # ── P4: 権益・出資情報パターン ─────────────────────────────
    for m in _INTEREST_PATTERN.finditer(text_n):
        region = m.group("region")
        action = m.group("action")
        code   = _region_to_code(region)
        if not code:
            continue

        # 資源種別を推定
        context = text_n[max(0, m.start() - 30):m.end() + 50]
        commodity = None
        for kw, com in [
            ("石油", "crude_oil"), ("原油", "crude_oil"),
            ("LNG", "lng"), ("液化天然ガス", "lng"),
            ("天然ガス", "natural_gas"),
            ("石炭", "coal"),
            ("銅", "copper"), ("鉄鉱石", "iron_ore"),
            ("リチウム", "lithium"), ("コバルト", "cobalt"),
            ("ニッケル", "nickel"), ("レアアース", "rare_earth"),
            ("金鉱", "gold"), ("ウラン", "uranium"),
        ]:
            if kw in context:
                commodity = com
                break

        exp_type = "RESOURCE_CONCESSION"
        if "出資" in action or "持分" in action:
            exp_type = "EQUITY_STAKE"
        elif "合弁" in action or "JV" in action:
            exp_type = "JOINT_VENTURE"

        key = (code, exp_type, commodity or "")
        if key not in {(r["country_code"], r["exposure_type"],
                        r.get("commodity_code") or "")
                       for r in records}:
            records.append({
                "ticker":            ticker,
                "market":            "JP",
                "country_code":      code,
                "exposure_type":     exp_type,
                "exposure_strength": 0.10,
                "revenue_share_est": None,
                "employee_share_est": None,
                "asset_share_est":   None,
                "commodity_code":    commodity,
                "source":            "edinet_auto",
                "evidence_url":      None,
                "rationale":         (
                    f"有報テキスト P4権益情報 [{section_type}]: "
                    f"{region}の{action[:20]}"
                ),
                "review_flag":       True,
                "valid_from":        today_str,
            })

    # ── P5: 国別売上文章パターン ───────────────────────────────
    for m in _COUNTRY_SALES_PATTERN.finditer(text_n):
        region = m.group("region")
        pct    = float(m.group("pct"))
        share  = round(pct / 100.0, 4)
        code   = _region_to_code(region)
        if not code or share < min_share:
            continue
        if share > seen_codes.get(code, 0.0):
            seen_codes[code] = share
            records.append({
                "ticker":            ticker,
                "market":            "JP",
                "country_code":      code,
                "exposure_type":     "SALES_NETWORK",
                "exposure_strength": share,
                "revenue_share_est": share,
                "employee_share_est": None,
                "asset_share_est":   None,
                "commodity_code":    None,
                "source":            "edinet_auto",
                "evidence_url":      None,
                "rationale":         (
                    f"有報テキスト P5売上文章 [{section_type}]: "
                    f"{region}向け {pct:.1f}%"
                ),
                "review_flag":       share < 0.20,
                "valid_from":        today_str,
            })

    # 重複排除（同一 country_code × exposure_type × commodity_code で最大値優先）
    dedup: dict[tuple, dict] = {}
    for r in records:
        key = (
            r["country_code"],
            r["exposure_type"],
            r.get("commodity_code") or "",
        )
        existing = dedup.get(key)
        if existing is None or r["exposure_strength"] > existing["exposure_strength"]:
            dedup[key] = r

    return list(dedup.values())


# ==============================================================
# BLOCK 4: BQ から有報テキスト取得
# ==============================================================

def prefetch_text_sections(codes: list, cfg=None,
                           project_id: str = "stock-analyzer-edinet") -> dict | None:
    """対象会社の本文（text_analysis と gov_text_blocks の対象の節・会社と節ごとに最新）を1クエリでまとめて取る。

    1社ずつ引くと、本文の表（約95MB）へのクエリが社数ぶん（既定50本）になり、
    時間と課金が社数に比例する。行が無い会社も空の dict で埋める（埋めないと個別取得に戻る）。
    Returns: {証券コード4桁: {section_type: text}}。BQ に届かなければ None（呼び出し側は1社ずつ取る）。
    """
    _pid = getattr(cfg, "GEO_BQ_PROJECT_ID", project_id) if cfg else project_id
    codes = sorted({str(c).replace(".T", "").replace(".t", "") for c in codes if c})
    if not codes:
        return {}
    try:
        import importlib, sys as _sys
        from pathlib import Path as _Path
        for p in ["/content", str(_Path(__file__).parent)]:
            if p not in _sys.path:
                _sys.path.insert(0, p)
        from google.cloud import bigquery
        raw_bq = importlib.import_module("edinet_bq_client").EDINETBQClient().client
        # 本文は EdinetDB（text_analysis・約600社）と政府 EDINET（gov_text_blocks・全上場）の両方から、
        # 会社・節ごとに決算期の新しい方を1つ。節は GEO_EDINET_TARGET_SECTIONS だけ（空なら全部）
        sections = list(getattr(cfg, "GEO_EDINET_TARGET_SECTIONS", []) or []) if cfg else []
        query = f"""
        WITH t AS (
          SELECT company_code, section_type, raw_text AS text_body, fiscal_year_end, updated_at AS ts
          FROM `{_pid}.edinet.text_analysis`
          WHERE company_code IN UNNEST(@codes) AND raw_text IS NOT NULL
          UNION ALL
          SELECT LEFT(c.sec_code, 4), g.section_type, g.raw_text, g.fiscal_year_end, g.fetched_at
          FROM `{_pid}.edinet.gov_text_blocks` g JOIN `{_pid}.edinet.company` c USING (edinet_code)
          WHERE LEFT(c.sec_code, 4) IN UNNEST(@codes) AND g.raw_text IS NOT NULL
        )
        SELECT company_code, section_type, text_body FROM t
        WHERE ARRAY_LENGTH(@sections) = 0 OR section_type IN UNNEST(@sections)
        QUALIFY ROW_NUMBER() OVER (PARTITION BY company_code, section_type ORDER BY fiscal_year_end DESC, ts DESC) = 1
        """
        job = raw_bq.query(query, job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ArrayQueryParameter("codes", "STRING", codes),
                              bigquery.ArrayQueryParameter("sections", "STRING", sections)]))
        out: dict = {c: {} for c in codes}
        for row in job.result():
            if row.section_type and (row.text_body or "").strip():
                out.setdefault(str(row.company_code), {}).setdefault(row.section_type, row.text_body)
        return out
    except Exception as e:
        print(f"  ⚠️  edinet テキストの一括取得に失敗（1社ずつ取る）: {str(e)[:80]}")
        return None


def _get_edinet_text_sections(
    ticker: str,
    cfg=None,
    project_id: str = "stock-analyzer-edinet",
    prefetched: dict | None = None,
    allow_fallback: bool = True,
) -> dict[str, str]:
    """
    BQ の edinet.text_analysis または
    edinet_bq_client 経由で有報テキストセクションを取得する。
    prefetched（prefetch_text_sections の戻り値）があれば BQ は引かない。
    allow_fallback=False なら、BQ に本文が無い会社の有報 HTML 取得（遅い）をしない。

    Returns:
        {section_type: text} or {} (取得失敗時)
    """
    _pid = getattr(cfg, "GEO_BQ_PROJECT_ID", project_id) if cfg else project_id
    # 証券コードに変換（"7203.T" → "7203"）
    code = ticker.replace(".T", "").replace(".t", "")

    try:
        import importlib, sys as _sys
        from pathlib import Path as _Path
        for p in ["/content", str(_Path(__file__).parent)]:
            if p not in _sys.path:
                _sys.path.insert(0, p)
        if prefetched is not None:
            from_bq = dict(prefetched.get(code) or {})
            if from_bq or not allow_fallback:
                return from_bq
            return _sections_from_html(cfg, code, importlib)
        _bq_mod = importlib.import_module("edinet_bq_client")
        raw_bq  = _bq_mod.EDINETBQClient().client

        # edinet.text_analysis から取得
        query = f"""
        SELECT section_type,
               -- 2026-09-26: 本文列は raw_text。旧コードは NULL 固定で、行があると
               -- 空文字の辞書を返して代替経路にも進まず、抽出が常に0件だった
               raw_text AS text_body
        FROM `{_pid}.edinet.text_analysis`
        WHERE company_code = '{code}'
        ORDER BY fiscal_year_end DESC
        LIMIT 10
        """
        rows = list(raw_bq.query(query).result())

        # text_analysis にテキスト本文がある場合（本文が空の行だけなら代替経路へ）
        from_bq = {
            row.section_type: row.text_body
            for row in rows
            if row.section_type and (row.text_body or "").strip()
        }
        if from_bq or not allow_fallback:
            return from_bq
        return _sections_from_html(cfg, code, importlib)

    except Exception as e:
        print(f"  ⚠️  edinet テキスト取得 [{ticker}]: {str(e)[:80]}")

    return {}


def _sections_from_html(cfg, code: str, importlib) -> dict:
    """text_analysis に本文が無い会社 → text_extractor.extract_sections()（EDINET の有報 HTML）で取る。
    edinet_client.EDINETClient / get_section_text は存在しないので text_extractor [E4-1] に委譲する。
    1社ごとに書類を取りに行くので遅い（呼び出し側が社数を絞る）。"""
    try:
        _te_mod = importlib.import_module("text_extractor")
        # extract_sections() の戻り値は {"securities_code", "company_name",
        # ..., "sections": {business, risk, ...}}。本文は "sections" の中だけで、
        # 外側の辞書を読むとメタ情報を本文として扱ってしまう。
        _sections = (_te_mod.extract_sections(cfg, str(code)) or {}).get("sections") or {}
        return {k: v for k, v in _sections.items() if isinstance(v, str) and v.strip()}
    except Exception:
        return {}


# ==============================================================
# BLOCK 5: BQ から IMPORTANT_TICKERS_JP を取得
# ==============================================================

def _listed_tickers(cfg=None) -> list[str]:
    """上場中で有報の本文（政府 EDINET か EdinetDB）がある会社の ticker。取れなければ []。"""
    try:
        from edinet_bq_client import EDINETBQClient
        client = EDINETBQClient().client
        _pid = getattr(cfg, "GEO_BQ_PROJECT_ID", client.project) if cfg else client.project
        sql = f"""
        SELECT DISTINCT LEFT(c.sec_code, 4) AS code FROM `{_pid}.edinet.company` c
        WHERE NOT IFNULL(c.is_delisted, FALSE) AND c.sec_code IS NOT NULL
          AND (c.edinet_code IN (SELECT DISTINCT edinet_code FROM `{_pid}.edinet.gov_text_blocks`)
               OR LEFT(c.sec_code, 4) IN (SELECT DISTINCT company_code FROM `{_pid}.edinet.text_analysis`))
        ORDER BY code
        """
        return [f"{r.code}.T" for r in client.query(sql).result() if r.code]
    except Exception as e:
        print(f"  ⚠️  上場会社の一覧を取れない: {str(e)[:60]}")
        return []


def _get_target_tickers(cfg=None) -> list[str]:
    """
    処理対象ティッカーリストを返す。
    優先順:
      1. cfg.IMPORTANT_TICKERS_JP
      2. edinet.doc_index から有報提出済みの証券コード（上位50社）
    """
    from_cfg = getattr(cfg, "IMPORTANT_TICKERS_JP", []) if cfg else []
    if from_cfg and not getattr(cfg, "GEO_EDINET_ALL_LISTED", True):
        return from_cfg
    listed = _listed_tickers(cfg)
    if listed:
        return listed
    if from_cfg:
        return from_cfg

    # config 未設定の場合は BQ から取得
    try:
        import importlib, sys as _sys
        from pathlib import Path as _Path
        for p in ["/content", str(_Path(__file__).parent)]:
            if p not in _sys.path:
                _sys.path.insert(0, p)
        _pid = getattr(cfg, "GEO_BQ_PROJECT_ID",
                       "stock-analyzer-edinet") if cfg else "stock-analyzer-edinet"
        _bq_mod = importlib.import_module("edinet_bq_client")
        raw_bq  = _bq_mod.EDINETBQClient().client
        query = f"""
        SELECT DISTINCT company_code
        FROM `{_pid}.edinet.doc_index`
        WHERE doc_type = '有価証券報告書'
          AND fetched_flag = TRUE
        ORDER BY company_code
        LIMIT 50
        """
        rows = list(raw_bq.query(query).result())
        return [f"{row.company_code}.T" for row in rows if row.company_code]
    except Exception as e:
        print(f"  ⚠️  ティッカーリスト取得失敗: {str(e)[:60]}")
        return []


# ==============================================================
# BLOCK 6: メインエントリポイント
# ==============================================================

def run_edinet_exposure_extractor(
    cfg=None,
    project_id: str = "stock-analyzer-edinet",
    target_tickers: list[str] | None = None,
    dry_run: bool = False,
) -> dict:
    """
    EDINET 有報テキストから海外権益情報を抽出し BQ に MERGE する。

    Args:
        cfg             : config モジュール
        project_id      : GCPプロジェクトID
        target_tickers  : 指定時はそのティッカーのみ処理
        dry_run         : True の場合 BQ 実行スキップ

    Returns:
        {
          "success":    bool,
          "total":      int,
          "merged":     int,
          "tickers":    list[str],
          "records":    list[dict],
          "error":      str,
          "timestamp":  str,
        }
    """
    _pid = getattr(cfg, "GEO_BQ_PROJECT_ID", project_id) if cfg else project_id
    print(f"\n📄 edinet_exposure_extractor: EDINET有報 海外権益抽出開始")
    print(f"   プロジェクト: {_pid}")

    # 対象ティッカー決定
    if target_tickers:
        tickers = target_tickers
    else:
        tickers = _get_target_tickers(cfg)

    if not tickers:
        return {
            "success":   False,
            "total":     0,
            "merged":    0,
            "tickers":   [],
            "records":   [],
            "error":     "対象ティッカーが0件",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    print(f"   対象ティッカー: {len(tickers)} 件")
    _min = getattr(cfg, "GEO_EXPOSURE_MIN_STRENGTH", 0.03) if cfg else 0.03
    target_sections = getattr(
        cfg, "GEO_EDINET_TARGET_SECTIONS",
        ["事業の概況", "海外事業", "リスク情報"],
    ) if cfg else ["事業の概況", "海外事業", "リスク情報"]

    all_records: list[dict] = []
    processed:   list[str]  = []
    skipped:     list[str]  = []

    # 本文は1クエリでまとめて取る。BQ に本文が無い会社だけ有報 HTML を取りに行くが、1社ずつで遅いので
    # 1回の実行で GEO_EDINET_HTML_FALLBACK_MAX 社まで（月次ランナーの1ステップ900秒に収める）。
    prefetched = prefetch_text_sections(tickers, cfg, _pid)
    fallback_left = int(getattr(cfg, "GEO_EDINET_HTML_FALLBACK_MAX", 5) if cfg else 5)

    for ticker in tickers:
        try:
            print(f"   📋 [{ticker}] テキスト取得中...")
            code = ticker.replace(".T", "").replace(".t", "")
            need_html = prefetched is not None and not (prefetched.get(code) or {})
            sections = _get_edinet_text_sections(ticker, cfg, _pid, prefetched=prefetched,
                                                 allow_fallback=(not need_html) or fallback_left > 0)
            if need_html and fallback_left > 0:
                fallback_left -= 1

            if not sections:
                print(f"   ⚠️  [{ticker}] テキスト未取得 → スキップ")
                skipped.append(ticker)
                continue

            ticker_records: list[dict] = []
            for sec_name, text in sections.items():
                if not text or len(text) < 100:
                    continue
                extracted = _extract_exposure_from_text(
                    text, ticker, sec_name, min_share=_min
                )
                ticker_records.extend(extracted)

            # ティッカー内で重複排除
            dedup: dict[tuple, dict] = {}
            for r in ticker_records:
                key = (
                    r["country_code"],
                    r["exposure_type"],
                    r.get("commodity_code") or "",
                )
                existing = dedup.get(key)
                if (existing is None
                        or r["exposure_strength"] > existing["exposure_strength"]):
                    dedup[key] = r
            ticker_records = list(dedup.values())

            all_records.extend(ticker_records)
            processed.append(ticker)
            print(f"   ✅ [{ticker}] 抽出: {len(ticker_records)} 件")

        except Exception as e:
            print(f"   ⚠️  [{ticker}] 処理エラー: {str(e)[:80]}")
            skipped.append(ticker)

    print(f"\n   📊 抽出完了: {len(all_records)} 件 / "
          f"{len(processed)} 銘柄処理 / {len(skipped)} 銘柄スキップ")

    if not all_records:
        return {
            "success":   False,
            "total":     0,
            "merged":    0,
            "tickers":   processed,
            "records":   [],
            "error":     "抽出レコード0件",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    if dry_run:
        print(f"   [DRY RUN] BQ実行スキップ")
        return {
            "success":   True,
            "total":     len(all_records),
            "merged":    0,
            "tickers":   processed,
            "records":   all_records,
            "error":     "",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    # ── BQ MERGE（jogmec_scraper の共通関数を利用）──────────────
    try:
        import importlib, sys as _sys
        from pathlib import Path as _Path
        for p in ["/content", str(_Path(__file__).parent)]:
            if p not in _sys.path:
                _sys.path.insert(0, p)
        _js     = importlib.import_module("jogmec_scraper")
        _bq_mod = importlib.import_module("edinet_bq_client")
        raw_bq  = _bq_mod.EDINETBQClient().client
    # 一次経路（edinet_bq_client＝Actions の BQ_CREDENTIALS 経路）の失敗理由と
    # Colabフォールバックの失敗理由の両方を返す（後者だけだと真因が隠れる）。
    except Exception as _primary_err:
        try:
            from google.cloud import bigquery as _bq_lib
            from google.colab import auth as _auth
            _auth.authenticate_user()
            raw_bq = _bq_lib.Client(project=_pid, location="asia-northeast1")
            import importlib, sys as _sys
            from pathlib import Path as _Path
            for p in ["/content", str(_Path(__file__).parent)]:
                if p not in _sys.path:
                    _sys.path.insert(0, p)
            _js = importlib.import_module("jogmec_scraper")
        except Exception as e:
            return {
                "success":   False,
                "total":     len(all_records),
                "merged":    0,
                "tickers":   processed,
                "records":   all_records,
                "error":     (f"BQ Client 取得失敗: {_primary_err} "
                              f"/ Colabフォールバックも失敗: {e}"),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

    try:
        BATCH_SIZE   = 500
        merged_total = 0
        for i in range(0, len(all_records), BATCH_SIZE):
            batch     = all_records[i:i + BATCH_SIZE]
            merge_sql = _js._build_exposure_merge_query(_pid, batch)
            job       = raw_bq.query(merge_sql)
            job.result()
            merged_total += len(batch)
            print(f"   MERGE進捗: {merged_total}/{len(all_records)} 件")

        print(f"   ✅ BQ MERGE 完了: {merged_total} 件")
        return {
            "success":   True,
            "total":     len(all_records),
            "merged":    merged_total,
            "tickers":   processed,
            "records":   all_records,
            "error":     "",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    except Exception as e:
        err = str(e)
        print(f"   ❌ BQ MERGE 失敗: {err[:200]}")
        return {
            "success":   False,
            "total":     len(all_records),
            "merged":    0,
            "tickers":   processed,
            "records":   all_records,
            "error":     err[:500],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


# ==============================================================
# 単体テスト / Colab実行エントリ
# ==============================================================

if __name__ == "__main__":
    import sys

    dry_run_flag = "--dry-run" in sys.argv
    target_flag  = None
    for i, arg in enumerate(sys.argv):
        if arg == "--target" and i + 1 < len(sys.argv):
            target_flag = sys.argv[i + 1].split(",")

    print("=" * 60)
    print("edinet_exposure_extractor.py v1.0")
    print("=" * 60)

    result = run_edinet_exposure_extractor(
        cfg             = None,
        project_id      = "stock-analyzer-edinet",
        target_tickers  = target_flag or ["8058.T", "1605.T", "5713.T"],
        dry_run         = dry_run_flag,
    )

    print(f"\n{'✅ 完了' if result['success'] else '❌ 失敗'}")
    print(f"   処理銘柄:     {result['tickers']}")
    print(f"   総レコード数: {result['total']}")
    print(f"   MERGE件数:   {result['merged']}")
    if result.get("records"):
        print(f"\n   サンプル（先頭3件）:")
        for r in result["records"][:3]:
            print(f"   {r['ticker']} → {r['country_code']} "
                  f"{r['exposure_type']} "
                  f"strength={r['exposure_strength']:.2f} "
                  f"rev={r.get('revenue_share_est')}")
    if result["error"]:
        print(f"   エラー: {result['error']}")