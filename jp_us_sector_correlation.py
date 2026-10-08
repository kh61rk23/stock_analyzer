# ==============================================================
# jp_us_sector_correlation.py  v1.0  (Idea2-A)
# JP 33業種 × US 11セクター 60日ローリング相関分析
# ==============================================================
# 【役割】
#   score_history.json（JP 33業種 composite_score）と
#   US セクターETF（XLK/XLF等）の日次価格を突き合わせ、
#   直近60日のピアソン相関を業種ペアごとに算出する。
#   相関 |r| >= 0.7 のペアを "significant_pairs" として抽出し、
#   weekly_report_generator が「JP×US相関マトリクス」セクションで参照する。
#
# 【入力（データソース）】
#   ・JP スコア : DATA_DIR/score_history.json
#                 形式: {"YYYY-MM-DD": {"1001": {"score": float, ...}, ...}}
#   ・US 価格   : yfinance（US セクターETF 11銘柄）
#                 フォールバック: INSIGHT_DIR/us_sectors/ の保存済みスコア
#
# 【算出ロジック】
#   1. JP スコア日次系列を前日値フォワードフィルで補完
#   2. US ETF 日次終値を取得 → 前日比リターン（%）に変換
#   3. JP スコア変化量（delta_score）と US リターンを日付アラインメント
#   4. 直近60日分のペアでピアソン相関 r を計算
#   5. |r| >= CORR_THRESHOLD（0.7）のペアを抽出
#
# 【出力スキーマ】
#   {
#     "significant_pairs": [
#       {
#         "jp_code":       str,   # JP 業種コード
#         "jp_name":       str,   # JP 業種名（ja）
#         "us_etf":        str,   # US ETF シンボル
#         "us_sector_ja":  str,   # US セクター名（ja）
#         "correlation":   float, # ピアソン r
#         "n_observations":int,   # 使用データ点数
#         "signal":        str,   # "aligned_up"/"aligned_down"/"mixed"
#         "jp_trend":      float, # JP スコア直近傾き
#         "us_trend":      float, # US ETF 直近リターン平均
#       }, ...
#     ],
#     "correlation_matrix": dict,  # {jp_code: {us_etf: r}} 全ペア
#     "stats": {
#       "total_pairs_checked": int,
#       "significant_count":   int,
#       "lookback_days":       int,
#       "data_points":         int,   # 有効日付数
#       "insufficient_data":   bool,
#     },
#     "cached":    bool,
#     "timestamp": str,
#   }
#
# 【依存】
#   config.py（DATA_DIR / CACHE_DIR / INSIGHT_DIR）
#   score_history.json（既存モジュールが日次蓄積）
#   yfinance（US ETF価格取得）
#   numpy 不使用・標準ライブラリのみで計算
#
# 【消費者】
#   ・weekly_report_generator.py の _section_jp_us_correlation() のみ。
#     significant_pairs / stats を読み「JP×US相関マトリクス（直近60日）」
#     セクションを生成する。スコア補正には一切使われない（レポート表示専用）。
#
# 【他3モジュールとの違い（jp_us_sector_bridge と特に紛らわしいので30秒で分かるように）】
#   ・vs jp_us_sector_bridge.py: 名前が似ているが別物。
#     本モジュール = 「日次」データで「同時（ラグ0）」の60日ローリング相関を算出し、
#     weekly_report に表示するだけ（composite_score には触れない・受動的）。
#     jp_us_sector_bridge.py = 「週次」スコアで「1週先行（ラグ1）」の相関を算出し、
#     有意なものは signal_aggregator 経由で composite_score を実際に±pt補正する
#     （能動的）。判定粒度（日次 vs 週次）とラグ（同時 vs 先行）と
#     用途（レポート表示のみ vs スコア補正）の3点が異なる。
#   ・vs sector_rotation_detector.py: 本モジュールは2系列（JP×US）の相関を見るが、
#     rotation_detector は1系列（JPスコア単体）のトレンド象限を見る。比較対象が違う。
#   ・vs us_sector_bridge_enhanced.py: 本モジュールは価格・スコアの統計的相関。
#     us_sector_bridge_enhanced.py はSICコード分類によるルールベースの
#     イベント（8-K決算）伝播で、算出方法が全く異なる。
#
# 【既知の状態】
#   有意ペアは0件が続いている。MIN_DATA_POINTS=20 は満たしていても、
#   LOOKBACK_DAYS=60 の設計に対してデータ蓄積が足りず、安定した有意相関が出にくい。
#   意思決定 J-04 = a: score_history が60日蓄積されるまで存廃判断を保留（削除・無効化しない）。
# ==============================================================

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


