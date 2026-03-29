# ==============================================================
# sector_agents/1004_agent.py
# 食料品 エージェント v1.0  （東証業種コード: 1004）
# ==============================================================
# 【分析対象企業】味の素 / サッポロホールディングス / キリンホールディングス / 日清製粉グループ / 日本たばこ産業
# 【マクロ感応度】
#   円安進行  → 🔴 逆風  （輸入原材料コスト上昇）
#   食料品インフレ → 💚 追い風（価格転嫁が進む局面では増収）
#   個人消費低迷 → 🟡 注意  （節約志向で内食へシフトも業態次第）
# ==============================================================

import sys
from pathlib import Path

_here = Path(__file__).parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here.parent))

from sector_agents.base_agent import (
    fetch_financials, build_macro_signals, build_result
)

SECTOR_CODE  = "1004"
SECTOR_NAME  = "食料品"

TARGET_TICKERS = [
    "2802.T",  # 味の素
    "2501.T",  # サッポロホールディングス
    "2503.T",  # キリンホールディングス
    "2002.T",  # 日清製粉グループ
    "2914.T",  # 日本たばこ産業
]

SECTOR_NOTE = """
### 食料品 ポイント
- **価格転嫁力**: 原材料高をどれだけ値上げで吸収できるかがカギ。ブランド力が重要。
- **円安の直撃**: 小麦・大豆・砂糖・食用油のほぼ全てを輸入に依存。
- **海外展開**: 味の素・日清は海外比率が高く、円安でも収益が増える二面性あり。
- **注目指標**: 小麦・大豆・とうもろこし先物価格、国内CPI食料品、家計消費支出。
"""


def run_analysis(sector_code: str, cfg,
                 macro_snapshot: dict = None) -> dict:
    print(f"  🍙 [{SECTOR_CODE}] {SECTOR_NAME} 分析開始...")

    companies     = fetch_financials(TARGET_TICKERS)
    macro_signals = build_macro_signals(SECTOR_CODE, cfg, macro_snapshot)

    ok = sum(1 for c in companies if c.get("fetch_ok"))
    tw = sum(1 for s in macro_signals if s["signal"] == "tailwind")
    hw = sum(1 for s in macro_signals if s["signal"] == "headwind")
    print(f"    財務データ: {ok}/{len(TARGET_TICKERS)} 成功 | マクロ: 追い風 {tw} / 逆風 {hw}")

    result = build_result(SECTOR_CODE, SECTOR_NAME, companies, macro_signals, cfg,
                          macro_snapshot=macro_snapshot)
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
