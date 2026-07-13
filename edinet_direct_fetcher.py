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

import requests
import zipfile
import io
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Optional

from edinet_bq_client import EDINETBQClient
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
XBRL_TYPE     = 5   # type=5: XBRL ZIP
HTML_TYPE     = 1   # type=1: HTML（E4 テキスト分析用。E1 では未使用）

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

    try:
        resp = requests.get(url, params=params, timeout=EDINET_REQUEST_TIMEOUT)
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
# Step 4: 1社分の取得・解析・BQ 格納
# ============================================================

def fetch_and_store_one(doc_meta: dict, bq_client: EDINETBQClient) -> bool:
    """
    doc_index の 1 レコード分の XBRL を取得・解析・BQ に格納する。
    doc_meta: doc_index テーブルの行 dict
    戻り値: 成功 True / 失敗 False
    """
    doc_id         = doc_meta.get("doc_id", "")
    edinet_code    = doc_meta.get("edinet_code", "")
    securities_code = doc_meta.get("securities_code", "")
    period_end     = str(doc_meta.get("period_end", ""))
    submit_date    = str(doc_meta.get("submit_date", ""))
    now            = bq_client.now_iso()

    # ---- XBRL ダウンロード ----
    zip_bytes = download_xbrl_zip(doc_id)
    if zip_bytes is None:
        return False

    # ---- XBRL 抽出 ----
    xbrl_text = extract_xbrl_instance(zip_bytes)
    if xbrl_text is None:
        print(f"[fetcher] {doc_id}: XBRL インスタンス文書が見つからない")
        return False

    # ---- 財務データ解析 ----
    fin_data = parse_financials_from_xbrl(xbrl_text, XBRL_TAG_MAP)

    # fiscal_year を period_end から計算
    fiscal_year = None
    if period_end and len(period_end) >= 4:
        try:
            fiscal_year = int(period_end[:4])
        except ValueError:
            pass

    # free_cf の計算（operating_cf + investing_cf）
    op_cf  = fin_data.get("operating_cf")
    inv_cf = fin_data.get("investing_cf")
    free_cf = (op_cf + inv_cf) if (op_cf is not None and inv_cf is not None) else None

    financials_row = {
        "edinet_code":                 edinet_code,
        "securities_code":             securities_code,
        "doc_id":                      doc_id,
        "period_end":                  period_end or None,
        "fiscal_year":                 fiscal_year,
        **fin_data,
        "free_cf":                     free_cf,
        "ifrs_flag":                   doc_meta.get("ifrs_flag", False),
        "created_at":                  now,
    }

    ok_fin = bq_client.insert_rows("financials", [financials_row])

    # ---- セグメントデータ解析 ----
    seg_records = parse_segments_from_xbrl(xbrl_text, XBRL_SEGMENT_TAGS)
    seg_rows = [
        {
            "edinet_code":      edinet_code,
            "securities_code":  securities_code,
            "doc_id":           doc_id,
            "period_end":       period_end or None,
            "raw_segment_name": seg["raw_segment_name"],
            "segment_sales":    seg.get("segment_sales"),
            "segment_profit":   seg.get("segment_profit"),
            "segment_assets":   seg.get("segment_assets"),
            "created_at":       now,
        }
        for seg in seg_records
    ]
    ok_seg = bq_client.insert_rows("segments", seg_rows) if seg_rows else True

    # ---- fetched_flag を更新 ----
    if ok_fin:
        bq_client.mark_doc_as_fetched(doc_id)
        print(
            f"[fetcher] {edinet_code} ({securities_code}) "
            f"period={period_end}: 財務 OK / セグメント {len(seg_rows)} 件"
        )

    return ok_fin and ok_seg


# ============================================================
# Step 5: ローテーション実行（edinet_rotation_manager から呼び出す）
# ============================================================

def run_fetcher(daily_limit: int = 33) -> dict:
    """
    BQ doc_index から未取得レコードを daily_limit 件取得・処理する。
    edinet_rotation_manager.py の run() から呼び出す想定。
    戻り値: { "success": int, "failure": int }
    """
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
