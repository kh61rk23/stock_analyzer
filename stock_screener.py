# ==============================================================
# stock_screener.py  v2.0  (Step C)
# 日本株・米国株 個別銘柄スクリーニング
# ==============================================================
# 【役割】
#   業種エージェント (sector_agents/) が算出したセクタースコアと
#   yfinance の財務指標（PER / PBR / ROE / 時価総額 / 出来高）を組み合わせ、
#   投資候補銘柄を絞り込む。
#
# 【スクリーニング仕様】
#   Step 1: セクタースコア上位 N 業種を選択
#   Step 2: 各業種の構成銘柄を yfinance で財務取得
#   Step 3: フィルター条件（PER / PBR / ROE / 時価総額）でパス/フェイルを判定
#   Step 4: 通過銘柄をスコアでソートして上位 M 件を返す
#
# 【スクリーニング指標】
#   PER   : 0 < PER <= 30（割高・赤字を除外）
#   PBR   : 0 < PBR <= 5（解散価値超を容認、極端な割高を除外）
#   ROE   : ROE >= 8%（資本効率の最低ライン）
#   時価総額: >= 100億円相当（流動性確保、デフォルト設定）
#   出来高 : >= 10万株/日（直近 20 日平均）
#
# 【スコア算出】
#   composite_score = セクタースコア(0-100) * 0.4
#                   + バリュースコア(0-100) * 0.3    ← PER/PBR逆数
#                   + クォリティスコア(0-100) * 0.3  ← ROE/売上成長率
#
# 【出力スキーマ】
#   {
#     "candidates":    [Candidate, ...],  # スクリーニング通過銘柄
#     "screened_count": int,              # スクリーニング実施銘柄数
#     "pass_count":     int,              # 通過銘柄数
#     "top_sectors":    [str, ...],       # 対象セクター名
#     "timestamp":      str,
#   }
#   Candidate = {ticker, name, sector_code, sector_name,
#                per, pbr, roe, market_cap_b, avg_volume,
#                composite_score, value_score, quality_score,
#                sector_score, pass_filters, filter_detail}
#
# 【依存】yfinance / config.py / sector_agents の results["sectors"]
# 【呼出】from stock_screener import run_stock_screener
# ==============================================================

import time
from datetime import datetime
from pathlib import Path
from typing import Optional


# ==============================================================
# BLOCK 0: 銘柄マスタ
# ==============================================================
# 業種コード → 日本株 ticker リスト（東証プライム中心）
# yfinance では東証銘柄に ".T" サフィックスが必要

