# ==============================================================
# google_trends_pipeline.py  v1.0  (RESEARCH-3)
# Googleトレンド × 求人倍率 オルタナティブデータ統合
# ==============================================================
# 【役割】
#   製品・サービス名の検索量増加（2〜4週後の売上先行指標）と
#   有効求人倍率の変化（2〜3ヶ月後の業績先行指標）を無料で取得し、
#   LEADING_INDICATOR_RULES への入力シグナルを生成する。
#
# 【データソース】
#   Googleトレンド: pytrends（非公式・無料）
#                   週次1回のみ実行（Googleブロック対策）
#   有効求人倍率:    gov_data_pipeline が e-Stat から取得済みの値を再利用
#                   → 新規 API 呼び出しなし
#
# 【出力スキーマ（trends_cache.json）】
#   {
#     "cached_at": "YYYY-MM-DD",
#     "sectors": {
#       "1016": {
#         "keywords":    ["半導体", "AI チップ"],
#         "trend_score": 78,    # 0〜100
#         "mom_growth":  0.23,  # 前月比増加率（小数）
#         "yoy_growth":  0.85,  # 前年比増加率
#         "signal":      "tailwind",  # "tailwind"/"headwind"/"neutral"
#         "pts_adj":     3.0,   # LEADING_INDICATOR_RULES への加算点
#       }, ...
#     },
#     "job_offers_signal": {
#       "value":   1.35,   # 有効求人倍率
#       "yoy":     0.05,   # 前年比変化
#       "signal":  "tailwind",
#       "sectors": ["1033", "1027"],  # 恩恵業種
#     }
#   }
#
# 【GitHub Actions 注意点】
#   - pytrends は週1回のみ実行（WEEKLY_MODULES）
#   - Googleにブロックされた場合はキャッシュ継続使用（7日有効）
#   - 失敗時は空の trend_score を返して後続処理を止めない設計
#
# 【依存】pytrends（要インストール）/ config.py / gov_data_pipeline
# 【呼出】from google_trends_pipeline import run_google_trends
# ==============================================================

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Optional


# ==============================================================
# BLOCK 0: 定数
# ==============================================================

TRENDS_CACHE_FILE  = "trends_cache.json"
CACHE_VALID_DAYS   = 7      # キャッシュ有効日数
FETCH_SLEEP_SEC    = 5.0    # リクエスト間隔（Googleブロック対策）
LOOKBACK_WEEKS     = 13     # 過去13週のトレンドデータを取得（約3ヶ月）

# 業種別キーワード定義（Googleトレンド検索語）
# 各業種の主力製品・サービス名を日本語で指定
DEFAULT_TREND_KEYWORDS: dict[str, list[str]] = {
    "1001": ["農業 IoT", "スマート農業"],
    "1002": ["石炭", "石油 価格"],
    "1003": ["建設 DX", "住宅 着工"],
    "1004": ["食品 物価", "食料品 高騰"],
    "1005": ["化学繊維", "繊維 素材"],
    "1006": ["製紙 需要", "紙 パルプ"],
    "1007": ["化学品 需要", "スペシャルティ化学"],
    "1008": ["新薬 承認", "医薬品 処方"],
    "1009": ["石油 価格", "ガソリン"],
    "1010": ["タイヤ", "ゴム 需要"],
    "1011": ["ガラス 需要", "セメント"],
    "1012": ["鉄鋼 価格", "鉄 需要"],
    "1013": ["銅 価格", "非鉄金属"],
    "1014": ["金属部品 需要"],
    "1015": ["産業機械", "ロボット 需要", "工場自動化"],
    "1016": ["半導体 需要", "AI チップ", "NVIDIA 日本"],
    "1017": ["EV 自動車", "電気自動車", "トヨタ"],
    "1018": ["精密機器", "医療機器 需要"],
    "1019": ["ゲーム 市場", "エンタメ"],
    "1020": ["電力 需要", "再生可能エネルギー"],
    "1021": ["物流 需要", "宅配便"],
    "1022": ["海運 市況", "BDI 指数"],
    "1023": ["航空 旅客", "旅行 需要"],
    "1024": ["倉庫 物流"],
    "1025": ["通信 需要", "5G", "データセンター"],
    "1026": ["卸売 商社"],
    "1027": ["ネット通販", "EC 売上", "楽天"],
    "1028": ["銀行 ローン", "住宅ローン 金利"],
    "1029": ["証券 投資", "株式 投資"],
    "1030": ["保険 需要"],
    "1031": ["消費者金融", "カードローン"],
    "1032": ["不動産 価格", "マンション 購入"],
    "1033": ["外食 需要", "レストラン 予約", "観光"],
}

