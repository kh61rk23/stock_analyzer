# ==============================================================
# multi_currency_monitor.py  v1.0  (A-6)
# 多通貨モニタリング: EUR/JPY / CNY/JPY / AUD/JPY + FRED補完
# ==============================================================
# 【役割】
#   USD/JPY に加えて EUR・CNY・AUD の対円レートを毎日取得し、
#   既存 MACRO_RULES を拡張した多通貨シグナルを業種スコアに反映する。
#
# 【なぜ多通貨が必要か】
#   USD/JPY だけでは捉えられない業種別の輸出リスクが存在する:
#     EUR/JPY: 欧州向け輸出企業（自動車・機械・精密機器）の採算
#     CNY/JPY: 中国向け輸出・現地生産企業（電気機器・化学・食料品）
#     AUD/JPY: 資源輸入国としての原料コスト（製紙・食品・電力）
#              + リスクオン/オフの先行指標
#
# 【出力スキーマ（run_multi_currency の戻り値）】
#   {
#     "rates": {
#       "usdjpy": 149.2, "eurjpy": 161.5, "cnyjpy": 20.8, "audjpy": 97.3,
#       "usdcny": 7.24, "usdeur": 0.92, "jp10y": 1.52, ...
#       "us_cpi_yoy":    3.2,    # 米CPI前年比% (FRED)
#       "us_unrate":     4.1,    # 米失業率% (FRED)
#       "us_hy_spread":  3.45,   # HYスプレッド (FRED)
#       "us_indpro_mom": 0.3,    # 鉱工業生産 前月比% (FRED)
#       "jp_cpi_yoy":    2.8,    # 日本CPI (FRED JPNCPIALLMINMEI)
#     },
#     "signals": [
#       {
#         "factor":   "eur_depreciation",
#         "label":    "EUR安（対円）",
#         "value":    "EUR/JPY: 155.2",
#         "tailwind": ["1017", "1015"],
#         "headwind": [],
#       },
#       ...
#     ],
#     "macro_adjustment": {sector_code: delta_score},
#     "summary_text":  str,
#     "timestamp":     str,
#   }
#
# 【composite_score への影響】
#   各通貨シグナルで tailwind: +5pt / headwind: -5pt を加算
#   （マクロシグナルの+10ptより小さい「補助シグナル」として設計）
#
# 【依存】yfinance / config.py（POLICY_FRED_API_KEY）
# 【呼出】from multi_currency_monitor import run_multi_currency
# ==============================================================

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ── 為替ティッカー（yfinance）──────────────────────────────────
CURRENCY_TICKERS = {
    "eurjpy":   "EURJPY=X",
    "cnyjpy":   "CNYJPY=X",
    "audjpy":   "AUDJPY=X",
    "usdjpy":   "USDJPY=X",      # 既存 MACRO_TICKERS と重複するが再取得で最新値を保証
    "usdcny":   "USDCNY=X",      # USD/CNY（CNY/JPY の検証用）
    "gbpjpy":   "GBPJPY=X",      # GBP/JPY（英国系製薬・金融）
    "jp10y":    "^JGB10Y",        # 日本10年国債（FRED補完前の速報値）
}

# ── FRED 追加取得系列 ──────────────────────────────────────────
# （既存 policy_tracker.py の FRED_SERIES に追加するデータ）
FRED_EXTRA_SERIES = {
    # 米国マクロ
    "CPIAUCSL":      {"label": "米CPI（前年比換算用）",  "type": "level"},
    "UNRATE":        {"label": "米失業率(%)",            "type": "level"},
    "INDPRO":        {"label": "米鉱工業生産指数",        "type": "level"},
    "BAMLH0A0HYM2":  {"label": "米HYスプレッド(%)",      "type": "level"},
    "MORTGAGE30US":  {"label": "米30年住宅ローン金利(%)", "type": "level"},
    "T10Y2Y":        {"label": "米2/10年スプレッド(%)",   "type": "level"},
    # 日本マクロ（FREDで取得可能）
    "JPNCPIALLMINMEI": {"label": "日本CPI（前年比換算用）", "type": "level"},
    "JPNUNEMPLOYMENT": {"label": "日本失業率(%)",           "type": "level"},
    "DEXJPUS":          {"label": "円ドル実績値（日次）",    "type": "level"},
    # 通貨
    "DEXCNUS":          {"label": "人民元ドル（日次）",       "type": "level"},
    "DEXUSEU":          {"label": "ドルユーロ（日次）",       "type": "level"},
}

