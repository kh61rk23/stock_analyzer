"""
edinet_direct_fetcher.py
=========================
【役割】   EDINET API から XBRL をダウンロードし、財務・セグメントデータを
           解析して BigQuery（financials / segments テーブル）に格納する
【実行】   edinet_rotation_manager.py から呼び出される（日次）
           初回確認は Colab で run_fetcher(daily_limit=5) を手動実行
【依存】   edinet_bq_client.py / config.py（XBRL_TAG_MAP / XBRL_SEGMENT_TAGS）
【更新日】 2026-04-11
"""

import os
import requests
import zipfile
import io
import time
import xml.etree.ElementTree as ET
from typing import Optional

# 2026-07-19: BQクライアントは遅延インポート化（google-cloud未導入のローカル環境
# でも純関数（build_financials_row / build_segment_rows）を利用・テスト可能にする）
try:
    from edinet_bq_client import EDINETBQClient
except Exception:
    EDINETBQClient = None

from config import (
    XBRL_TAG_MAP,
    XBRL_SEGMENT_TAGS,
    EDINET_REQUEST_INTERVAL,
    EDINET_REQUEST_TIMEOUT,
)


# ============================================================
# 定数
# ============================================================
# 2026-07-12是正: 実在しないホスト disclosure.edinet-api.go.jp → 正ホストへ統一
EDINET_BASE   = "https://disclosure.edinet-fsa.go.jp/api/v2"
from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'B6_EDINET_DIRECT_FETCHER_XBRL_TYPE': 'XBRL_TYPE',
}

XBRL_TYPE     = 5   # type=5: XBRL ZIP

# 文脈参照 ID のキーワード（当期連結優先 → 単体にフォールバック）
# EDINET XBRL の contextRef 命名規則に基づく優先順位
CONTEXT_PRIORITY = [
    "CurrentYearDuration_ConsolidatedMember",       # 当期連結（通期）
    "CurrentYearDuration",                          # 当期単体（通期）
    "CurrentYearInstant_ConsolidatedMember",        # 当期連結（期末時点）
    "CurrentYearInstant",                           # 当期単体（期末時点）
]


# ============================================================
# Step 1: XBRL ZIP ダウンロード
# ============================================================

def download_xbrl_zip(doc_id: str) -> Optional[bytes]:
    """
    EDINET API から XBRL ZIP をダウンロードし、バイト列で返す。
    失敗時は None を返す。
    """
    url = f"{EDINET_BASE}/documents/{doc_id}"
    params = {"type": XBRL_TYPE}

    # 2026-07-19是正: EDINET API v2 は Subscription-Key 必須。旧コードは
    # 無認証でリクエストしており、キーがあっても 401 で全滅する構造だった。
    # 認証方式は既存流儀（edinet_client.py / api_key_health.py と同じ
    # Ocp-Apim-Subscription-Key ヘッダ）に統一。
    # 2026-09-12: 政府EDINET APIキーは撤去済み（EdinetDBへ一本化）。
    # 本モジュールは ENABLE_EDINET_DIRECT_FETCH=False で退役済みだが、
    # 誤って有効化された場合に無言で動き出さないよう空を返す。
    api_key = ""
    if not api_key:
        print(f"[fetcher] {doc_id}: 政府EDINET APIは撤去済みのためスキップ")
        return None
    headers = {"Ocp-Apim-Subscription-Key": api_key}

    try:
        resp = requests.get(url, params=params, headers=headers,
                            timeout=EDINET_REQUEST_TIMEOUT)
        resp.raise_for_status()
        # Content-Type が application/zip であることを確認
        content_type = resp.headers.get("Content-Type", "")
        if "zip" not in content_type and len(resp.content) < 1000:
            print(f"[fetcher] {doc_id}: 予期しないレスポンス (Content-Type={content_type})")
            return None
        return resp.content
    except requests.RequestException as e:
        print(f"[fetcher] {doc_id}: ダウンロードエラー: {e}")
        return None


# ============================================================
# Step 2: ZIP から XBRL インスタンス文書を抽出
# ============================================================

