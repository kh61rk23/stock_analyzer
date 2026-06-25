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
from google.api_core.exceptions import GoogleAPICallError, Forbidden  # Forbidden=403
from google.auth.exceptions import GoogleAuthError          # 認証情報不正・欠落


# ============================================================
# 定数
# ============================================================
EDINET_BASE      = "https://disclosure.edinet-api.go.jp/api/v2"
TARGET_DOC_TYPES = {"120", "140"}   # 120=有報, 140=四半期報告書
# doc_index.doc_type は日本語ラベルで格納する。
# 消費側 edinet_bq_client.get_unfetched_docs が
#   doc_type IN ('有価証券報告書', '四半期報告書')
# でフィルタするため、数字コードのままだと永久にヒットしない。
DOC_TYPE_LABELS  = {"120": "有価証券報告書", "140": "四半期報告書"}
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

def _build_rows(results: list[dict], disclosure_date: str) -> list[dict]:
    """API レスポンスの生データを BigQuery 挿入用の辞書リストに変換する。

    ★ カラム名は doc_index の正本スキーマに厳密に一致させる
      （SPEC_db_schema.md 1-1 / edinet_bq_schema.py TABLE_DEFINITIONS["edinet"]["doc_index"]）。
      旧コードは securities_code / doc_type_code / submit_date / xbrl_flag /
      html_flag / period_start / period_end / created_at という存在しない列名で
      組んでおり、INSERT も get_fetched_submit_dates の SELECT も
      "Unrecognized name" で失敗していた（producer/consumer 不整合）。
    """
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
            "company_code":    _parse_securities_code(r.get("secCode")),
            "edinet_code":     r.get("edinetCode", ""),
            "company_name":    r.get("filerName", ""),
            "doc_type":        DOC_TYPE_LABELS.get(doc_type, doc_type),
            "fiscal_year_end": _to_date_or_none(r.get("periodEnd")),
            "disclosure_date": disclosure_date,
            "xbrl_available":  r.get("xbrlFlag") == "1",
            "html_available":  r.get("htmlFlag") == "1",
            "fetched_flag":    False,
            "ifrs_flag":       ifrs_flag,
            "updated_at":      now,
            # fetched_at は実際に XBRL を取得した時刻。インデックス時点では
            # 未取得なので設定しない（NULL のまま）。
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
    # ── BigQuery 接続（権限不足・認証なしは「致命」ではなく「スキップ」扱い）──
    #   本ステップは daily_run.yml の 3-B にあり、後続の本体アナライザー（step 5）
    #   より前に走る。ここで例外を送出して exit 1 になると、ワークフロー全体が
    #   中断し、当日の解析が一切実行されない（実害: 2026-06-24 の本番実行が
    #   doc_indexer の BQ 403 でここ止まり＝2分43秒で全体停止）。
    #   補助的な EDINET インデックス更新の失敗で日次解析を止めないため、
    #   ONT-1 と同じ graceful degradation 方針で「警告して return」する。
    try:
        client        = EDINETBQClient()
        fetched_dates = set(client.get_fetched_submit_dates(days=max(days_back + 1, 10)))
    except (GoogleAPICallError, GoogleAuthError) as e:
        _first = (str(e).splitlines() or [""])[0][:200]
        print("[doc_indexer] ⚠️ BigQuery 処理に失敗したため doc_index 更新をスキップします"
              "（後続の本体アナライザーは継続実行）。")
        print(f"[doc_indexer]   理由: {type(e).__name__}: {_first}")
        if isinstance(e, (Forbidden, GoogleAuthError)):
            # 認証・権限（IAM）の問題 → GCP 側の設定が必要
            print("[doc_indexer]   ※ 認証/権限エラーです。サービスアカウント（BQ_CREDENTIALS）に"
                  " roles/bigquery.jobUser（または bigquery.user）を BQ_PROJECT_ID の"
                  " プロジェクトへ付与してください。コード側では解決できません。")
        else:
            # BadRequest 等 → クエリ/スキーマ不整合などコード側の問題の可能性
            print("[doc_indexer]   ※ 認証以外のBQエラーです（スキーマ不整合・SQL誤り等の可能性）。"
                  " doc_index の列名と SQL/挿入キーの一致を SPEC_db_schema.md と照合してください。")
        return

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
            try:
                ok = client.insert_rows("doc_index", rows)
            except (GoogleAPICallError, GoogleAuthError) as e:
                _first = (str(e).splitlines() or [""])[0][:120]
                print(f"[doc_indexer] {target}: BQ 書き込み不可のためスキップ "
                      f"({type(e).__name__}: {_first})")
                continue
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