# ── 多通貨シグナル定義 ───────────────────────────────────────
# EUR/JPY, CNY/JPY, AUD/JPY それぞれの閾値と影響業種
# 各シグナルの補正値: tailwind/headwind = +5pt / -5pt
CURRENCY_RULES = {
    # EUR/JPY 高（円安ユーロ高）→ 欧州向け輸出採算改善
    "eur_strong": {
        "label":     "EUR高（欧州向け採算改善）",
        "condition": lambda r: r.get("eurjpy", 0) > 162,
        "value_fmt": lambda r: f"EUR/JPY: {r['eurjpy']:.1f}",
        "tailwind":  ["1017", "1015", "1018", "1016"],   # 自動車・機械・精密・電気
        "headwind":  ["1004", "1027"],                    # 食料品・小売（欧州輸入品コスト↑）
        "rationale": "EU向け輸出採算改善。ドイツ・フランス向け輸出が多い自動車・機械に恩恵。",
    },
    # EUR/JPY 低（円高ユーロ安）
    "eur_weak": {
        "label":     "EUR安（欧州向け採算悪化）",
        "condition": lambda r: r.get("eurjpy", 999) < 155,
        "value_fmt": lambda r: f"EUR/JPY: {r['eurjpy']:.1f}",
        "tailwind":  ["1004", "1027"],
        "headwind":  ["1017", "1015", "1018"],
        "rationale": "EU向け輸出採算悪化。欧州向け輸出比率が高い自動車・機械に逆風。",
    },
    # CNY/JPY 高（人民元高・円安）→ 中国向け輸出・現地生産採算改善
    "cny_strong": {
        "label":     "CNY高（中国向け採算改善）",
        "condition": lambda r: r.get("cnyjpy", 0) > 21.5,
        "value_fmt": lambda r: f"CNY/JPY: {r['cnyjpy']:.2f}",
        "tailwind":  ["1016", "1007", "1017", "1015"],   # 電気・化学・自動車・機械
        "headwind":  [],
        "rationale": "人民元高で中国現地生産利益の円換算が増加。中国依存度が高い電気機器・化学に恩恵。",
    },
    # CNY/JPY 低（人民元安）→ 中国向け採算悪化 + デフレ輸出リスク
    "cny_weak": {
        "label":     "CNY安（中国デフレ輸出リスク）",
        "condition": lambda r: r.get("cnyjpy", 999) < 19.5,
        "value_fmt": lambda r: f"CNY/JPY: {r['cnyjpy']:.2f}",
        "tailwind":  ["1004", "1027"],                   # 食料品・小売（安価な輸入品恩恵）
        "headwind":  ["1016", "1017", "1012"],            # 電気・自動車・鉄鋼（中国競合激化）
        "rationale": "人民元安で中国企業の価格競争力が向上。日本製造業の中国市場でのシェア低下リスク。",
    },
    # AUD/JPY 高（豪ドル高・リスクオン）→ 資源関連コスト上昇
    "aud_strong": {
        "label":     "AUD高（資源コスト上昇・リスクオン）",
        "condition": lambda r: r.get("audjpy", 0) > 98,
        "value_fmt": lambda r: f"AUD/JPY: {r['audjpy']:.1f}",
        "tailwind":  ["1009", "1013", "1012"],            # 石油・非鉄・鉄鋼（資源企業）
        "headwind":  ["1006", "1004", "1020"],            # 製紙・食料品・電力（資源コスト↑）
        "rationale": "豪ドル高はリスクオンシグナル兼資源価格上昇の代理指標。資源輸入コスト上昇に注意。",
    },
    # AUD/JPY 低（リスクオフ先行指標）
    "aud_weak": {
        "label":     "AUD安（リスクオフ先行シグナル）",
        "condition": lambda r: r.get("audjpy", 999) < 90,
        "value_fmt": lambda r: f"AUD/JPY: {r['audjpy']:.1f}",
        "tailwind":  ["1008", "1020", "1004"],            # 医薬・電力・食料（ディフェンシブ）
        "headwind":  ["1022", "1017", "1016"],            # 海運・自動車・電気（景気敏感）
        "rationale": "豪ドルはリスク資産の先行指標。AUD安時はリスクオフ局面に入っている可能性。",
    },
    # 米HYスプレッド拡大（信用リスク上昇）
    "hy_spread_wide": {
        "label":     "米HYスプレッド拡大（信用リスク上昇）",
        "condition": lambda r: r.get("us_hy_spread", 0) > 5.0,
        "value_fmt": lambda r: f"HYスプレッド: {r['us_hy_spread']:.2f}%",
        "tailwind":  ["1008", "1020"],                   # ディフェンシブ
        "headwind":  ["1028", "1029", "1031", "1032"],   # 銀行・証券・金融・不動産
        "rationale": "HYスプレッド拡大は信用収縮のシグナル。金融・不動産セクターへの逆風。",
    },
    # 米失業率上昇（景気後退シグナル）
    "us_unrate_rising": {
        "label":     "米失業率上昇（景気後退シグナル）",
        "condition": lambda r: r.get("us_unrate", 0) > 4.5,
        "value_fmt": lambda r: f"米失業率: {r['us_unrate']:.1f}%",
        "tailwind":  ["1008", "1020", "1004"],
        "headwind":  ["1022", "1017", "1025"],
        "rationale": "米失業率上昇は景気後退の遅行指標。米国向け輸出需要の減少リスク。",
    },
}