# トレンドスコアの閾値

from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'GTP_TREND_TAILWIND_THRESHOLD': 'TREND_TAILWIND_THRESHOLD',
    'GTP_TREND_HEADWIND_THRESHOLD': 'TREND_HEADWIND_THRESHOLD',
    'GTP_TREND_MOM_BOOST_THRESHOLD': 'TREND_MOM_BOOST_THRESHOLD',
    'B4_GOOGLE_TRENDS_PIPELINE_CACHE_VALID_DAYS': 'CACHE_VALID_DAYS',
    'B4_GOOGLE_TRENDS_PIPELINE_LOOKBACK_WEEKS': 'LOOKBACK_WEEKS',
    'B5_GOOGLE_TRENDS_PIPELINE_FETCH_SLEEP_SEC': 'FETCH_SLEEP_SEC',
    'B6_GOOGLE_TRENDS_PIPELINE_TREND_TAILWIND_PTS': 'TREND_TAILWIND_PTS',
}

TREND_TAILWIND_THRESHOLD  = 60   # 60以上 → 追い風
TREND_HEADWIND_THRESHOLD  = 40   # 40未満 → 逆風
TREND_MOM_BOOST_THRESHOLD = 0.20 # 前月比+20%超 → +3pt ブースト

# LEADING_INDICATOR_RULES への加算点
TREND_TAILWIND_PTS = 3.0
TREND_HEADWIND_PTS = -3.0


# ==============================================================
# BLOCK 1: キャッシュ管理
# ==============================================================

def _cache_path(cfg) -> Path:
    return Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / TRENDS_CACHE_FILE


def _load_cache(cfg) -> Optional[dict]:
    """
    有効期間内のキャッシュを返す。
    期限切れ or 存在しない場合は None。
    """
    p = _cache_path(cfg)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        cached_at = datetime.fromisoformat(data.get("cached_at", "2000-01-01"))
        if datetime.now() - cached_at < timedelta(days=CACHE_VALID_DAYS):
            return data
    except Exception:
        pass
    return None


def _save_cache(cfg, cache: dict) -> None:
    p = _cache_path(cfg)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        cache["cached_at"] = datetime.now().isoformat()
        p.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"  ⚠️  trends_cache 保存失敗: {e}")


# ==============================================================
# BLOCK 2: Googleトレンド取得
# ==============================================================

def _try_import_pytrends():
    """pytrends をインポートする。失敗時は None。"""
    try:
        from pytrends.request import TrendReq
        return TrendReq
    except ImportError:
        return None


def _fetch_trend_for_sector(
    pytrends_cls,
    sector_code: str,
    keywords:    list[str],
    geo:         str = "JP",
) -> Optional[dict]:
    """
    1業種のGoogleトレンドデータを取得する。

    Args:
        pytrends_cls: TrendReq クラス
        sector_code:  業種コード
        keywords:     検索キーワードリスト（最大5件）
        geo:          地域コード（日本: "JP"）

    Returns:
        {
          "trend_score": float,   # 直近4週の平均（0〜100）
          "mom_growth":  float,   # 前月比増加率
          "yoy_growth":  float,   # 前年比増加率
        }
        取得失敗時は None
    """
    try:
        pt = pytrends_cls(hl="ja-JP", tz=540, timeout=(10, 30))
        # キーワードは最大5件（pytrends の制約）
        kw_list = keywords[:5]

        pt.build_payload(
            kw_list,
            cat=0,
            timeframe=f"today {LOOKBACK_WEEKS}-w",
            geo=geo,
        )
        interest = pt.interest_over_time()

        if interest is None or interest.empty:
            return None

        # キーワードごとの週次値の平均を業種スコアとする
        kw_cols = [c for c in interest.columns if c != "isPartial"]
        if not kw_cols:
            return None

        # 全キーワードの平均値で週次系列を構成
        weekly_avg = interest[kw_cols].mean(axis=1).tolist()
        if not weekly_avg:
            return None

        n = len(weekly_avg)
        # 直近4週の平均（trend_score）
        recent_4w  = sum(weekly_avg[-4:]) / min(4, n)
        # 前月（5〜8週前）の平均
        prev_4w    = sum(weekly_avg[-8:-4]) / min(4, max(1, n - 4)) if n > 4 else recent_4w
        # 前年同期（49〜52週前）の平均
        yoy_start  = max(0, n - 52)
        yoy_end    = max(0, n - 48)
        prev_year  = (
            sum(weekly_avg[yoy_start:yoy_end]) / max(1, yoy_end - yoy_start)
            if yoy_end > yoy_start else recent_4w
        )

        # 成長率計算（ゼロ除算対策）
        mom_growth = (recent_4w - prev_4w)   / max(1.0, prev_4w)
        yoy_growth = (recent_4w - prev_year) / max(1.0, prev_year)

        return {
            "trend_score": round(recent_4w, 1),
            "mom_growth":  round(mom_growth, 4),
            "yoy_growth":  round(yoy_growth, 4),
        }

    except Exception as e:
        err_str = str(e)[:60]
        if "429" in err_str or "rate" in err_str.lower():
            print(f"    🚫 Googleレート制限: {sector_code}")
        return None