def extract_xbrl_instance(zip_bytes: bytes) -> Optional[str]:
    """
    ZIP の中から XBRL インスタンス文書（.xbrl）を取得し、文字列で返す。
    EDINET の ZIP 構造:
      XBRL/PublicDoc/
        *.xbrl        ← インスタンス文書（財務データ本体）
        *.xsd         ← スキーマ定義
        *.xml         ← ラベル・計算リンクベース等
    """
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            # .xbrl ファイルを探す（PublicDoc 直下を優先）
            xbrl_files = [
                name for name in zf.namelist()
                if name.endswith(".xbrl") and "PublicDoc" in name
            ]
            if not xbrl_files:
                # PublicDoc がない場合は任意の .xbrl を取得
                xbrl_files = [name for name in zf.namelist() if name.endswith(".xbrl")]

            if not xbrl_files:
                return None

            # 最も短いパス（最上位の .xbrl）を選択
            target = sorted(xbrl_files, key=len)[0]
            raw = zf.read(target)
            # EDINET XBRL は Shift-JIS または UTF-8
            for enc in ("utf-8", "shift_jis", "cp932"):
                try:
                    return raw.decode(enc)
                except UnicodeDecodeError:
                    continue
            return raw.decode("utf-8", errors="replace")
    except zipfile.BadZipFile as e:
        print(f"[fetcher] ZIP 展開エラー: {e}")
        return None


# ============================================================
# Step 3: XBRL パース（財務数値の抽出）
# ============================================================

def _get_value_from_element(root: ET.Element, local_name: str) -> Optional[float]:
    """
    ElementTree のルートから指定ローカル名の要素を検索し、
    文脈参照 ID の優先順位に従って数値を返す。
    Python 3.8+ の {*}LocalName ワイルドカード構文を使用。
    """
    # 全一致要素を取得（ET の名前空間ワイルドカード構文: .//{*}LocalName）
    # ★ 2026-06-10 バグ修正: 壊れた f-string（f".//{{{*}}}..."）が残存し
    #   構文エラーで本モジュール全体が import 不能 → E2-1 が常時スキップ
    #   されていた。正しい行のみ残す。
    elements = root.findall(".//{*}" + local_name)

    if not elements:
        return None

    # contextRef 優先順位でスキャン
    indexed = {e.get("contextRef", ""): e for e in elements}
    for ctx in CONTEXT_PRIORITY:
        if ctx in indexed:
            val = indexed[ctx].text
            if val is not None:
                try:
                    return float(val.replace(",", ""))
                except ValueError:
                    pass

    # 優先 contextRef になければ最初の数値を返す
    for e in elements:
        if e.text:
            try:
                return float(e.text.replace(",", ""))
            except ValueError:
                pass
    return None


def parse_financials_from_xbrl(xbrl_text: str, tag_map: dict) -> dict:
    """
    XBRL インスタンス文書から財務データを抽出し、
    financials テーブルのカラム名をキーとする dict を返す。
    tag_map: { "カラム名": "XBRLローカルタグ名" }（config.py で定義）
    """
    try:
        root = ET.fromstring(xbrl_text.encode("utf-8"))
    except ET.ParseError as e:
        print(f"[fetcher] XBRL XML パースエラー: {e}")
        return {}

    result = {}
    for col_name, xbrl_tag in tag_map.items():
        val = _get_value_from_element(root, xbrl_tag)
        result[col_name] = val  # 取得できない場合は None のまま格納

    return result


def parse_segments_from_xbrl(xbrl_text: str, segment_tags: dict) -> list[dict]:
    """
    XBRL インスタンス文書からセグメント情報を抽出し、
    segments テーブルのレコードリストを返す。
    segment_tags: {
        "sales":  "XBRLタグ名（セグメント売上）",
        "profit": "XBRLタグ名（セグメント利益）",
        "assets": "XBRLタグ名（セグメント資産）",
    }
    セグメントは contextRef の命名パターンで識別する。
    """
    try:
        root = ET.fromstring(xbrl_text.encode("utf-8"))
    except ET.ParseError:
        return []

    segments = {}  # { segment_label: {sales, profit, assets} }

    for field, xbrl_tag in segment_tags.items():
        elements = root.findall(".//{*}" + xbrl_tag)
        for e in elements:
            ctx = e.get("contextRef", "")
            # セグメント contextRef は "CurrentYearDuration_" + セグメント名 の形式
            # 連結・当期・非合計のコンテキストのみ対象
            if "CurrentYearDuration" not in ctx:
                continue
            if any(x in ctx for x in ["ConsolidatedMember", "Total", "Elimination"]):
                continue

            # セグメントラベルとして contextRef から抽出
            label = ctx.replace("CurrentYearDuration_", "").strip()
            if not label:
                label = "（名称未取得）"

            if label not in segments:
                segments[label] = {"segment_sales": None, "segment_profit": None, "segment_assets": None}

            if e.text:
                try:
                    segments[label][field] = float(e.text.replace(",", ""))
                except ValueError:
                    pass

    return [{"raw_segment_name": k, **v} for k, v in segments.items()]