CACHE_FILENAME = "multi_currency_cache.json"
CACHE_HOURS    = 8    # 為替は8時間キャッシュ（1日3回更新）
SIGNAL_DELTA   = 5.0  # tailwind/headwind 1件あたりの補正点


# ==============================================================
# BLOCK 1: レート取得
# ==============================================================

def _fetch_yfinance_rates() -> dict:
    """yfinance から全通貨ペアの最新レートを取得する。"""
    try:
        import yfinance as yf
    except ImportError:
        return {}

    rates = {}
    for key, ticker in CURRENCY_TICKERS.items():
        try:
            info  = yf.Ticker(ticker).info or {}
            price = info.get("regularMarketPrice") or info.get("bid")
            if price:
                rates[key] = round(float(price), 4)
            time.sleep(0.2)
        except Exception:
            pass
    return rates


def _fetch_fred_extras(api_key: str) -> dict:
    """FRED から追加マクロ指標を取得する。前年比/前月比も計算する。"""
    if not api_key:
        return {}

    try:
        import requests
    except ImportError:
        return {}

    data = {}
    base_url = "https://api.stlouisfed.org/fred/series/observations"

    for series_id, meta in FRED_EXTRA_SERIES.items():
        try:
            params = {
                "series_id":         series_id,
                "api_key":           api_key,
                "file_type":         "json",
                "sort_order":        "desc",
                "limit":             "14",    # 直近14件（前年比計算に13ヶ月分必要）
                "observation_start": (datetime.now() - timedelta(days=400)).strftime("%Y-%m-%d"),
            }
            r = requests.get(base_url, params=params, timeout=8)
            if r.status_code != 200:
                continue
            obs = [o for o in r.json().get("observations", []) if o.get("value") != "."]
            if not obs:
                continue

            latest_val = float(obs[0]["value"])
            key_name   = series_id.lower().replace("0", "0")  # そのまま使う

            # 前年比（12ヶ月前と比較）
            yoy = None
            if len(obs) >= 13:
                prev_val = float(obs[12]["value"])
                if abs(prev_val) > 0.001:
                    yoy = round((latest_val - prev_val) / abs(prev_val) * 100, 2)

            # 前月比
            mom = None
            if len(obs) >= 2:
                prev_val = float(obs[1]["value"])
                if abs(prev_val) > 0.001:
                    mom = round((latest_val - prev_val) / abs(prev_val) * 100, 2)

            data[series_id.lower()] = latest_val
            if yoy is not None:
                data[f"{series_id.lower()}_yoy"] = yoy
            if mom is not None:
                data[f"{series_id.lower()}_mom"] = mom

            time.sleep(0.1)
        except Exception:
            continue

    # 使いやすいエイリアスを追加
    alias_map = {
        "cpiaucsl_yoy":       "us_cpi_yoy",
        "unrate":             "us_unrate",
        "indpro_mom":         "us_indpro_mom",
        "bamlh0a0hym2":       "us_hy_spread",
        "mortgage30us":       "us_mortgage30",
        "t10y2y":             "us_2y10y_spread",
        "jpncpiallminmei_yoy": "jp_cpi_yoy",
        "jpnunemployment":    "jp_unrate",
        "dexjpus":            "usdjpy_fred",
    }
    for src, dst in alias_map.items():
        if src in data:
            data[dst] = data[src]

    return data