# ==============================================================
# BLOCK 3: スコア正規化とシグナル判定
# ==============================================================

def _normalize_to_score(trend_score: float, mom_growth: float, cfg=None) -> tuple[float, str, float]:
    """
    Googleトレンドの生スコア（0〜100）とMoM成長率から
    業種評価用のスコアとシグナルを生成する。

    Googleトレンドの数値は相対値（期間内最大値=100）のため、
    絶対値ではなく前月比変化率をメインシグナルとする。

    Returns:
        (normalized_score: 0〜100, signal: str, pts_adj: float)
    """
    # MoM成長率を 0〜100 スコアに変換
    # -50%〜+100% を 0〜100 にマッピング
    mom_score = max(0.0, min(100.0, (mom_growth + 0.5) / 1.5 * 100))

    # trend_score（生数値）と mom_score の平均
    final_score = round((trend_score * 0.4 + mom_score * 0.6), 1)

    # シグナル判定
    # 2026-07-25 バッチ6: 前月比ブースト閾値を config.TREND_MOM_THRESHOLD から読む
    # （従来はモジュール定数 TREND_MOM_BOOST_THRESHOLD のみが効いていた）
    _mom_th = float(getattr(cfg, "TREND_MOM_THRESHOLD", TREND_MOM_BOOST_THRESHOLD))
    if final_score >= TREND_TAILWIND_THRESHOLD or mom_growth >= _mom_th:
        signal  = "tailwind"
        pts_adj = TREND_TAILWIND_PTS
        # MoM20%超は追加ブースト
        if mom_growth >= _mom_th * 2:
            pts_adj += 2.0
    elif final_score < TREND_HEADWIND_THRESHOLD:
        signal  = "headwind"
        pts_adj = TREND_HEADWIND_PTS
    else:
        signal  = "neutral"
        pts_adj = 0.0

    return final_score, signal, round(pts_adj, 1)


# ==============================================================
# BLOCK 4: 求人倍率シグナル生成
# ==============================================================

def _calc_job_offers_signal(gov_result: dict) -> Optional[dict]:
    """
    gov_data_pipeline の結果から有効求人倍率シグナルを生成する。

    gov_result["signals"] または gov_result["stats"] から
    unemployment / job_offers 関連データを取得する。
    データがない場合は None を返す（API呼び出しはしない）。
    """
    if not gov_result or not isinstance(gov_result, dict):
        return None

    # gov_data_pipeline の出力形式から取得
    # 完全失業率を求人倍率の代理変数として使用
    signals = gov_result.get("signals", [])
    unemployment_data = None
    wages_data = None

    for sig in signals:
        if sig.get("id") == "unemployment":
            unemployment_data = sig
        elif sig.get("id") == "wages":
            wages_data = sig

    if unemployment_data is None:
        return None

    yoy = unemployment_data.get("yoy")
    if yoy is None:
        return None

    # 失業率の前年比が低下（改善）→ 求人が多い → 先行追い風
    # 失業率の前年比が上昇 → 求人が減少 → 先行逆風
    if yoy < -0.1:    # 0.1%以上改善
        signal = "tailwind"
        sectors = ["1033", "1027", "1003", "1021"]  # サービス・小売・建設・陸運
        desc = f"失業率改善（前年比{yoy:+.2f}%）→ 採用拡大・業績先行追い風"
    elif yoy > 0.1:   # 0.1%以上悪化
        signal = "headwind"
        sectors = ["1033", "1027", "1028"]
        desc = f"失業率悪化（前年比{yoy:+.2f}%）→ 消費・サービス業に逆風"
    else:
        signal = "neutral"
        sectors = []
        desc = f"失業率横ばい（前年比{yoy:+.2f}%）"

    return {
        "value":   unemployment_data.get("latest_value"),
        "yoy":     yoy,
        "signal":  signal,
        "sectors": sectors,
        "desc":    desc,
    }


