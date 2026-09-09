# ==============================================================
# credit_data.py  v1.0  (3-5 / A-2)
# JPX 週次信用残高取得 + 信用倍率フラグ
# ==============================================================
# 【役割】
#   JPX（日本取引所グループ）が毎週公開する「信用取引残高」CSVを取得し、
#   銘柄ごとの信用倍率（買い残 ÷ 売り残）を算出する。
#
# 【信用倍率の意味】
#   倍率 < 1.0  : 売り残 > 買い残 → 踏み上げ（ショートスクイーズ）期待
#                 売りが多すぎる＝価格が下がりすぎている可能性
#   倍率 1-3    : 適正範囲（一般的）
#   倍率 3-6    : やや買い偏重（過熱注意）
#   倍率 > 6    : 買い残多すぎ → 戻り売り圧力。高い倍率は天井シグナル
#   倍率 > 10.0 : 買い偏重が極端 → 崩れると急落リスク
#
# 【データソース】
#   JPX 信用取引残高 週次CSV（毎週金曜公開）
#   URL: https://www.jpx.co.jp/markets/statistics-equities/margin/index.html
#
# 【出力スキーマ】
#   {
#     "records": {ticker: CreditRecord},
#     "flag_short_squeeze": [CreditRecord, ...],  # 倍率 < 1.0
#     "flag_overheated":    [CreditRecord, ...],  # 倍率 > 10.0
#     "as_of_date": str,   # 計上基準日
#     "total":      int,
#     "timestamp":  str,
#   }
#   CreditRecord = {
#     "ticker":        "1332.T",
#     "code":          "1332",
#     "name":          str,
#     "long_shares":   int,    # 買い残（株数）
#     "short_shares":  int,    # 売り残（株数）
#     "margin_ratio":  float,  # 信用倍率 = long / short
#     "long_change":   int,    # 買い残増減
#     "short_change":  int,    # 売り残増減
#     "flag":          str,    # "squeeze" / "overheated" / "normal"
#   }
#
# 【screener 統合】
#   run_stock_screener() → _calc_scores() に credit_bonus を追加:
#     squeeze（倍率<1.0）  : quality_score + 3pt（短期逆張り候補）
#     overheated（倍率>10）: composite_score - 5pt（崩落リスク警告）
#
# 【依存】requests / config.py
# 【呼出】from credit_data import run_credit_data, get_credit_bonus
# ==============================================================

from __future__ import annotations

import csv
import io
import json
import time
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Optional

from config_sync import sync_module_constants

# ── 定数 ──────────────────────────────────────────────────────
SQUEEZE_THRESHOLD   = 1.0    # 倍率 < 1.0 → 踏み上げ候補
OVERHEAT_THRESHOLD  = 10.0   # 倍率 > 10.0 → 買い偏重（崩落リスク）
CAUTION_THRESHOLD   = 6.0    # 倍率 > 6.0 → 注意（screenerペナルティなし、ラベルのみ）

# screener ボーナス
BONUS_SQUEEZE       = 3.0    # 踏み上げ候補: quality_score +3pt
PENALTY_OVERHEAT    = -5.0   # 過熱: composite_score -5pt

CACHE_HOURS         = 24 * 7  # 週次データなので7日キャッシュ
CACHE_FILENAME      = "credit_data_cache.json"

# JPX 信用残高 CSV の URL パターン（週次更新）
# 例: https://www.jpx.co.jp/markets/statistics-equities/margin/tvdivq0000001mf0-att/data_j.csv
JPX_MARGIN_CSV_URL = (
    "https://www.jpx.co.jp/markets/statistics-equities/margin/"
    "tvdivq0000001mf0-att/data_j.csv"
)
# フォールバック: 過去数週分のURLを試す
JPX_FALLBACK_URLS = [
    "https://www.jpx.co.jp/markets/statistics-equities/margin/"
    "tvdivq0000001mf0-att/data_j.csv",
]


# ==============================================================
# BLOCK 1: JPX CSV 取得
# ==============================================================

_CFG_CONST_MAP = {
    "CREDIT_SQUEEZE_THRESH":  "SQUEEZE_THRESHOLD",
    "CREDIT_OVERHEAT_THRESH": "OVERHEAT_THRESHOLD",
    "CREDIT_CAUTION_THRESHOLD": "CAUTION_THRESHOLD",
    "CREDIT_BONUS_SQUEEZE": "BONUS_SQUEEZE",
    "CREDIT_PENALTY_OVERHEAT": "PENALTY_OVERHEAT",
    'B4_CREDIT_DATA_CACHE_HOURS': 'CACHE_HOURS',
}