# ==============================================================
# BLOCK 0: 定数
# ==============================================================


import finance_logic
from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'JUSC_CORR_THRESHOLD': 'CORR_THRESHOLD',
    'B4_JP_US_SECTOR_CORRELATION_LOOKBACK_DAYS': 'LOOKBACK_DAYS',
    'B4_JP_US_SECTOR_CORRELATION_FETCH_BUFFER_DAYS': 'FETCH_BUFFER_DAYS',
    'B4_JP_US_SECTOR_CORRELATION_CACHE_TTL_DAYS': 'CACHE_TTL_DAYS',
    'B6_JP_US_SECTOR_CORRELATION_MIN_DATA_POINTS': 'MIN_DATA_POINTS',
}

CORR_THRESHOLD   = 0.70   # 有意とみなす相関係数の絶対値
LOOKBACK_DAYS    = 60     # 相関計算ウィンドウ（日）
MIN_DATA_POINTS  = 20     # 最低必要データ点数
FETCH_BUFFER_DAYS = 90    # yfinance 取得バッファ（LOOKBACK + 余裕）

CACHE_FILENAME   = "jp_us_sector_corr_cache.json"
CACHE_TTL_DAYS   = 7

# US セクターETF 11銘柄と日本語セクター名
US_SECTOR_ETFS: dict[str, str] = {
    "XLK":  "情報技術",
    "XLY":  "一般消費財",
    "XLF":  "金融",
    "XLV":  "ヘルスケア",
    "XLE":  "エネルギー",
    "XLB":  "素材",
    "XLI":  "資本財",
    "XLC":  "通信サービス",
    "XLU":  "公益事業",
    "XLRE": "不動産",
    "XLP":  "生活必需品",
}

# JP業種コード → 主要対応 US ETF（優先検索ペア）
JP_US_AFFINITY: dict[str, str] = {
    "1004": "XLP",    # 食料品   ↔ 生活必需品
    "1007": "XLB",    # 化学     ↔ 素材
    "1008": "XLV",    # 医薬品   ↔ ヘルスケア
    "1009": "XLE",    # 石油     ↔ エネルギー
    "1012": "XLB",    # 鉄鋼     ↔ 素材
    "1013": "XLB",    # 非鉄     ↔ 素材
    "1015": "XLI",    # 機械     ↔ 資本財
    "1016": "XLK",    # 電気機器 ↔ 情報技術
    "1017": "XLY",    # 輸送用機器 ↔ 一般消費財
    "1020": "XLU",    # 電力ガス ↔ 公益事業
    "1025": "XLC",    # 情報通信 ↔ 通信サービス
    "1027": "XLY",    # 小売業   ↔ 一般消費財
    "1028": "XLF",    # 銀行業   ↔ 金融
    "1030": "XLF",    # 保険業   ↔ 金融
    "1032": "XLRE",   # 不動産   ↔ 不動産
}


# ==============================================================
# BLOCK 1: キャッシュ管理
# ==============================================================

def _cache_path(cfg) -> Path:
    d = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache"))
    d.mkdir(parents=True, exist_ok=True)
    return d / CACHE_FILENAME


def _load_cache(cfg) -> Optional[dict]:
    try:
        p = _cache_path(cfg)
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        cached_at = data.get("cached_at", "")
        if cached_at:
            age_days = (datetime.now() - datetime.fromisoformat(cached_at)).days
            if age_days < CACHE_TTL_DAYS:
                return data
    except Exception:
        pass
    return None


def _save_cache(cfg, data: dict) -> None:
    try:
        data["cached_at"] = datetime.now().isoformat()
        p = _cache_path(cfg)
        p.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"    ⚠️  [corr] キャッシュ保存失敗: {str(e)[:60]}")


# ==============================================================
# BLOCK 2: データ読み込み
# ==============================================================

def _load_jp_score_history(cfg) -> dict:
    """
    score_history.json を読み込む。
    形式: {"YYYY-MM-DD": {"1001": {"score": float}, ...}}
    保存場所: DATA_DIR/score_history.json（主）/ insights/score_history.json（副）
    """
    candidates = [
        Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / "score_history.json",
        Path(getattr(cfg, "DATA_DIR", "stock_data/data")).parent
        / "insights" / "score_history.json",
        Path("stock_data/data/score_history.json"),
        Path("stock_data/insights/score_history.json"),
    ]
    for p in candidates:
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data:
                    return data
            except Exception:
                pass
    return {}