# ==============================================================
# BLOCK 5: LEADING_INDICATOR_RULES への統合出力
# ==============================================================

def build_leading_indicator_signals(trends_cache: dict) -> list:
    """
    trends_cache から LEADING_INDICATOR_RULES 形式のシグナルリストを生成する。

    stock_analyzer_main.py の evaluate_leading_indicators() と同じ
    シグナル形式で返すため、そのまま results["leading_indicator_signals"]
    に追加できる。

    Returns:
        [{
          "rule_id":     "GTREND_{sector_code}",
          "label":       str,
          "sector_code": str,
          "signal":      "tailwind"/"headwind"/"neutral",
          "pts_adj":     float,
          "source":      "google_trends",
          "trend_score": float,
          "mom_growth":  float,
        }, ...]
    """
    signals = []
    sectors_data = trends_cache.get("sectors", {})

    for code, data in sectors_data.items():
        signal  = data.get("signal", "neutral")
        pts_adj = data.get("pts_adj", 0.0)
        if abs(pts_adj) < 0.1:
            continue   # neutralシグナルは出力しない

        signals.append({
            "rule_id":     f"GTREND_{code}",
            "label":       f"Googleトレンド({data.get('keywords', [code])[:1][0] if data.get('keywords') else code})",
            "sector_code": code,
            "signal":      signal,
            "pts_adj":     pts_adj,
            "source":      "google_trends",
            "trend_score": data.get("trend_score", 50.0),
            "mom_growth":  data.get("mom_growth", 0.0),
        })

    # 求人倍率シグナルを追加
    job_sig = trends_cache.get("job_offers_signal")
    if job_sig and job_sig.get("signal") != "neutral":
        for code in job_sig.get("sectors", []):
            pts = TREND_TAILWIND_PTS if job_sig["signal"] == "tailwind" else TREND_HEADWIND_PTS
            signals.append({
                "rule_id":     f"JOB_OFFERS_{code}",
                "label":       f"求人倍率シグナル（{job_sig.get('desc', '')}）",
                "sector_code": code,
                "signal":      job_sig["signal"],
                "pts_adj":     pts,
                "source":      "job_offers_ratio",
                "yoy":         job_sig.get("yoy"),
            })

    return signals


# ==============================================================
# BLOCK 6: trends_cache.json への保存（7日キャッシュ）
# ==============================================================

# BLOCK 6 の内容は BLOCK 1 に統合済み（_load_cache / _save_cache）


# ==============================================================
# BLOCK 7: メインエントリポイント
# ==============================================================