JP_SECTOR_TICKERS = {
    "1001": ["1332.T", "1301.T", "1377.T"],                          # 水産・農林
    "1002": ["1662.T", "1605.T", "1642.T"],                          # 鉱業
    "1003": ["1802.T", "1803.T", "1812.T", "1808.T", "1820.T"],     # 建設
    "1004": ["2002.T", "2269.T", "2282.T", "2503.T", "2801.T",
             "2802.T", "2914.T"],                                     # 食料品
    "1005": ["3401.T", "3402.T", "3103.T"],                          # 繊維
    "1006": ["3861.T", "3893.T", "3941.T"],                          # パルプ・紙
    "1007": ["4004.T", "4005.T", "4063.T", "4183.T", "4188.T",
             "4208.T"],                                               # 化学
    "1008": ["4151.T", "4502.T", "4503.T", "4506.T", "4507.T",
             "4519.T", "4523.T", "4568.T"],                          # 医薬品
    "1009": ["5001.T", "5002.T", "5019.T"],                          # 石油・石炭
    "1010": ["5108.T", "5101.T"],                                     # ゴム
    "1011": ["5201.T", "5202.T", "5233.T", "5301.T"],               # ガラス・土石
    "1012": ["5401.T", "5406.T", "5411.T", "5713.T"],               # 鉄鋼
    "1013": ["5711.T", "5714.T", "5706.T", "5707.T"],               # 非鉄金属
    "1014": ["5631.T", "5901.T", "3436.T"],                          # 金属製品
    "1015": ["6273.T", "6301.T", "6302.T", "6305.T", "6326.T",
             "6361.T", "6366.T", "7011.T"],                          # 機械
    "1016": ["6501.T", "6502.T", "6504.T", "6594.T", "6645.T",
             "6674.T", "6701.T", "6702.T", "6723.T", "6752.T",
             "6758.T", "6762.T", "6857.T", "8035.T"],               # 電気機器
    "1017": ["7201.T", "7202.T", "7203.T", "7205.T", "7211.T",
             "7261.T", "7267.T", "7269.T", "7270.T"],               # 輸送用機器
    "1018": ["4902.T", "7733.T", "7741.T", "7731.T", "6146.T"],    # 精密機器
    "1019": ["7832.T", "7974.T", "9684.T", "6460.T"],               # その他製品
    "1020": ["9501.T", "9502.T", "9503.T", "9531.T", "9532.T"],     # 電気・ガス
    "1021": ["9001.T", "9005.T", "9007.T", "9008.T", "9009.T"],     # 陸運
    "1022": ["9101.T", "9104.T", "9107.T"],                          # 海運
    "1023": ["9202.T", "9201.T"],                                     # 空運
    "1024": ["9301.T", "9064.T"],                                     # 倉庫・運輸
    "1025": ["4307.T", "4704.T", "9613.T", "9432.T", "9433.T",
             "9434.T", "9984.T", "3659.T"],                          # 情報・通信
    "1026": ["2768.T", "8001.T", "8002.T", "8031.T", "8053.T",
             "8058.T"],                                               # 卸売
    "1027": ["2651.T", "2678.T", "3382.T", "7186.T", "8267.T",
             "9843.T", "9983.T"],                                     # 小売
    "1028": ["8306.T", "8308.T", "8309.T", "8316.T", "8411.T"],     # 銀行
    "1029": ["8601.T", "8604.T", "8616.T"],                          # 証券
    "1030": ["8725.T", "8750.T", "8766.T"],                          # 保険
    "1031": ["8515.T", "8566.T", "8572.T", "8591.T"],               # その他金融
    "1032": ["3003.T", "8801.T", "8802.T", "8804.T", "8830.T"],     # 不動産
    "1033": ["2432.T", "4661.T", "6098.T", "9602.T", "9735.T"],     # サービス
}

# S&P500 構成銘柄から代表銘柄（業種別）
US_SECTOR_TICKERS = {
    "XLE": ["XOM", "CVX", "COP", "EOG", "SLB"],       # エネルギー
    "XLK": ["AAPL", "MSFT", "NVDA", "AVGO", "ORCL"],  # テクノロジー
    "XLF": ["BRK-B", "JPM", "BAC", "WFC", "GS"],      # 金融
    "XLV": ["LLY", "UNH", "JNJ", "ABBV", "MRK"],      # ヘルスケア
    "XLI": ["GE", "CAT", "RTX", "HON", "UPS"],        # 資本財
    "XLY": ["AMZN", "TSLA", "HD", "MCD", "NKE"],      # 一般消費財
    "XLP": ["PG", "KO", "PEP", "COST", "WMT"],        # 生活必需品
    "XLU": ["NEE", "DUK", "SO", "D", "AEP"],          # 公益
    "XLB": ["LIN", "APD", "SHW", "ECL", "NEM"],       # 素材
    "XLRE": ["PLD", "AMT", "EQIX", "PSA", "O"],       # 不動産
    "XLC": ["META", "GOOGL", "NFLX", "DIS", "CMCSA"], # 通信
}


# ==============================================================
# BLOCK 1: yfinance データ取得
# ==============================================================

