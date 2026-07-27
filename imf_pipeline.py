# ==============================================================
# imf_pipeline.py  v1.0
# IMF DataMapper API によるグローバルマクロ指標取得
# ==============================================================
# 【役割】
#   IMF の無料 DataMapper API（APIキー不要）から下記指標を取得し、
#   gov_data_pipeline の国内統計を補完するグローバル視点を追加する。
#
# 【取得指標】
#   NGDP_RPCH  : 実質GDP成長率（%）          ← 景気サイクル判断
#   PCPIPCH    : 消費者物価上昇率（%）        ← グローバルインフレ
#   LUR        : 失業率（%）                 ← 雇用環境
#   BCA_NGDPD  : 経常収支 / GDP比（%）       ← 外需・資本フロー
#   GGXWDG_NGDP: 政府債務 / GDP比（%）      ← 財政リスク
#
# 【対象国・地域】
#   日本（JPN）/ 米国（USA）/ 中国（CHN）/ ユーロ圏（Euro Area: EA19）
#   / 世界（World: W）
#
# 【APIキー】不要。User-Agent ヘッダーのみ必要。
#
# 【出力スキーマ】
#   {
#     "indicators": {
#       "NGDP_RPCH": {
#         "JPN": {"2023": 1.9, "2024": 0.5, "2025e": 1.1},
#         "USA": {...}, ...
#       },
#       ...
#     },
#     "signals":    [{"label", "signal", "reason", "score"}, ...],
#     "summary":    str,   # 1行要約
#     "timestamp":  str,
#   }
#
# 【依存】requests / config.py
# 【呼出】from imf_pipeline import run_imf_pipeline
# ==============================================================

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

IMF_API_BASE   = "https://www.imf.org/external/datamapper/api/v1"
IMF_HEADERS    = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; StockAnalyzer/1.0; "
        "+https://github.com/user/stock-analyzer)"
    ),
    "Accept": "application/json",
}

# ==============================================================
# BLOCK 0: 取得設定
# ==============================================================

INDICATORS = {
    "NGDP_RPCH":    "実質GDP成長率(%)",
    "PCPIPCH":      "消費者物価上昇率(%)",
    "LUR":          "失業率(%)",
    "BCA_NGDPD":    "経常収支/GDP(%)",
    "GGXWDG_NGDP":  "政府債務/GDP(%)",
}

COUNTRIES = {
    "JPN":  "日本",
    "USA":  "米国",
    "CHN":  "中国",
    "DEU":  "ドイツ",
    "EA19": "ユーロ圏",
    "W":    "世界",
}

# 直近 N 年分を取得（予測値含む）
from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'B6_IMF_PIPELINE_FETCH_YEARS': 'FETCH_YEARS',
}

FETCH_YEARS = 3


# ==============================================================
# BLOCK 1: API 取得
# ==============================================================

def _fetch_indicator(indicator: str, timeout: int = 15) -> Optional[dict]:
    """
    1指標の全国データを IMF DataMapper API から取得する。

    Returns:
        {country_code: {year_str: float}, ...}
        失敗時は None
    """
    try:
        import requests
    except ImportError:
        return None

    # 取得年範囲
    now         = datetime.now()
    start_year  = now.year - FETCH_YEARS + 1
    end_year    = now.year + 1   # 翌年予測まで含む
    year_params = ",".join(str(y) for y in range(start_year, end_year + 1))

    url = f"{IMF_API_BASE}/{indicator}?periods={year_params}"

    try:
        resp = requests.get(url, headers=IMF_HEADERS, timeout=timeout)
        if resp.status_code != 200:
            print(f"    ⚠️  IMF API HTTP {resp.status_code}: {indicator}")
            return None
        raw    = resp.json()
        values = raw.get("values", {}).get(indicator, {})
        return values   # {country_code: {year_str: float}}
    except Exception as e:
        print(f"    ⚠️  IMF API 例外 ({indicator}): {str(e)[:60]}")
        return None


def fetch_all_indicators(cfg) -> dict:
    """
    全指標を取得して {indicator: {country: {year: val}}} を返す。
    """
    result = {}
    enabled_indicators = getattr(cfg, "IMF_INDICATORS", list(INDICATORS.keys()))
    enabled_countries  = getattr(cfg, "IMF_COUNTRIES",  list(COUNTRIES.keys()))

    for ind in enabled_indicators:
        data = _fetch_indicator(ind)
        if data is None:
            print(f"    [{ind}] 取得失敗")
            result[ind] = {}
            time.sleep(0.5)
            continue

        # 対象国のみ抽出
        result[ind] = {
            country: data[country]
            for country in enabled_countries
            if country in data
        }
        found = list(result[ind].keys())
        print(f"    [{ind}] {INDICATORS[ind]}: {len(found)}カ国取得")
        time.sleep(0.4)   # レート制限対策

    return result


