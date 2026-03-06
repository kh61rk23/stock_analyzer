# ==============================================================
# sector_agents/1015_agent.py
# 機械 エージェント v1.0  (東証業種コード: 1015)
# ==============================================================
# 分析対象企業: 6301.T / 6326.T / 6367.T / 6113.T / 7011.T
# ==============================================================

import sys
from pathlib import Path

_here = Path(__file__).parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here.parent))

from sector_agents.base_agent import (
    fetch_financials, build_macro_signals, build_result
)

SECTOR_CODE = "1015"
SECTOR_NAME = "機械"

TARGET_TICKERS = ['6301.T', '6326.T', '6367.T', '6113.T', '7011.T']

SECTOR_NOTE = """
### 機械 ポイント
- 工作機械・産業機械・建設機械など幅広い。世界景気感応度が高い。
- **製造業設備投資**: 国内外の設備投資サイクルに強く連動。
- **円安**: 輸出比率が高く、円安は業績押し上げ要因。
- **半導体・EV関連設備**: 需要急増領域で受注増期待。
"""


def run_analysis(sector_code: str, cfg) -> dict:
    """
    機械 の分析を実行する。
    stock_analyzer_main.py の run_sector_analysis() から動的ロードで呼ばれる。
    """
    result = {}
    print(f"  [{SECTOR_CODE}] {SECTOR_NAME} 分析開始...")

    companies = fetch_financials(TARGET_TICKERS)
    ok = sum(1 for c in companies if c.get("fetch_ok"))
    print(f"    財務データ取得: {ok}/{len(TARGET_TICKERS)} 銘柄成功")

    macro_signals = build_macro_signals(SECTOR_CODE, cfg)
    tw = sum(1 for s in macro_signals if s["signal"] == "tailwind")
    hw = sum(1 for s in macro_signals if s["signal"] == "headwind")
    print(f"    マクロシグナル: 追い風 {tw}件 / 逆風 {hw}件")

    result = build_result(SECTOR_CODE, SECTOR_NAME, companies, macro_signals, cfg)
    result["insight_text"] += "\n" + SECTOR_NOTE
    result["sector_note"]   = SECTOR_NOTE

    print(f"    ✅ {SECTOR_NAME}: スコア {result['composite_score']}点 / リスク {result['risk_level']}")
    return result


if __name__ == "__main__":
    import importlib.util
    for p in ["/content/config.py", str(Path(__file__).parents[1] / "config.py")]:
        if Path(p).exists():
            spec = importlib.util.spec_from_file_location("config", p)
            cfg  = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cfg)
            break
    result = run_analysis(SECTOR_CODE, cfg)
    print(result["insight_text"])
