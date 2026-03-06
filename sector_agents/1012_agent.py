# ==============================================================
# sector_agents/1012_agent.py
# 鉄鋼 エージェント v1.0  (東証業種コード: 1012)
# ==============================================================
# 分析対象企業: 5401.T / 5411.T / 5406.T
# ==============================================================

import sys
from pathlib import Path

_here = Path(__file__).parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here.parent))

from sector_agents.base_agent import (
    fetch_financials, build_macro_signals, build_result
)

SECTOR_CODE = "1012"
SECTOR_NAME = "鉄鋼"

TARGET_TICKERS = ['5401.T', '5411.T', '5406.T']

SECTOR_NOTE = """
### 鉄鋼 ポイント
- 高炉・電炉による鉄鋼製品の製造・販売。
- **鉄鉱石・石炭**: 主原料コストで業績変動の主因。
- **中国過剰生産**: ダンピング競合で市況を下押しするリスク。
- **高付加価値鋼材**: 販売構成が利益率を決める重要指標。
"""


def run_analysis(sector_code: str, cfg) -> dict:
    """
    鉄鋼 の分析を実行する。
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