# ============================================================
# Step 4: BQ 行ビルダー（純関数・実スキーマ準拠）
# ============================================================
# 2026-07-19是正: 旧コードは実在しない列（edinet_code/securities_code/
# period_end/fiscal_year/ifrs_flag/created_at 等）で INSERT しており、
# 実テーブル（financials: fin_id/company_code/fiscal_year_end/...、
# segments: segment_id/segment_name/...、segment_finance: sf_id/revenue/...）
# に対して全行 insert エラーになる構造だった（EDINETキー失効前から
# financials/segments が 0 行のままだった根本原因の一つ）。
# 実スキーマは MCP 直query（2026-07-19）で確認済み。

def _ratio(num, den) -> Optional[float]:
    """分子/分母の安全な除算（None・0分母は None）。"""
    try:
        if num is None or den is None or float(den) == 0.0:
            return None
        return round(float(num) / float(den), 6)
    except (TypeError, ValueError):
        return None


def build_financials_row(doc_meta: dict, fin_data: dict, now: str) -> dict:
    """
    XBRL 抽出結果（XBRL_TAG_MAP のキー）を edinet.financials の
    実スキーマ行へ変換する純関数。
    """
    doc_id       = doc_meta.get("doc_id", "")
    company_code = doc_meta.get("company_code") or None
    fye          = doc_meta.get("fiscal_year_end")
    fye_str      = str(fye)[:10] if fye is not None and str(fye) not in ("", "None", "NaT") else None

    op_cf  = fin_data.get("operating_cf")
    inv_cf = fin_data.get("investing_cf")
    free_cf = (op_cf + inv_cf) if (op_cf is not None and inv_cf is not None) else None

    emp = fin_data.get("num_employees")
    return {
        "fin_id":                doc_id + "_fin",
        "doc_id":                doc_id,
        "company_code":          company_code,
        "fiscal_year_end":       fye_str,
        "report_period":         "FY",
        "net_sales":             fin_data.get("net_sales"),
        "operating_income":      fin_data.get("operating_income"),
        "ordinary_income":       fin_data.get("ordinary_income"),
        "net_income":            fin_data.get("net_income"),
        "total_assets":          fin_data.get("total_assets"),
        "equity":                fin_data.get("equity"),
        "interest_bearing_debt": fin_data.get("interest_bearing_debt"),
        "cash_and_equiv":        fin_data.get("cash"),
        "operating_cf":          op_cf,
        "investing_cf":          inv_cf,
        "free_cf":               free_cf,
        "employee_count":        int(emp) if emp is not None else None,
        "avg_age_years":         fin_data.get("avg_age"),
        "avg_annual_salary":     fin_data.get("avg_salary"),
        "total_director_comp":   fin_data.get("total_director_remuneration"),
        "max_single_comp":       fin_data.get("max_director_remuneration"),
        "capex":                 fin_data.get("capex"),
        "depreciation":          fin_data.get("depreciation"),
        "roe":               _ratio(fin_data.get("net_income"), fin_data.get("equity")),
        "roa":               _ratio(fin_data.get("net_income"), fin_data.get("total_assets")),
        "operating_margin":  _ratio(fin_data.get("operating_income"), fin_data.get("net_sales")),
        "labor_productivity": _ratio(fin_data.get("net_sales"), emp),
        "currency":              "JPY",
        "source":                "edinet_direct",
        "updated_at":            now,
    }


def build_segment_rows(doc_meta: dict, seg_records: list[dict], now: str) -> tuple[list[dict], list[dict]]:
    """
    セグメント抽出結果を edinet.segments（名称マスタ）と
    edinet.segment_finance（数値）の実スキーマ行ペアへ変換する純関数。

    Returns:
        (segments_rows, segment_finance_rows)
    """
    doc_id       = doc_meta.get("doc_id", "")
    company_code = doc_meta.get("company_code") or None
    fye          = doc_meta.get("fiscal_year_end")
    period       = str(fye)[:10] if fye is not None and str(fye) not in ("", "None", "NaT") else None

    total_sales = sum(
        float(s["segment_sales"]) for s in seg_records
        if s.get("segment_sales") is not None
    )

    seg_rows, fin_rows = [], []
    for i, seg in enumerate(seg_records):
        name  = str(seg.get("raw_segment_name", "")).strip() or f"segment_{i}"
        sid   = f"{doc_id}_seg{i:02d}"
        sales = seg.get("segment_sales")
        profit = seg.get("segment_profit")
        seg_rows.append({
            "segment_id":        sid,
            "doc_id":            doc_id,
            "company_code":      company_code,
            "segment_name":      name,
            "segment_name_norm": name.lower().replace(" ", "").replace("　", ""),
            "segment_type":      "business",
            "parent_segment_id": None,
            "report_period":     period,
            "source_id":         doc_id,
            "updated_at":        now,
        })
        fin_rows.append({
            "sf_id":            sid + "_fin",
            "segment_id":       sid,
            "revenue":          sales,
            "revenue_ratio":    _ratio(sales, total_sales),
            "operating_profit": profit,
            "operating_margin": _ratio(profit, sales),
            "assets":           seg.get("segment_assets"),
            "geography":        None,
            "currency":         "JPY",
            "report_period":    period,
            "updated_at":       now,
        })
    return seg_rows, fin_rows