# ==============================================================
# BLOCK 2: シグナル評価
# ==============================================================

def evaluate_currency_signals(rates: dict) -> list:
    """
    rates 辞書から多通貨シグナルを評価して返す。

    Returns:
        [{"factor", "label", "value", "tailwind", "headwind", "rationale"}, ...]
    """
    signals = []
    for factor, rule in CURRENCY_RULES.items():
        try:
            if rule["condition"](rates):
                signals.append({
                    "factor":    factor,
                    "label":     rule["label"],
                    "value":     rule["value_fmt"](rates),
                    "tailwind":  rule["tailwind"],
                    "headwind":  rule["headwind"],
                    "rationale": rule["rationale"],
                })
        except Exception:
            continue
    return signals


def calc_currency_adjustments(signals: list) -> dict:
    """
    通貨シグナルから業種コードごとの composite_score 補正量を計算する。

    Returns:
        {sector_code: delta_float}  例: {"1017": 10.0, "1004": -5.0}
    """
    adjustments: dict[str, float] = {}
    for sig in signals:
        for code in sig.get("tailwind", []):
            adjustments[code] = adjustments.get(code, 0) + SIGNAL_DELTA
        for code in sig.get("headwind", []):
            adjustments[code] = adjustments.get(code, 0) - SIGNAL_DELTA

    # ±15点にクリップ
    return {k: max(-15.0, min(15.0, v)) for k, v in adjustments.items()}


# ==============================================================
# BLOCK 3: キャッシュ管理
# ==============================================================

def _load_cache(cfg) -> Optional[dict]:
    try:
        p = Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / CACHE_FILENAME
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
        p = Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / CACHE_FILENAME
        p.parent.mkdir(parents=True, exist_ok=True)
        data["cached_at"] = datetime.now().isoformat()
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# ==============================================================
# BLOCK 4: テキスト整形
# ==============================================================