def _fetch_us_etf_prices(
    etf_symbols: list[str],
    lookback_days: int = FETCH_BUFFER_DAYS,
) -> dict[str, dict[str, float]]:
    """
    yfinance で US ETF の日次終値を取得する。

    Returns:
        {"YYYY-MM-DD": {"XLK": price_float, ...}}
        失敗時は空 dict を返す（例外を投げない）
    """
    try:
        import yf_cache as yf
    except ImportError:
        print("    ⚠️  [corr] yfinance が未インストールです")
        return {}

    end_dt   = datetime.now()
    start_dt = end_dt - timedelta(days=lookback_days)

    try:
        tickers_str = " ".join(etf_symbols)
        # yfinance の multi-ticker DL
        df = yf.download(
            tickers_str,
            start=start_dt.strftime("%Y-%m-%d"),
            end=end_dt.strftime("%Y-%m-%d"),
            auto_adjust=True,
            progress=False,
        )
        if df is None or df.empty:
            return {}

        # マルチカラム対応（tickers が複数の場合は ("Close", "XLK") 形式）
        result: dict[str, dict[str, float]] = {}
        close_df = None

        try:
            if hasattr(df.columns, "levels"):
                # MultiIndex
                close_df = df["Close"]
            else:
                close_df = df[["Close"]] if "Close" in df.columns else df
        except Exception:
            close_df = df

        if close_df is None or close_df.empty:
            return {}

        for date_idx in close_df.index:
            try:
                date_str = date_idx.strftime("%Y-%m-%d")
                day_data: dict[str, float] = {}
                for sym in etf_symbols:
                    try:
                        val = close_df.at[date_idx, sym]
                        if val is not None and not math.isnan(float(val)):
                            day_data[sym] = round(float(val), 4)
                    except Exception:
                        pass
                if day_data:
                    result[date_str] = day_data
            except Exception:
                pass

        return result

    except Exception as e:
        print(f"    ⚠️  [corr] yfinance 取得失敗: {str(e)[:80]}")
        return {}


def _load_us_score_history_fallback(cfg) -> dict[str, dict[str, float]]:
    """
    yfinance が失敗した場合のフォールバック:
    us_sector_pipeline の保存済みスコアを読み込む。
    Returns: {"YYYY-MM-DD": {"XLK": score_float, ...}}
    """
    us_dir_candidates = [
        Path(getattr(cfg, "DATA_DIR", "stock_data/data")).parent
        / "insights" / "us_sectors",
        Path("stock_data/insights/us_sectors"),
    ]
    result: dict[str, dict[str, float]] = {}
    for d in us_dir_candidates:
        if not d.exists():
            continue
        for f in sorted(d.glob("us_sector_scores_*.json"))[-FETCH_BUFFER_DAYS:]:
            try:
                date_str = f.stem.replace("us_sector_scores_", "")
                raw = json.loads(f.read_text(encoding="utf-8"))
                day: dict[str, float] = {}
                for etf, info in raw.get("sectors", {}).items():
                    s = (info.get("composite_score") or info.get("score")
                         if isinstance(info, dict) else info)
                    if s is not None:
                        day[etf] = float(s)
                if day:
                    result[date_str] = day
            except Exception:
                continue
        break
    return result


# ==============================================================
# BLOCK 3: 時系列前処理
# ==============================================================

def _extract_jp_daily_series(
    score_history: dict,
    sector_codes: list[str],
) -> dict[str, dict[str, Optional[float]]]:
    """
    score_history から各業種の日次スコア系列を抽出する。
    形式: {sector_code: {"YYYY-MM-DD": score_or_None}}
    """
    series: dict[str, dict[str, Optional[float]]] = {c: {} for c in sector_codes}

    for date_str, day_data in score_history.items():
        if not isinstance(day_data, dict):
            continue
        for code in sector_codes:
            entry = day_data.get(code)
            if entry is None:
                continue
            if isinstance(entry, dict):
                score = entry.get("score") or entry.get("composite_score")
            else:
                try:
                    score = float(entry)
                except (TypeError, ValueError):
                    score = None
            series[code][date_str] = float(score) if score is not None else None

    return series