# ==============================================================
# BLOCK 2: シグナル判定
# ==============================================================

def _latest_value(country_data: dict) -> Optional[float]:
    """
    {year_str: float} から最新の確定値（最も新しい年）を返す。
    値が None / NaN でないものを選ぶ。
    """
    for year in sorted(country_data.keys(), reverse=True):
        v = country_data.get(year)
        try:
            f = float(v)
            if f == f:   # NaN チェック
                return f
        except (TypeError, ValueError):
            continue
    return None


def judge_imf_signals(indicators: dict) -> list:
    """
    取得した IMF データから投資シグナルを判定する。

    Returns:
        [{"label": str, "signal": "tailwind"|"headwind"|"neutral",
          "reason": str, "score": int}, ...]
    """
    signals = []
    now_year = str(datetime.now().year)

    # ① 米国 GDP 成長率
    us_gdp = indicators.get("NGDP_RPCH", {}).get("USA", {})
    us_gdp_val = _latest_value(us_gdp)
    if us_gdp_val is not None:
        if us_gdp_val >= 2.0:
            signals.append({
                "label":  "米国GDP成長率",
                "signal": "tailwind",
                "reason": f"米国 実質GDP+{us_gdp_val:.1f}% → 外需型製造業・IT追い風",
                "score":  1,
            })
        elif us_gdp_val < 0.5:
            signals.append({
                "label":  "米国GDP成長率",
                "signal": "headwind",
                "reason": f"米国 実質GDP+{us_gdp_val:.1f}% → 景気鈍化リスク",
                "score": -1,
            })
        else:
            signals.append({
                "label":  "米国GDP成長率",
                "signal": "neutral",
                "reason": f"米国 実質GDP+{us_gdp_val:.1f}%（横ばい）",
                "score":  0,
            })

    # ② 世界 CPI（グローバルインフレ）
    world_cpi = indicators.get("PCPIPCH", {}).get("W", {})
    world_cpi_val = _latest_value(world_cpi)
    if world_cpi_val is not None:
        if world_cpi_val >= 4.0:
            signals.append({
                "label":  "グローバルCPI",
                "signal": "headwind",
                "reason": f"世界CPI+{world_cpi_val:.1f}% → 金融引締め長期化リスク",
                "score": -1,
            })
        elif world_cpi_val <= 2.0:
            signals.append({
                "label":  "グローバルCPI",
                "signal": "tailwind",
                "reason": f"世界CPI+{world_cpi_val:.1f}% → インフレ収束・緩和転換期待",
                "score":  1,
            })
        else:
            signals.append({
                "label":  "グローバルCPI",
                "signal": "neutral",
                "reason": f"世界CPI+{world_cpi_val:.1f}%（許容範囲内）",
                "score":  0,
            })

    # ③ 日本 経常収支
    jpn_bca = indicators.get("BCA_NGDPD", {}).get("JPN", {})
    jpn_bca_val = _latest_value(jpn_bca)
    if jpn_bca_val is not None:
        if jpn_bca_val >= 2.0:
            signals.append({
                "label":  "日本経常収支",
                "signal": "tailwind",
                "reason": f"日本 経常収支/GDP+{jpn_bca_val:.1f}% → 円高圧力減少・輸出株追い風",
                "score":  1,
            })
        elif jpn_bca_val < 0:
            signals.append({
                "label":  "日本経常収支",
                "signal": "headwind",
                "reason": f"日本 経常収支/GDP{jpn_bca_val:.1f}% → 赤字転落・円安加速リスク",
                "score": -1,
            })

    # ④ 中国 GDP 成長率（資源・素材業種への影響）
    chn_gdp = indicators.get("NGDP_RPCH", {}).get("CHN", {})
    chn_gdp_val = _latest_value(chn_gdp)
    if chn_gdp_val is not None:
        if chn_gdp_val >= 4.5:
            signals.append({
                "label":  "中国GDP成長率",
                "signal": "tailwind",
                "reason": f"中国 実質GDP+{chn_gdp_val:.1f}% → 資源・素材・海運追い風",
                "score":  1,
            })
        elif chn_gdp_val < 3.0:
            signals.append({
                "label":  "中国GDP成長率",
                "signal": "headwind",
                "reason": f"中国 実質GDP+{chn_gdp_val:.1f}% → 需要減・コモディティ逆風",
                "score": -1,
            })

    return signals


# ==============================================================
# BLOCK 3: レポート保存
# ==============================================================