def _fetch_ticker_data(ticker: str) -> Optional[dict]:
    """
    1銘柄の財務データを yfinance で取得する。

    Returns:
        {ticker, name, per, pbr, roe, market_cap_b, avg_volume}
        取得失敗時は None
    """
    try:
        import yfinance as yf
        import numpy as np

        t    = yf.Ticker(ticker)
        info = t.info

        if not info or info.get("regularMarketPrice") is None:
            return None

        per        = info.get("trailingPE")
        pbr        = info.get("priceToBook")
        roe        = info.get("returnOnEquity")           # 小数表記（0.15=15%）
        market_cap = info.get("marketCap") or 0
        name       = info.get("longName") or info.get("shortName") or ticker

        # 出来高（直近20日平均）
        avg_vol = info.get("averageVolume") or info.get("averageVolume10days") or 0

        # None/NaN を None に統一
        def _clean(v):
            if v is None:
                return None
            try:
                f = float(v)
                return None if (f != f) else f  # NaN チェック
            except (TypeError, ValueError):
                return None

        return {
            "ticker":       ticker,
            "name":         name[:40],
            "per":          _clean(per),
            "pbr":          _clean(pbr),
            "roe":          _clean(roe),       # 小数表記のまま保持
            "market_cap_b": round(market_cap / 1e8, 1),  # 億円/億ドル
            "avg_volume":   int(avg_vol),
        }
    except Exception:
        return None


# ==============================================================
# BLOCK 2: フィルター & スコア算出
# ==============================================================

def _apply_filters(data: dict, cfg) -> tuple:
    """
    フィルター条件を適用する。

    Returns:
        (passed: bool, detail: dict)
        detail = {"per": "OK"|"NG"|"N/A", "pbr": ..., "roe": ...,
                  "market_cap": ..., "volume": ...}
    """
    per_max    = getattr(cfg, "SCREEN_PER_MAX",       30.0)
    pbr_max    = getattr(cfg, "SCREEN_PBR_MAX",       5.0)
    roe_min    = getattr(cfg, "SCREEN_ROE_MIN",       0.08)   # 8%
    mcap_min_b = getattr(cfg, "SCREEN_MCAP_MIN_B",    100.0)  # 100億
    vol_min    = getattr(cfg, "SCREEN_VOL_MIN",       100_000)

    detail = {}
    fails  = []

    per = data.get("per")
    if per is None:
        detail["per"] = "N/A"
    elif per <= 0 or per > per_max:
        detail["per"] = f"NG({per:.1f})"
        fails.append("per")
    else:
        detail["per"] = f"OK({per:.1f})"

    pbr = data.get("pbr")
    if pbr is None:
        detail["pbr"] = "N/A"
    elif pbr <= 0 or pbr > pbr_max:
        detail["pbr"] = f"NG({pbr:.1f})"
        fails.append("pbr")
    else:
        detail["pbr"] = f"OK({pbr:.1f})"

    roe = data.get("roe")
    if roe is None:
        detail["roe"] = "N/A"
    elif roe < roe_min:
        detail["roe"] = f"NG({roe*100:.1f}%)"
        fails.append("roe")
    else:
        detail["roe"] = f"OK({roe*100:.1f}%)"

    mcap = data.get("market_cap_b", 0)
    if mcap < mcap_min_b:
        detail["market_cap"] = f"NG({mcap:.0f}億)"
        fails.append("market_cap")
    else:
        detail["market_cap"] = f"OK({mcap:.0f}億)"

    vol = data.get("avg_volume", 0)
    if vol < vol_min:
        detail["volume"] = f"NG({vol:,})"
        fails.append("volume")
    else:
        detail["volume"] = f"OK({vol:,})"

    return (len(fails) == 0), detail


