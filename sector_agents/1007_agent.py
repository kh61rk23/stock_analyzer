# ==============================================================
# sector_agents/1007_agent.py
# 化学 エージェント v1.0  (東証業種コード: 1007)
# ==============================================================
# 分析対象企業: 4063.T / 4183.T / 4188.T / 4452.T / 4901.T
# ==============================================================

import sys
from pathlib import Path

_here = Path(__file__).parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here.parent))

from sector_agents.base_agent import (
    fetch_financials, build_macro_signals, build_result
)

SECTOR_CODE = "1007"
SECTOR_NAME = "化学"

TARGET_TICKERS = ['4063.T', '4183.T', '4188.T', '4452.T', '4901.T']

SECTOR_NOTE = """
### 化学 ポイント
- 石化・機能化学・特殊化学と幅広い。ナフサ価格・原油に連動。
- **原油・ナフサ**: 石化系の主要原料コストで業績変動の主因。
- **電池材料・半導体材料**: 成長領域で投資テーマになりやすい。
- **円安**: ドル建て輸出が多い企業は追い風。
"""


def run_analysis(sector_code: str, cfg) -> dict:
    """
    化学 の分析を実行する。
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
