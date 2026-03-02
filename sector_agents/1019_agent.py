# ==============================================================
# sector_agents/1019_agent.py
# その他製品 エージェント v1.0  (東証業種コード: 1019)
# ==============================================================
# 分析対象企業: 7951.T / 4902.T / 7974.T / 9684.T
# ==============================================================

import sys
from pathlib import Path

_here = Path(__file__).parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here.parent))

from sector_agents.base_agent import (
    fetch_financials, build_macro_signals, build_result
)

SECTOR_CODE = "1019"
SECTOR_NAME = "その他製品"

TARGET_TICKERS = ['7951.T', '4902.T', '7974.T', '9684.T']

SECTOR_NOTE = """
### その他製品 ポイント
- 楽器・玩具・ゲーム・写真フィルム等の多様な製品群。
- **コンテンツ力**: ゲーム・IPの競争力が株価を左右。
- **為替**: 海外売上比率が高い企業は円安が追い風。
- **消費動向**: 可処分所得・余暇時間に連動する消費財。
"""


def run_analysis(sector_code: str, cfg) -> dict:
    """
    その他製品 の分析を実行する。
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