def _calc_scores(data: dict, sector_score: float) -> dict:
    """
    バリュースコア / クォリティスコア / 総合スコアを算出する。

    バリュースコア (0-100):
        PER の逆数を正規化。PER=10→高スコア、PER=30→低スコア
        PBR の逆数を加味。
    クォリティスコア (0-100):
        ROE を正規化。ROE=30%→高スコア
    composite_score:
        セクター 40% + バリュー 30% + クォリティ 30%
    """
    per = data.get("per")
    pbr = data.get("pbr")
    roe = data.get("roe")

    # バリュースコア
    if per and per > 0:
        per_score = max(0, min(100, (30 - per) / 20 * 100))
    else:
        per_score = 50  # N/A は中間値

    if pbr and pbr > 0:
        pbr_score = max(0, min(100, (5 - pbr) / 4 * 100))
    else:
        pbr_score = 50

    value_score = round((per_score * 0.6 + pbr_score * 0.4), 1)

    # クォリティスコア
    if roe is not None:
        quality_score = round(max(0, min(100, roe / 0.30 * 100)), 1)
    else:
        quality_score = 50

    # 総合スコア
    composite = round(
        sector_score * 0.40
        + value_score  * 0.30
        + quality_score * 0.30,
        1,
    )

    return {
        "sector_score":   round(sector_score, 1),
        "value_score":    value_score,
        "quality_score":  quality_score,
        "composite_score": composite,
    }


# ==============================================================
# BLOCK 3: セクター別スクリーニング
# ==============================================================

def _screen_sector(sector_code: str, sector_name: str,
                   sector_score: float, tickers: list,
                   cfg, market: str = "JP") -> list:
    """
    1 業種の銘柄リストをスクリーニングする。

    Returns:
        通過した Candidate 辞書のリスト
    """
    candidates = []
    print(f"    [{sector_code}] {sector_name}: {len(tickers)}銘柄チェック中...")

    for ticker in tickers:
        data = _fetch_ticker_data(ticker)
        if data is None:
            continue

        passed, detail = _apply_filters(data, cfg)
        scores         = _calc_scores(data, sector_score)

        candidate = {
            "ticker":          ticker,
            "name":            data["name"],
            "market":          market,
            "sector_code":     sector_code,
            "sector_name":     sector_name,
            "per":             data.get("per"),
            "pbr":             data.get("pbr"),
            "roe":             round(data["roe"] * 100, 1) if data.get("roe") is not None else None,
            "market_cap_b":    data.get("market_cap_b"),
            "avg_volume":      data.get("avg_volume"),
            "pass_filters":    passed,
            "filter_detail":   detail,
            **scores,
        }
        candidates.append(candidate)
        time.sleep(0.2)   # yfinance レート制限対策

    passed = [c for c in candidates if c["pass_filters"]]
    print(f"      → 通過: {len(passed)}/{len(candidates)}銘柄")
    return candidates


# ==============================================================
# BLOCK 4: レポート保存
# ==============================================================

def _save_screener_report(result: dict, cfg) -> None:
    """insights/screener/screener_YYYY-MM-DD.md に保存する。"""
    today      = datetime.now().strftime("%Y-%m-%d")
    screen_dir = Path(getattr(cfg, "INSIGHT_DIR", "insights")) / "screener"

    try:
        screen_dir.mkdir(parents=True, exist_ok=True)
        now   = datetime.now().strftime("%Y-%m-%d %H:%M")
        lines = [
            f"# スクリーニングレポート（{today}）",
            f"**生成日時**: {now}  |  "
            f"**スクリーニング対象**: {result['screened_count']}銘柄  |  "
            f"**通過**: {result['pass_count']}銘柄",
            "",
            "## 📊 候補銘柄（composite_score 降順）",
            "",
            "| Ticker | 銘柄名 | 業種 | PER | PBR | ROE(%) | スコア |",
            "|--------|--------|------|-----|-----|--------|--------|",
        ]

        passed = [c for c in result["candidates"] if c["pass_filters"]]
        passed.sort(key=lambda x: x["composite_score"], reverse=True)

        for c in passed[:30]:
            per = f"{c['per']:.1f}" if c["per"] else "N/A"
            pbr = f"{c['pbr']:.1f}" if c["pbr"] else "N/A"
            roe = f"{c['roe']:.1f}" if c["roe"] is not None else "N/A"
            lines.append(
                f"| {c['ticker']} | {c['name'][:20]} | {c['sector_name'][:8]} "
                f"| {per} | {pbr} | {roe} | {c['composite_score']:.1f} |"
            )

        out = screen_dir / f"screener_{today}.md"
        out.write_text("\n".join(lines), encoding="utf-8")
        print(f"  💾 スクリーニングレポート保存: screener_{today}.md")

    except Exception as e:
        print(f"  ⚠️  スクリーニングレポート保存失敗: {str(e)[:60]}")