def run_google_trends(cfg, results: dict = None) -> dict:
    """
    Googleトレンドと求人倍率から業種別シグナルを生成する。

    stock_analyzer_main.py から WEEKLY_MODULES として呼び出す:
        _gt = _load_local_module("google_trends_pipeline", ...)
        gt_result = _gt.run_google_trends(cfg, results)
        results["google_trends"] = gt_result
        # LEADING_INDICATOR_RULES に統合
        if gt_result.get("leading_signals"):
            existing = results.get("leading_indicator_signals", [])
            results["leading_indicator_signals"] = (
                existing + gt_result["leading_signals"]
            )

    処理:
      1. 7日以内のキャッシュがあれば再利用
      2. pytrends で業種別トレンド取得（業種数分 × 5秒）
      3. gov_data_pipeline 結果から求人倍率シグナルを生成
      4. LEADING_INDICATOR_RULES 形式のシグナルリストに変換
      5. trends_cache.json に保存

    Args:
        cfg     : config モジュール
        results : run_all_tasks() の results dict
                  results["gov_data"] から求人倍率を取得

    Returns:
        {
          "sectors":          {sector_code: trend_data},
          "leading_signals":  [signal_dict, ...],
          "total_tailwinds":  int,
          "total_headwinds":  int,
          "cached":           bool,
          "timestamp":        str,
        }
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    if not getattr(cfg, "ENABLE_GOOGLE_TRENDS", True):
        return {"skipped": True}

    print("\n  📈 Googleトレンド分析 開始...")
    t0 = datetime.now()

    # ── Step1: キャッシュ確認 ─────────────────────────────────
    cached = _load_cache(cfg)
    if cached:
        n_cached = len(cached.get("sectors", {}))
        print(f"    ✅ キャッシュ使用（{n_cached}業種 / 有効期間内）")
        leading_signals = build_leading_indicator_signals(cached)
        return {
            "sectors":         cached.get("sectors", {}),
            "job_offers_signal": cached.get("job_offers_signal"),
            "leading_signals": leading_signals,
            "total_tailwinds": sum(1 for s in leading_signals if s["signal"] == "tailwind"),
            "total_headwinds": sum(1 for s in leading_signals if s["signal"] == "headwind"),
            "cached":          True,
            "timestamp":       cached.get("cached_at", ""),
        }

    # ── Step2: pytrends セットアップ ─────────────────────────
    TrendReq = _try_import_pytrends()
    if TrendReq is None:
        print("    ⚠️  pytrends 未インストール → スキップ")
        print("    → requirements.txt に pytrends>=4.9.0 を追加してください")
        return {
            "skipped":  True,
            "reason":   "pytrends 未インストール",
            "sectors":  {},
            "leading_signals": [],
            "cached":   False,
        }

    # ── Step3: 業種別トレンド取得 ────────────────────────────
    # config から TREND_KEYWORDS を取得（なければデフォルト使用）
    trend_keywords = getattr(cfg, "TREND_KEYWORDS", DEFAULT_TREND_KEYWORDS)
    # 対象業種（JAPAN_SECTORS と一致するものに絞る）
    japan_sectors  = getattr(cfg, "JAPAN_SECTORS", {})
    target_codes   = set(trend_keywords.keys())
    if japan_sectors:
        target_codes = target_codes & set(japan_sectors.keys())

    sectors_data = {}
    error_count  = 0
    MAX_ERRORS   = 3   # これ以上連続エラーならブロックとみなして停止

    for i, code in enumerate(sorted(target_codes)):
        keywords = trend_keywords.get(code, [])
        if not keywords:
            continue

        print(f"    [{i+1}/{len(target_codes)}] {code}: {keywords[:2]}...")

        raw = _fetch_trend_for_sector(TrendReq, code, keywords)
        if raw is None:
            error_count += 1
            print(f"      ⚠️  取得失敗（連続エラー: {error_count}/{MAX_ERRORS}）")
            if error_count >= MAX_ERRORS:
                print(f"    🚫 連続エラー{MAX_ERRORS}回 → 取得を中断（キャッシュ利用を推奨）")
                break
            time.sleep(FETCH_SLEEP_SEC * 2)
            continue

        error_count = 0   # 成功でリセット
        final_score, signal, pts_adj = _normalize_to_score(
            raw["trend_score"], raw["mom_growth"], cfg
        )
        sectors_data[code] = {
            "keywords":    keywords[:3],
            "trend_score": final_score,
            "mom_growth":  raw["mom_growth"],
            "yoy_growth":  raw["yoy_growth"],
            "signal":      signal,
            "pts_adj":     pts_adj,
        }

        if signal != "neutral":
            icon = "📈" if signal == "tailwind" else "📉"
            print(f"      {icon} {signal} score={final_score:.0f} "
                  f"MoM={raw['mom_growth']:+.0%}")

        # レート制限対策
        time.sleep(FETCH_SLEEP_SEC)

    # ── Step4: 求人倍率シグナル ──────────────────────────────
    results = results or {}
    # ★ 2026-06-10 バグ修正: main の書き込みキーは "gov"
    gov_result = results.get("gov", results.get("gov_data", {}))
    job_offers_signal = _calc_job_offers_signal(gov_result)

    # ── Step5: キャッシュ保存 ────────────────────────────────
    cache_data = {
        "sectors":            sectors_data,
        "job_offers_signal":  job_offers_signal,
    }
    _save_cache(cfg, cache_data)

    # ── Step6: LEADING_INDICATOR_RULES形式への変換 ───────────
    leading_signals = build_leading_indicator_signals(cache_data)

    elapsed = (datetime.now() - t0).seconds
    tailwinds = sum(1 for s in leading_signals if s.get("signal") == "tailwind")
    headwinds = sum(1 for s in leading_signals if s.get("signal") == "headwind")
    print(
        f"    ✅ Googleトレンド完了（{elapsed}秒）: "
        f"{len(sectors_data)}業種取得 "
        f"| 追い風{tailwinds} / 逆風{headwinds}"
    )

    return {
        "sectors":           sectors_data,
        "job_offers_signal": job_offers_signal,
        "leading_signals":   leading_signals,
        "total_tailwinds":   tailwinds,
        "total_headwinds":   headwinds,
        "cached":            False,
        "timestamp":         datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
