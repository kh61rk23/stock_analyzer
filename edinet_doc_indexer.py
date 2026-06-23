"""
edinet_doc_indexer.py
======================
【役割】   EDINET API から当日の開示書類一覧を取得し BigQuery doc_index に登録する
【実行】   GitHub Actions daily_run.yml で毎朝自動実行
           初回セットアップ時は Colab で days_back=90 等を指定して手動実行可
【更新日】 2026-04-11
"""

import requests
import time
from datetime import date, timedelta, timezone, datetime
from edinet_bq_client import EDINETBQClient


# ============================================================
# 定数
# ============================================================
EDINET_BASE      = "https://disclosure.edinet-api.go.jp/api/v2"
TARGET_DOC_TYPES = {"120", "140"}   # 120=有報, 140=四半期報告書
REQUEST_INTERVAL = 1.5              # EDINET API レート制限対策（秒）
REQUEST_TIMEOUT  = 30               # タイムアウト（秒）


# ============================================================
# EDINET API 呼び出し
# ============================================================

def _fetch_raw(target_date: str) -> list[dict]:
    """
    指定日の EDINET 書類一覧 JSON を取得して返す。
    type=2: メタ情報のみ（書類本体は含まない）
    """
    url    = f"{EDINET_BASE}/documents.json"
    params = {"date": target_date, "type": 2}

    try:
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        return data.get("results", [])
    except requests.RequestException as e:
        print(f"[doc_indexer] EDINET API エラー ({target_date}): {e}")
        return []


def _parse_securities_code(raw_code: str | None) -> str:
    """
    EDINET の secCode は末尾に '0' が付いた5桁（例: '72030'）。
    末尾の '0' を除いた4桁の証券コードに変換する。
    """
    if not raw_code:
        return ""
    raw_code = str(raw_code).strip()
    # 5桁かつ末尾が '0' の場合のみ除去
    if len(raw_code) == 5 and raw_code.endswith("0"):
        return raw_code[:4]
    return raw_code


def _to_date_or_none(value: str | None) -> str | None:
    """YYYY-MM-DD 形式の文字列か None を返す（BigQuery DATE 型対応）"""
    if not value:
        return None
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return value
    except ValueError:
        return None


# ============================================================
# 変換・登録
# ============================================================

def _build_rows(results: list[dict], submit_date: str) -> list[dict]:
    """API レスポンスの生データを BigQuery 挿入用の辞書リストに変換する"""
    now  = datetime.now(timezone.utc).isoformat()
    rows = []

    for r in results:
        doc_type = str(r.get("docTypeCode", ""))
        if doc_type not in TARGET_DOC_TYPES:
            continue

        # 会計基準の判定（EDINET は 'ifrsFlg' キーで返す場合がある）
        accounting_std = str(r.get("accountingStandard", ""))
        ifrs_flag      = accounting_std.upper() in {"IFRS", "US-GAAP"}

        rows.append({
            "doc_id":          r.get("docID", ""),
            "edinet_code":     r.get("edinetCode", ""),
            "company_name":    r.get("filerName", ""),
            "securities_code": _parse_securities_code(r.get("secCode")),
            "doc_type_code":   doc_type,
            "period_start":    _to_date_or_none(r.get("periodStart")),
            "period_end":      _to_date_or_none(r.get("periodEnd")),
            "submit_date":     submit_date,
            "xbrl_flag":       r.get("xbrlFlag") == "1",
            "html_flag":       r.get("htmlFlag") == "1",
            "ifrs_flag":       ifrs_flag,
            "fetched_flag":    False,
            "created_at":      now,
            "updated_at":      now,
        })

    return rows


# ============================================================
# メイン処理
# ============================================================

def run_doc_indexer(days_back: int = 1):
    """
    過去 days_back 日分の EDINET 書類一覧を取得し BigQuery に登録する。

    Args:
        days_back:
            通常稼働: 1（当日分のみ）
            初回セットアップ: 90（過去3ヶ月分をまとめて取得）
    """
    client          = EDINETBQClient()
    fetched_dates   = set(client.get_fetched_submit_dates(days=max(days_back + 1, 10)))
    today           = date.today()
    total_inserted  = 0

    for i in range(days_back):
        target = (today - timedelta(days=i)).strftime("%Y-%m-%d")

        # 既に登録済みの日付はスキップ
        if target in fetched_dates:
            print(f"[doc_indexer] {target}: 登録済みのためスキップ")
            continue

        # EDINET API 取得
        raw     = _fetch_raw(target)
        rows    = _build_rows(raw, target)

        if not rows:
            print(f"[doc_indexer] {target}: 対象書類なし（または API エラー）")
        else:
            ok = client.insert_rows("doc_index", rows)
            status = "OK" if ok else "ERROR"
            print(f"[doc_indexer] {target}: {len(rows)} 件登録 ({status})")
            if ok:
                total_inserted += len(rows)

        # レート制限対策
        if i < days_back - 1:
            time.sleep(REQUEST_INTERVAL)

    print(f"[doc_indexer] 完了: 合計 {total_inserted} 件挿入")


# ============================================================
# GitHub Actions / Colab エントリポイント
# ============================================================
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="EDINET doc_index 取得スクリプト")
    parser.add_argument(
        "--days-back", type=int, default=1,
        help="取得する過去日数（初回セットアップ時は 90 等を指定）"
    )
    args = parser.parse_args()

    run_doc_indexer(days_back=args.days_back)