# ============================================================
# Step 4b: 1社分の取得・解析・BQ 格納
# ============================================================

def fetch_and_store_one(doc_meta: dict, bq_client: EDINETBQClient) -> bool:
    """
    doc_index の 1 レコード分の XBRL を取得・解析・BQ に格納する。
    doc_meta: doc_index テーブルの行 dict（実列: doc_id/company_code/
              edinet_code/fiscal_year_end/disclosure_date 等）
    戻り値: 成功 True / 失敗 False
    """
    doc_id       = doc_meta.get("doc_id", "")
    edinet_code  = doc_meta.get("edinet_code", "")
    company_code = doc_meta.get("company_code", "")
    now          = bq_client.now_iso()

    # ---- XBRL ダウンロード ----
    zip_bytes = download_xbrl_zip(doc_id)
    if zip_bytes is None:
        return False

    # ---- XBRL 抽出 ----
    xbrl_text = extract_xbrl_instance(zip_bytes)
    if xbrl_text is None:
        print(f"[fetcher] {doc_id}: XBRL インスタンス文書が見つからない")
        return False

    # ---- 財務データ解析 → financials（実スキーマ）----
    fin_data = parse_financials_from_xbrl(xbrl_text, XBRL_TAG_MAP)
    financials_row = build_financials_row(doc_meta, fin_data, now)
    ok_fin = bq_client.insert_rows("financials", [financials_row])

    # ---- セグメントデータ解析 → segments + segment_finance（実スキーマ）----
    seg_records = parse_segments_from_xbrl(xbrl_text, XBRL_SEGMENT_TAGS)
    seg_rows, seg_fin_rows = build_segment_rows(doc_meta, seg_records, now)
    ok_seg = bq_client.insert_rows("segments", seg_rows) if seg_rows else True
    ok_sf  = bq_client.insert_rows("segment_finance", seg_fin_rows) if seg_fin_rows else True

    # ---- fetched_flag を更新 ----
    if ok_fin:
        bq_client.mark_doc_as_fetched(doc_id)
        print(
            f"[fetcher] {edinet_code} ({company_code}) "
            f"fiscal_year_end={financials_row.get('fiscal_year_end')}: "
            f"財務 OK / セグメント {len(seg_rows)} 件"
        )

    return ok_fin and ok_seg and ok_sf


# ============================================================
# Step 5: ローテーション実行（edinet_rotation_manager から呼び出す）
# ============================================================

def run_fetcher(daily_limit: int = 33) -> dict:
    # D-①-1: retained only for historical recovery; active runs must use EdinetDB.
    import config as cfg
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    try:
        import config
        if not getattr(config, "ENABLE_EDINET_DIRECT_FETCH", False):
            print("[fetcher] skipped: government EDINET parser is retired")
            return {"skipped": True, "reason": "ENABLE_EDINET_DIRECT_FETCH=False"}
    except Exception:
        pass
    """
    BQ doc_index から未取得レコードを daily_limit 件取得・処理する。
    edinet_rotation_manager.py の run() から呼び出す想定。
    戻り値: { "success": int, "failure": int }
    """
    if EDINETBQClient is None:
        print("[fetcher] google-cloud-bigquery 未導入のためスキップ")
        return {"success": 0, "failure": 0}
    bq_client = EDINETBQClient()
    unfetched = bq_client.get_unfetched_docs(limit=daily_limit)

    if unfetched.empty:
        print("[fetcher] 未取得レコードなし")
        return {"success": 0, "failure": 0}

    success, failure = 0, 0

    for _, row in unfetched.iterrows():
        doc_meta = row.to_dict()
        ok = fetch_and_store_one(doc_meta, bq_client)
        if ok:
            success += 1
        else:
            failure += 1
        time.sleep(EDINET_REQUEST_INTERVAL)

    print(f"[fetcher] 完了: 成功={success} / 失敗={failure}")
    return {"success": success, "failure": failure}


# ============================================================
# Colab 確認用エントリポイント
# ============================================================
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="EDINET XBRL 取得・BQ 格納")
    parser.add_argument("--limit", type=int, default=5,
                        help="1回の実行で処理する最大件数（初回確認は 5 推奨）")
    args = parser.parse_args()

    result = run_fetcher(daily_limit=args.limit)
    print(f"結果: {result}")