def _forward_fill(series: dict[str, Optional[float]]) -> dict[str, float]:
    """
    日付キー昇順にフォワードフィルして None を除去する。
    score_history は実行日のみ更新されるため、非更新日を前日値で補完する。
    """
    filled: dict[str, float] = {}
    last_val: Optional[float] = None
    for date_str in sorted(series.keys()):
        val = series[date_str]
        if val is not None:
            last_val = val
        if last_val is not None:
            filled[date_str] = last_val
    return filled


def _prices_to_returns(
    price_series: dict[str, dict[str, float]],
) -> dict[str, dict[str, float]]:
    """
    日次終値 → 前日比リターン（%）に変換する。
    Returns: {etf_sym: {"YYYY-MM-DD": return_pct}}
    """
    returns: dict[str, dict[str, float]] = {}
    # ETF ごとに変換
    all_syms: set[str] = set()
    for day_data in price_series.values():
        all_syms.update(day_data.keys())

    for sym in all_syms:
        sym_returns: dict[str, float] = {}
        sorted_dates = sorted(price_series.keys())
        prev_price: Optional[float] = None
        for date_str in sorted_dates:
            price = price_series.get(date_str, {}).get(sym)
            if price is not None and prev_price is not None and prev_price != 0:
                ret = (price - prev_price) / prev_price * 100
                sym_returns[date_str] = round(ret, 6)
            if price is not None:
                prev_price = price
        returns[sym] = sym_returns
    return returns


def _score_deltas(filled: dict[str, float]) -> dict[str, float]:
    """
    前日値フォワードフィル済みスコア → 日次変化量に変換する。
    Returns: {"YYYY-MM-DD": delta_score}
    """
    deltas: dict[str, float] = {}
    sorted_dates = sorted(filled.keys())
    for i in range(1, len(sorted_dates)):
        d_cur  = sorted_dates[i]
        d_prev = sorted_dates[i - 1]
        deltas[d_cur] = round(filled[d_cur] - filled[d_prev], 4)
    return deltas


# ==============================================================
# BLOCK 4: ピアソン相関計算
# ==============================================================

def _pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    """ピアソン相関係数を算出する。共通実装は finance_logic.pearson（U-7）。"""
    return finance_logic.pearson(xs, ys, min_n=MIN_DATA_POINTS, round_digits=4)


def _calc_trend(values: list[float]) -> float:
    """共通実装は finance_logic.linear_slope（Batch 10）。"""
    return finance_logic.linear_slope(values, min_n=3, round_digits=6)


def _rolling_corr_60d(
    jp_delta_series:  dict[str, float],
    us_return_series: dict[str, float],
) -> tuple[Optional[float], int]:
    """
    直近 LOOKBACK_DAYS 日分の JP スコア変化量 と US リターンの
    ピアソン相関を算出する。

    Returns:
        (correlation, n_observations)
    """
    # 共通日付のうち直近 LOOKBACK_DAYS 日を抽出
    common_dates = sorted(
        set(jp_delta_series.keys()) & set(us_return_series.keys())
    )[-LOOKBACK_DAYS:]

    if len(common_dates) < MIN_DATA_POINTS:
        return None, len(common_dates)

    jp_vals = [jp_delta_series[d] for d in common_dates]
    us_vals = [us_return_series[d] for d in common_dates]

    r = _pearson(jp_vals, us_vals)
    return r, len(common_dates)


# ==============================================================
# BLOCK 5: シグナル判定
# ==============================================================

def _classify_signal(
    jp_filled: dict[str, float],
    us_return_series: dict[str, float],
    window: int = 14,
) -> str:
    """
    直近 window 日の JP スコアトレンドと US リターン平均から
    シグナルを分類する。
    - "aligned_up":   JP上昇 × US上昇 → 両市場で追い風
    - "aligned_down": JP下落 × US下落 → 両市場で逆風
    - "mixed":        方向が異なる → 乖離注意
    """
    try:
        jp_dates = sorted(jp_filled.keys())[-window:]
        jp_vals  = [jp_filled[d] for d in jp_dates]
        jp_trend = _calc_trend(jp_vals)

        us_dates = sorted(us_return_series.keys())[-window:]
        us_vals  = [us_return_series[d] for d in us_dates]
        us_avg   = sum(us_vals) / len(us_vals) if us_vals else 0.0

        if jp_trend > 0 and us_avg > 0:
            return "aligned_up"
        if jp_trend < 0 and us_avg < 0:
            return "aligned_down"
        return "mixed"
    except Exception:
        return "mixed"


# ==============================================================
# BLOCK 6: メイン実行フロー
# ==============================================================