# ==============================================================
# BLOCK 5: メインエントリポイント
# ==============================================================

def run_stock_screener(cfg, sector_results: dict) -> dict:
    """
    セクター分析結果を受け取り、個別銘柄スクリーニングを実行する。

    Args:
        cfg            : config モジュール
        sector_results : results["sectors"] の dict
                         {sector_code: {"composite_score": float,
                                        "sector_name_ja": str, ...}}

    Returns:
        {
          "candidates":     全銘柄データ（フィルター通過・不通過を含む）,
          "screened_count": int,
          "pass_count":     int,
          "top_sectors":    [sector_name, ...],
          "timestamp":      str,
        }
    """
    print("\n  📋 個別銘柄スクリーニング（Step C）開始...")
    t0 = datetime.now()

    top_n_sectors = getattr(cfg, "SCREEN_TOP_N_SECTORS", 5)

    # セクタースコア上位 N 業種を選択
    if not sector_results:
        print("  ⚠️  sector_results が空のためスクリーニングをスキップ")
        return {
            "candidates": [], "screened_count": 0, "pass_count": 0,
            "top_sectors": [],
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    sorted_sectors = sorted(
        sector_results.items(),
        key=lambda x: x[1].get("composite_score", 0)
            if isinstance(x[1], dict) else 0,
        reverse=True,
    )[:top_n_sectors]

    top_sector_names = [
        v.get("sector_name_ja", k) if isinstance(v, dict) else str(k)
        for k, v in sorted_sectors
    ]
    print(f"  対象業種（上位{top_n_sectors}）: {' / '.join(top_sector_names)}")

    all_candidates = []

    for code, sec_data in sorted_sectors:
        if not isinstance(sec_data, dict):
            continue
        sector_score = sec_data.get("composite_score", 50.0)
        sector_name  = sec_data.get("sector_name_ja", code)
        tickers      = JP_SECTOR_TICKERS.get(code, [])

        if not tickers:
            print(f"    [{code}] {sector_name}: 銘柄マスタなし → スキップ")
            continue

        candidates = _screen_sector(
            sector_code  = code,
            sector_name  = sector_name,
            sector_score = sector_score,
            tickers      = tickers,
            cfg          = cfg,
            market       = "JP",
        )
        all_candidates.extend(candidates)

    passed = [c for c in all_candidates if c["pass_filters"]]

    elapsed = (datetime.now() - t0).seconds
    print(
        f"  ✅ スクリーニング完了（{elapsed}秒）: "
        f"対象{len(all_candidates)}銘柄 / 通過{len(passed)}銘柄"
    )

    result = {
        "candidates":     all_candidates,
        "screened_count": len(all_candidates),
        "pass_count":     len(passed),
        "top_sectors":    top_sector_names,
        "timestamp":      datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    _save_screener_report(result, cfg)
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

    # 仮の sector_results でテスト
    mock_sectors = {
        "1016": {"composite_score": 78.0, "sector_name_ja": "電気機器"},
        "1017": {"composite_score": 65.0, "sector_name_ja": "輸送用機器"},
    }
    result = run_stock_screener(cfg, mock_sectors)
    print(f"\n通過銘柄数: {result['pass_count']}")
    for c in sorted(result["candidates"],
                    key=lambda x: x["composite_score"], reverse=True)[:5]:
        if c["pass_filters"]:
            print(f"  {c['ticker']} | PER:{c['per']} | ROE:{c['roe']}% "
                  f"| score:{c['composite_score']}")