# 旧テスト・外部呼び出しとの互換名。実装本体は共通ヘルパーへ統一する。
_CFG_THRESHOLD_MAP = _CFG_CONST_MAP


def sync_thresholds_from_config(cfg) -> dict:
    """互換入口: config_sync による実行時同期を行う。"""
    return sync_module_constants(globals(), cfg, _CFG_CONST_MAP)


def _fetch_jpx_csv(url: str, timeout: int = 15) -> Optional[str]:
    """JPX の信用残高 CSV をテキストとして取得する。"""
    try:
        import requests
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept":          "text/csv,application/csv,*/*",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer":         "https://www.jpx.co.jp/",
        }
        r = requests.get(url, headers=headers, timeout=timeout)
        if r.status_code == 200:
            # JPX CSV は Shift-JIS エンコード
            r.encoding = "shift_jis"
            return r.text
        return None
    except Exception as e:
        print(f"    ⚠️  JPX CSV 取得失敗 ({url[:60]}): {str(e)[:60]}")
        return None


def _parse_jpx_csv(text: str) -> list:
    """
    JPX 信用残高 CSV を解析して CreditRecord リストを返す。

    JPX CSV フォーマット（Shift-JIS / 週次）:
      列: 計上基準日, 市場区分, コード, 銘柄名, 信用買残(株), 前週比, 信用売残(株), 前週比, 信用倍率
    """
    records = []
    as_of_date = ""

    try:
        reader = csv.reader(io.StringIO(text))
        header_skipped = False

        for row in reader:
            # 空行・ヘッダー行スキップ
            if not row or len(row) < 5:
                continue
            if not header_skipped:
                # 最初の数値行まで読み飛ばす
                try:
                    int(row[2])  # コード欄が数字ならデータ行
                    header_skipped = True
                except (ValueError, IndexError):
                    continue

            try:
                # 列インデックスは JPX のフォーマットに準拠
                # 0: 計上基準日, 1: 市場区分, 2: コード, 3: 銘柄名
                # 4: 信用買残(株), 5: 前週比(買), 6: 信用売残(株), 7: 前週比(売), 8: 信用倍率
                as_of_raw  = row[0].strip()
                code       = row[2].strip().zfill(4)
                name       = row[3].strip()
                long_raw   = row[4].replace(",", "").strip()
                long_chg   = row[5].replace(",", "").strip()
                short_raw  = row[6].replace(",", "").strip()
                short_chg  = row[7].replace(",", "").strip()

                long_shares  = int(long_raw)   if long_raw.lstrip("-").isdigit()  else 0
                short_shares = int(short_raw)  if short_raw.lstrip("-").isdigit() else 0
                long_change  = int(long_chg)   if long_chg.lstrip("-").isdigit()  else 0
                short_change = int(short_chg)  if short_chg.lstrip("-").isdigit() else 0

                # 信用倍率計算
                if short_shares > 0:
                    margin_ratio = round(long_shares / short_shares, 2)
                elif long_shares > 0:
                    margin_ratio = 99.99   # 売り残ゼロ（買いのみ）
                else:
                    continue   # 両方ゼロはスキップ

                # フラグ付与
                if margin_ratio < SQUEEZE_THRESHOLD:
                    flag = "squeeze"
                elif margin_ratio > OVERHEAT_THRESHOLD:
                    flag = "overheated"
                elif margin_ratio >= CAUTION_THRESHOLD:  # 6.0以上を caution
                    flag = "caution"
                else:
                    flag = "normal"

                ticker = f"{code}.T"
                as_of_date = as_of_raw

                records.append({
                    "ticker":       ticker,
                    "code":         code,
                    "name":         name,
                    "long_shares":  long_shares,
                    "short_shares": short_shares,
                    "long_change":  long_change,
                    "short_change": short_change,
                    "margin_ratio": margin_ratio,
                    "flag":         flag,
                    "as_of_date":   as_of_raw,
                })
            except (IndexError, ValueError):
                continue

    except Exception as e:
        print(f"    ⚠️  CSV 解析エラー: {str(e)[:80]}")

    return records, as_of_date


# ==============================================================
# BLOCK 2: screener ボーナス算出
# ==============================================================

def get_credit_bonus(ticker: str, credit_cache: dict) -> tuple[float, str]:
    """
    stock_screener.py の _calc_scores() から呼び出す。
    信用倍率フラグに応じた bonus と label を返す。

    Args:
        ticker:        例 "6758.T"
        credit_cache:  run_credit_data() の records 辞書

    Returns:
        (bonus: float, label: str)
        bonus: composite_score への加算値
        label: "信用squeeze候補" / "信用過熱⚠️" / ""
    """
    rec = credit_cache.get(ticker)
    if not rec:
        return 0.0, ""
    flag  = rec.get("flag", "normal")
    ratio = rec.get("margin_ratio", 0)
    if flag == "squeeze":
        return BONUS_SQUEEZE, f"信用squeeze({ratio:.1f}倍)"
    elif flag == "overheated":
        return PENALTY_OVERHEAT, f"信用過熱⚠️({ratio:.1f}倍)"
    elif flag == "caution":
        return 0.0, f"信用注意({ratio:.1f}倍)"
    return 0.0, ""


# ==============================================================
# BLOCK 3: キャッシュ管理
# ==============================================================

def _cache_path(cfg) -> Path:
    return Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / CACHE_FILENAME


def _load_cache(cfg) -> Optional[dict]:
    try:
        p = _cache_path(cfg)
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        cached_at = datetime.fromisoformat(data.get("cached_at", "2000-01-01"))
        if datetime.now() - cached_at < timedelta(hours=CACHE_HOURS):
            return data
    except Exception:
        pass
    return None


def _save_cache(cfg, data: dict) -> None:
    try:
        p = _cache_path(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        data["cached_at"] = datetime.now().isoformat()
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"    ⚠️  キャッシュ保存失敗: {e}")


# ==============================================================
# BLOCK 4: アラートテキスト生成
# ==============================================================

def format_credit_alert(result: dict, top_n: int = 8) -> str:
    """
    週次アラート向けのテキストを生成する。

    出力例:
      📊 信用残高週報（2026-03-14基準）
      🟢 踏み上げ候補（倍率<1.0）: 5銘柄
        1332.T 日本水産: 倍率0.72（買い残1.2M / 売り残1.7M）
      🔴 買い偏重注意（倍率>10）: 3銘柄
        6857.T アドバンテスト: 倍率12.4
    """
    as_of = result.get("as_of_date", "N/A")
    total = result.get("total", 0)
    squeeze   = result.get("flag_short_squeeze", [])
    overheat  = result.get("flag_overheated", [])
    caution   = result.get("flag_caution", [])

    lines = [f"📊 信用残高週報（{as_of} 基準 / {total}銘柄）"]

    if squeeze:
        lines.append(f"\n🟢 踏み上げ候補（倍率<{SQUEEZE_THRESHOLD}）: {len(squeeze)}銘柄")
        for r in squeeze[:top_n]:
            lines.append(
                f"  {r['ticker']} {r['name'][:14]}: "
                f"倍率{r['margin_ratio']:.2f} "
                f"（買{r['long_shares']//1000}千株 / 売{r['short_shares']//1000}千株）"
            )
    else:
        lines.append("🟢 踏み上げ候補: なし")

    if overheat:
        lines.append(f"\n🔴 買い偏重注意（倍率>{OVERHEAT_THRESHOLD}）: {len(overheat)}銘柄")
        for r in overheat[:top_n]:
            lines.append(
                f"  {r['ticker']} {r['name'][:14]}: "
                f"倍率{r['margin_ratio']:.1f} ⚠️崩落リスク"
            )

    if caution:
        names = " / ".join(f"{r['ticker']}({r['margin_ratio']:.0f}倍)" for r in caution[:5])
        lines.append(f"\n🟡 高倍率注意（{CAUTION_THRESHOLD}<倍率<{OVERHEAT_THRESHOLD}）: {names}")

    return "\n".join(lines)


# ==============================================================
# BLOCK 5: メインエントリポイント
# ==============================================================

def run_credit_data(cfg) -> dict:
    """
    JPX 信用残高データを取得・解析して返す。

    stock_analyzer_main.py から以下のように呼び出す:
        _cd = _load_local_module("credit_data", ...)
        cd_result = _cd.run_credit_data(cfg)
        results["credit_data"] = cd_result

    Returns:
        {
          "records":            {ticker: CreditRecord},
          "flag_short_squeeze": [CreditRecord, ...],   # 倍率 < 1.0
          "flag_overheated":    [CreditRecord, ...],   # 倍率 > 10.0
          "flag_caution":       [CreditRecord, ...],   # 倍率 6〜10
          "as_of_date":         str,
          "total":              int,
          "summary_text":       str,
          "timestamp":          str,
        }
    """
    print("\n  📊 信用残高データ取得開始...")

    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)

    # キャッシュ確認（週次データなので7日間有効）
    cached = _load_cache(cfg)
    if cached:
        print(f"    ✅ キャッシュ使用（{cached.get('as_of_date','N/A')} / "
              f"踏み上げ{len(cached.get('flag_short_squeeze',[]))}件）")
        return cached

    # JPX CSV 取得
    text = None
    for url in JPX_FALLBACK_URLS:
        text = _fetch_jpx_csv(url)
        if text:
            print(f"    ✅ JPX CSV 取得成功（{len(text):,}文字）")
            break
        time.sleep(1)

    if not text:
        print("    ⚠️  JPX CSV 全URL取得失敗。yfinance フォールバックに切り替えます...")
        return _fallback_via_yfinance(cfg)

    # CSV 解析
    records_list, as_of_date = _parse_jpx_csv(text)

    if not records_list:
        print("    ⚠️  CSV 解析結果が空。yfinance フォールバックに切り替えます...")
        return _fallback_via_yfinance(cfg)

    # ticker → record の辞書形式に変換
    records_dict = {r["ticker"]: r for r in records_list}

    squeeze  = sorted([r for r in records_list if r["flag"] == "squeeze"],
                       key=lambda x: x["margin_ratio"])
    overheat = sorted([r for r in records_list if r["flag"] == "overheated"],
                       key=lambda x: -x["margin_ratio"])
    caution  = sorted([r for r in records_list if r["flag"] == "caution"],
                       key=lambda x: -x["margin_ratio"])

    print(f"    解析完了: {len(records_list)}銘柄 "
          f"/ 踏み上げ候補{len(squeeze)}件 "
          f"/ 過熱{len(overheat)}件 / 注意{len(caution)}件")

    result = {
        "records":            records_dict,
        "flag_short_squeeze": squeeze,
        "flag_overheated":    overheat,
        "flag_caution":       caution,
        "as_of_date":         as_of_date,
        "total":              len(records_list),
        "timestamp":          datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    result["summary_text"] = format_credit_alert(result)

    _save_cache(cfg, result)
    return result


def _fallback_via_yfinance(cfg) -> dict:
    """
    JPX CSV が取得できない場合の yfinance フォールバック。
    SECTOR_REPRESENTATIVE_TICKERS の銘柄について
    yfinance の sharesShort / floatShares から代理値を算出する。
    """
    print("    📡 yfinance フォールバック（代理値）...")
    rep = getattr(cfg, "SECTOR_REPRESENTATIVE_TICKERS", {})
    all_tickers = [t for ts in rep.values() for t in ts[:2]]

    records_dict = {}
    try:
        import yf_cache as yf
        for ticker in all_tickers[:30]:  # 上限30銘柄
            try:
                info = yf.Ticker(ticker).info or {}
                shares_short = info.get("sharesShort", 0) or 0
                float_shares = info.get("floatShares", 0) or 0
                shares_outstanding = info.get("sharesOutstanding", 0) or 0

                if float_shares > 0 and shares_short > 0:
                    # 代理信用倍率 = outstanding / short（yfinanceは買い残が取れないため）
                    short_ratio = round(shares_short / float_shares * 100, 2)
                    margin_ratio = round(1.0 / (shares_short / float_shares), 2) if shares_short > 0 else 99.99
                    flag = "squeeze" if margin_ratio < SQUEEZE_THRESHOLD else (
                           "overheated" if margin_ratio > OVERHEAT_THRESHOLD else "normal")

                    records_dict[ticker] = {
                        "ticker":       ticker,
                        "code":         ticker.replace(".T", ""),
                        "name":         (info.get("longName") or ticker)[:20],
                        "long_shares":  0,      # yfinanceでは不明
                        "short_shares": shares_short,
                        "long_change":  0,
                        "short_change": 0,
                        "margin_ratio": margin_ratio,
                        "flag":         flag,
                        "as_of_date":   datetime.now().strftime("%Y-%m-%d"),
                        "source":       "yfinance_fallback",
                    }
                time.sleep(0.2)
            except Exception:
                continue
    except Exception as e:
        print(f"    ⚠️  yfinance フォールバック失敗: {e}")

    as_of = datetime.now().strftime("%Y-%m-%d")
    squeeze  = [r for r in records_dict.values() if r["flag"] == "squeeze"]
    overheat = [r for r in records_dict.values() if r["flag"] == "overheated"]

    result = {
        "records":            records_dict,
        "flag_short_squeeze": squeeze,
        "flag_overheated":    overheat,
        "flag_caution":       [],
        "as_of_date":         as_of,
        "total":              len(records_dict),
        "source":             "yfinance_fallback",
        "timestamp":          datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    result["summary_text"] = format_credit_alert(result)
    return result