def run_jp_us_sector_correlation(cfg) -> dict:
    """
    stock_analyzer_main.py の run_all_tasks() から呼ばれるエントリポイント。
    WEEKLY_MODULES に "jp_us_sector_correlation" を追加して使用する。

    処理フロー:
      1. キャッシュ有効 → キャッシュ返却
      2. score_history.json を読み込み JP 日次スコア系列を構築
      3. yfinance で US ETF 日次価格を取得（失敗時はフォールバック）
      4. JP: スコア変化量 / US: 日次リターン に変換
      5. 全 JP×US ペアの 60日ローリング相関を計算
      6. |r| >= 0.7 のペアを significant_pairs として抽出
      7. キャッシュ保存して結果を返す

    Returns:
        {
          "significant_pairs":  list,
          "correlation_matrix": dict,
          "stats":              dict,
          "cached":             bool,
          "timestamp":          str,
        }
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    print("\n  📊 JP×US相関分析（Idea2-A）実行中...")

    # ── キャッシュ確認 ───────────────────────────────────────────
    cached = _load_cache(cfg)
    if cached:
        n_sig = len(cached.get("significant_pairs", []))
        print(f"    ✅ キャッシュ使用: 有意ペア {n_sig}件")
        cached["cached"] = True
        return cached

    # ── 1. JP スコア読み込み ─────────────────────────────────────
    score_history = _load_jp_score_history(cfg)
    if not score_history:
        print("    ⚠️  score_history.json が見つからないため、スキップします")
        return _empty_result(insufficient_data=True)

    # 対象業種コード（score_historyに登録済みのもの）
    all_jp_codes: set[str] = set()
    for day_data in score_history.values():
        if isinstance(day_data, dict):
            all_jp_codes.update(day_data.keys())
    jp_codes = sorted(all_jp_codes)
    print(f"    📋 JP 業種数: {len(jp_codes)}業種 / 履歴日数: {len(score_history)}日")

    if len(score_history) < MIN_DATA_POINTS:
        print(f"    ⚠️  データ不足（{len(score_history)}日 < {MIN_DATA_POINTS}日）")
        return _empty_result(insufficient_data=True)

    # ── 2. JP 日次スコア系列の構築 ───────────────────────────────
    jp_raw_series  = _extract_jp_daily_series(score_history, jp_codes)
    jp_filled: dict[str, dict[str, float]] = {}
    jp_delta:  dict[str, dict[str, float]] = {}
    for code in jp_codes:
        filled = _forward_fill(jp_raw_series[code])
        if len(filled) >= MIN_DATA_POINTS:
            jp_filled[code] = filled
            jp_delta[code]  = _score_deltas(filled)

    if not jp_delta:
        print("    ⚠️  有効なJPスコア系列がありません")
        return _empty_result(insufficient_data=True)

    # ── 3. US ETF 価格取得 ─────────────────────────────────────
    etf_symbols = list(US_SECTOR_ETFS.keys())
    print(f"    📡 US ETF取得中（{len(etf_symbols)}銘柄, {FETCH_BUFFER_DAYS}日）...")

    us_price_data = _fetch_us_etf_prices(etf_symbols, FETCH_BUFFER_DAYS)
    if not us_price_data:
        print("    ⚠️  yfinance失敗 → フォールバックデータを使用")
        us_price_data = _load_us_score_history_fallback(cfg)

    if not us_price_data:
        print("    ⚠️  US ETFデータ取得失敗 → スキップ")
        return _empty_result(insufficient_data=True)

    # ── 4. US リターン系列の構築 ─────────────────────────────────
    us_returns = _prices_to_returns(us_price_data)
    valid_etfs = [sym for sym in etf_symbols if len(us_returns.get(sym, {})) >= MIN_DATA_POINTS]
    print(f"    ✅ US ETF有効銘柄: {len(valid_etfs)}/{len(etf_symbols)}銘柄")

    # ── 5. 全ペアの相関計算 ──────────────────────────────────────
    japan_sectors = getattr(cfg, "JAPAN_SECTORS", {})
    corr_matrix:   dict[str, dict[str, Optional[float]]] = {}
    significant_pairs: list[dict] = []
    total_pairs = 0

    for jp_code in jp_codes:
        if jp_code not in jp_delta:
            continue
        corr_matrix[jp_code] = {}
        jp_name = japan_sectors.get(jp_code, {}).get("name_ja", jp_code)

        for etf in valid_etfs:
            total_pairs += 1
            r, n_obs = _rolling_corr_60d(jp_delta[jp_code], us_returns[etf])
            corr_matrix[jp_code][etf] = r

            if r is not None and abs(r) >= CORR_THRESHOLD:
                signal = _classify_signal(
                    jp_filled.get(jp_code, {}),
                    us_returns[etf],
                )
                # JP スコアトレンド（直近14日）
                jp_vals_14 = [
                    jp_filled[jp_code][d]
                    for d in sorted(jp_filled.get(jp_code, {}).keys())[-14:]
                ]
                jp_trend_14 = _calc_trend(jp_vals_14) if jp_vals_14 else 0.0

                # US リターン平均（直近14日）
                us_ret_14 = [
                    us_returns[etf][d]
                    for d in sorted(us_returns[etf].keys())[-14:]
                ]
                us_avg_14 = round(sum(us_ret_14) / len(us_ret_14), 4) if us_ret_14 else 0.0

                significant_pairs.append({
                    "jp_code":        jp_code,
                    "jp_name":        jp_name,
                    "us_etf":         etf,
                    "us_sector_ja":   US_SECTOR_ETFS.get(etf, etf),
                    "correlation":    r,
                    "n_observations": n_obs,
                    "signal":         signal,
                    "jp_trend":       round(jp_trend_14, 4),
                    "us_trend":       us_avg_14,
                    "affinity_match": JP_US_AFFINITY.get(jp_code) == etf,
                })

    # 相関の絶対値降順でソート
    significant_pairs.sort(key=lambda x: abs(x["correlation"]), reverse=True)

    print(f"    📊 全ペア: {total_pairs} / 有意ペア(|r|≥{CORR_THRESHOLD}): "
          f"{len(significant_pairs)}件")
    for p in significant_pairs[:5]:
        sign = "+" if p["correlation"] > 0 else ""
        print(f"       {p['jp_name']} × {p['us_etf']}({p['us_sector_ja']}): "
              f"r={sign}{p['correlation']:.3f} [{p['signal']}]")

    # ── 6. 有効な日付数を集計 ────────────────────────────────────
    all_dates: set[str] = set()
    for d in jp_delta.values():
        all_dates.update(d.keys())

    result: dict = {
        "significant_pairs":  significant_pairs,
        "correlation_matrix": corr_matrix,
        "stats": {
            "total_pairs_checked": total_pairs,
            "significant_count":   len(significant_pairs),
            "lookback_days":       LOOKBACK_DAYS,
            "data_points":         len(all_dates),
            "jp_codes_analyzed":   len(jp_delta),
            "us_etfs_analyzed":    len(valid_etfs),
            "insufficient_data":   False,
        },
        "cached":    False,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    _save_cache(cfg, result)
    print(f"  ✅ JP×US相関分析完了: 有意ペア {len(significant_pairs)}件")
    return result


def _empty_result(insufficient_data: bool = False) -> dict:
    return {
        "significant_pairs":  [],
        "correlation_matrix": {},
        "stats": {
            "total_pairs_checked": 0,
            "significant_count":   0,
            "lookback_days":       LOOKBACK_DAYS,
            "data_points":         0,
            "insufficient_data":   insufficient_data,
        },
        "cached":    False,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ==============================================================
# BLOCK 7: 単体テスト用エントリポイント
# ==============================================================

if __name__ == "__main__":
    import importlib.util, sys

    cfg = None
    for p in ["/content/config.py", str(Path(__file__).parent / "config.py")]:
        if Path(p).exists():
            spec = importlib.util.spec_from_file_location("config", p)
            if spec and spec.loader:
                cfg = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(cfg)
                break

    if cfg is None:
        print("ERROR: config.py が読み込めません")
        sys.exit(1)

    result = run_jp_us_sector_correlation(cfg)
    stats  = result.get("stats", {})
    print(f"\n--- 結果サマリー ---")
    print(f"検証ペア数     : {stats.get('total_pairs_checked', 0)}")
    print(f"有意ペア(r≥0.7): {stats.get('significant_count', 0)}")
    print(f"データ点数     : {stats.get('data_points', 0)}日")

    pairs = result.get("significant_pairs", [])
    if pairs:
        print(f"\n--- 有意相関ペア TOP10 ---")
        for p in pairs[:10]:
            sign = "+" if p["correlation"] > 0 else ""
            print(f"  [{sign}{p['correlation']:.3f}] "
                  f"{p['jp_name']:12s} × {p['us_etf']:5s}({p['us_sector_ja']:8s}) "
                  f"[{p['signal']}] n={p['n_observations']}")