def _save_imf_report(result: dict, cfg) -> None:
    """insights/imf/imf_YYYY-MM-DD.md に保存する。"""
    today   = datetime.now().strftime("%Y-%m-%d")
    imf_dir = Path(getattr(cfg, "INSIGHT_DIR", "insights")) / "imf"

    try:
        imf_dir.mkdir(parents=True, exist_ok=True)
        now   = datetime.now().strftime("%Y-%m-%d %H:%M")
        lines = [
            f"# IMFグローバルマクロレポート（{today}）",
            f"**生成日時**: {now}",
            "",
            "## 📡 シグナルサマリー",
            "",
        ]

        tw = sum(1 for s in result["signals"] if s["signal"] == "tailwind")
        hw = sum(1 for s in result["signals"] if s["signal"] == "headwind")
        lines.append(f"追い風 **{tw}件** / 逆風 **{hw}件**")
        lines.append("")

        for s in result["signals"]:
            icon = {"tailwind": "📈", "headwind": "📉", "neutral": "➡️"}.get(s["signal"])
            lines.append(f"- {icon} {s['reason']}")
        lines.append("")

        # 指標テーブル
        now_year  = datetime.now().year
        for ind, ind_label in INDICATORS.items():
            data = result["indicators"].get(ind, {})
            if not data:
                continue
            lines += [f"## {ind_label} ({ind})", ""]
            years = [str(y) for y in range(now_year - 2, now_year + 2)]
            header = "| 国 | " + " | ".join(years) + " |"
            sep    = "|---|" + "---|" * len(years)
            lines += [header, sep]
            for code, label in COUNTRIES.items():
                cdata = data.get(code, {})
                vals  = [f"{cdata.get(y,''):.1f}" if cdata.get(y) is not None
                         else "—" for y in years]
                lines.append(f"| {label}({code}) | " + " | ".join(vals) + " |")
            lines.append("")

        out = imf_dir / f"imf_{today}.md"
        out.write_text("\n".join(lines), encoding="utf-8")
        print(f"  💾 IMFレポート保存: imf_{today}.md")

    except Exception as e:
        print(f"  ⚠️  IMFレポート保存失敗: {str(e)[:60]}")


def _save_imf_cache(result: dict, cfg) -> None:
    """data/imf_cache.json に保存（翌日以降の差分比較用）。"""
    data_dir = Path(getattr(cfg, "DATA_DIR", "data"))
    cache    = data_dir / "imf_cache.json"
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    except Exception:
        pass


# ==============================================================
# BLOCK 4: メインエントリポイント
# ==============================================================

def run_imf_pipeline(cfg) -> dict:
    """
    IMF DataMapper API からグローバルマクロ指標を取得し、
    シグナルを判定して返す。

    Returns:
        {
          "indicators": {indicator: {country: {year: float}}},
          "signals":    [{label, signal, reason, score}],
          "summary":    str,
          "timestamp":  str,
        }
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    print("\n  🌍 IMFグローバルマクロパイプライン 開始...")
    t0 = datetime.now()

    indicators = fetch_all_indicators(cfg)
    signals    = judge_imf_signals(indicators)

    tw = sum(1 for s in signals if s["signal"] == "tailwind")
    hw = sum(1 for s in signals if s["signal"] == "headwind")

    if tw > hw:
        summary = f"グローバル景気: 追い風優勢（追い風{tw}件 / 逆風{hw}件）"
    elif hw > tw:
        summary = f"グローバル景気: 逆風優勢（追い風{tw}件 / 逆風{hw}件）"
    else:
        summary = f"グローバル景気: 均衡（追い風{tw}件 / 逆風{hw}件）"

    elapsed = (datetime.now() - t0).seconds
    print(f"  ✅ IMF完了（{elapsed}秒）: {summary}")

    result = {
        "indicators": indicators,
        "signals":    signals,
        "summary":    summary,
        "timestamp":  datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    _save_imf_report(result, cfg)
    _save_imf_cache(result, cfg)
    return result


# ==============================================================
# 単体テスト
# ==============================================================
if __name__ == "__main__":
    import importlib.util
    for p in ["/content/config.py",
              str(Path(__file__).parent / "config.py")]:
        if Path(p).exists():
            spec = importlib.util.spec_from_file_location("config", p)
            cfg  = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cfg)
            break
    result = run_imf_pipeline(cfg)
    print(f"\n{result['summary']}")
    for s in result["signals"]:
        icon = {"tailwind": "📈", "headwind": "📉", "neutral": "➡️"}.get(s["signal"])
        print(f"  {icon} {s['reason']}")