def format_currency_report(result: dict) -> str:
    """
    週次レポート・アラート向けのテキストを生成する。

    出力例:
      🌍 多通貨モニター
      EUR/JPY: 161.5  CNY/JPY: 20.8  AUD/JPY: 97.3  GBP/JPY: 190.2
      米CPI前年比: 3.2%  米失業率: 4.1%  HYスプレッド: 3.45%
      シグナル（2件）:
        📈 EUR高（欧州向け採算改善）: 自動車・機械に+5pt
        📉 CNY安（中国デフレリスク）: 電気機器・自動車に-5pt
    """
    rates   = result.get("rates", {})
    signals = result.get("signals", [])

    lines = ["🌍 多通貨モニター"]

    # 為替レート一覧
    rate_parts = []
    for label, key in [("EUR/JPY", "eurjpy"), ("CNY/JPY", "cnyjpy"),
                        ("AUD/JPY", "audjpy"), ("GBP/JPY", "gbpjpy")]:
        v = rates.get(key)
        if v:
            rate_parts.append(f"{label}: {v:.2f}")
    if rate_parts:
        lines.append("  " + "  ".join(rate_parts))

    # FRED指標
    fred_parts = []
    for label, key in [("米CPI(YoY)", "us_cpi_yoy"), ("米失業率", "us_unrate"),
                        ("HYスプレッド", "us_hy_spread"), ("日本CPI(YoY)", "jp_cpi_yoy")]:
        v = rates.get(key)
        if v is not None:
            fred_parts.append(f"{label}: {v:.2f}%")
    if fred_parts:
        lines.append("  " + " | ".join(fred_parts))

    # シグナル
    if signals:
        lines.append(f"  通貨シグナル（{len(signals)}件）:")
        for s in signals[:5]:
            tw = "/".join(s["tailwind"][:3]) if s["tailwind"] else "-"
            hw = "/".join(s["headwind"][:3]) if s["headwind"] else "-"
            icon = "📈" if s["tailwind"] else "📉"
            lines.append(f"    {icon} {s['label']} [{s['value']}]")
            if s["tailwind"]:
                lines.append(f"       追い風: {tw} (+{SIGNAL_DELTA:.0f}pt)")
            if s["headwind"]:
                lines.append(f"       逆風:   {hw} (-{SIGNAL_DELTA:.0f}pt)")
    else:
        lines.append("  通貨シグナル: なし（中立圏）")

    return "\n".join(lines)


# ==============================================================
# BLOCK 5: メインエントリポイント
# ==============================================================

def run_multi_currency(cfg) -> dict:
    """
    多通貨モニタリングを実行する。

    stock_analyzer_main.py から以下のように呼び出す:
        _mc = _load_local_module("multi_currency_monitor", ...)
        mc_result = _mc.run_multi_currency(cfg)
        results["multi_currency"] = mc_result

    Returns:
        {
          "rates":              dict,   # 全レートとFRED指標
          "signals":            list,   # アクティブなシグナルリスト
          "macro_adjustment":   dict,   # {sector_code: delta_score}
          "summary_text":       str,    # レポート用テキスト
          "timestamp":          str,
        }
    """
    print("\n  🌍 多通貨モニタリング開始...")

    # キャッシュ確認
    cached = _load_cache(cfg)
    if cached:
        print(f"    ✅ キャッシュ使用（{len(cached.get('signals', []))}シグナル）")
        return cached

    # レート取得
    print("    為替レート取得中...")
    rates = _fetch_yfinance_rates()

    # FRED 追加指標
    fred_key = getattr(cfg, "POLICY_FRED_API_KEY", "")
    if fred_key:
        print("    FRED 追加指標取得中...")
        fred_data = _fetch_fred_extras(fred_key)
        rates.update(fred_data)
        n_fred = len([k for k in fred_data if not k.endswith(("_yoy", "_mom"))])
        print(f"    ✅ FRED: {n_fred}系列取得")
    else:
        print("    ⏩ FRED: APIキー未設定（yfinanceのみ）")

    # レート表示
    for key, val in sorted(rates.items()):
        if any(key.startswith(p) for p in ["eur", "cny", "aud", "usd", "gbp"]):
            print(f"      {key}: {val}")

    # シグナル評価
    signals     = evaluate_currency_signals(rates)
    adjustments = calc_currency_adjustments(signals)

    if signals:
        print(f"    📊 シグナル検出: {len(signals)}件")
        for s in signals:
            print(f"      {s['label']} [{s['value']}]")

    summary_text = format_currency_report({
        "rates": rates, "signals": signals,
    })

    result = {
        "rates":            rates,
        "signals":          signals,
        "macro_adjustment": adjustments,
        "summary_text":     summary_text,
        "timestamp":        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    _save_cache(cfg, result)
    print(f"    ✅ 多通貨モニタリング完了: {len(signals)}シグナル / 補正{len(adjustments)}業種")
    return result
