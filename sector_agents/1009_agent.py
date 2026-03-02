# ==============================================================
# sector_agents/1009_agent.py
# 石油・石炭製品 エージェント v1.0  (東証業種コード: 1009)
# ==============================================================
# 分析対象企業: 5019.T / 5020.T / 5021.T
# ==============================================================

import sys
from pathlib import Path

_here = Path(__file__).parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here.parent))

from sector_agents.base_agent import (
    fetch_financials, build_macro_signals, build_result
)

SECTOR_CODE = "1009"
SECTOR_NAME = "石油・石炭製品"

TARGET_TICKERS = ['5019.T', '5020.T', '5021.T']

SECTOR_NOTE = """
### 石油・石炭製品 ポイント
- 原油の精製・販売が主軸。WTI・Dubai原油価格に直結。
- **原油高**: 在庫評価益が発生し短期利益を押し上げる。
- **脱炭素**: 長期的なリスクとして再エネ移行コストが重い。
- **設備老朽化**: 製油所の更新投資負担が継続する。
"""


def run_analysis(sector_code: str, cfg) -> dict:
    """
    石油・石炭製品 の分析を実行する。
    stock_analyzer_main.py の run_sector_analysis() から動的ロードで呼ばれる。
    """
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
