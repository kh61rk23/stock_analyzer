# ==============================================================
# sector_agents/1009_agent.py
# 石油・石炭製品 エージェント v1.0  （東証業種コード: 1009）
# ==============================================================
# 【分析対象企業】出光興産 / ENEOSホールディングス / コスモエネルギーホールディングス / INPEX / 昭和四日市石油
# 【マクロ感応度】
#   原油高    → 💚 追い風（製品価格上昇・在庫評価益）
#   原油安    → 🔴 逆風  （在庫評価損・精製マージン悪化）
#   円安進行  → 🔴 逆風  （原油はドル建て・輸入コスト増）
# ==============================================================

import sys
from pathlib import Path

_here = Path(__file__).parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here.parent))

from sector_agents.base_agent import (
    fetch_financials, build_macro_signals, build_result
)

SECTOR_CODE  = "1009"
SECTOR_NAME  = "石油・石炭製品"

TARGET_TICKERS = [
    "5019.T",  # 出光興産
    "5020.T",  # ENEOSホールディングス
    "5021.T",  # コスモエネルギーホールディングス
    "1605.T",  # INPEX
    "5016.T",  # 昭和四日市石油
]

SECTOR_NOTE = """
### 石油・石炭製品 ポイント
- **精製マージン**: 原油と石油製品価格の差（クラック・スプレッド）が収益のカギ。
- **在庫評価損益**: 原油価格変動で四半期ごとに大きな在庫評価損益が発生。
- **脱炭素化の長期リスク**: EV普及・再エネ拡大でガソリン需要は長期的に減少。
- **注目指標**: WTI・ドバイ原油先物、精製マージン、ガソリン店頭価格、EV普及率。
"""


def run_analysis(sector_code: str, cfg) -> dict:
    print(f"  🛢️ [{SECTOR_CODE}] {SECTOR_NAME} 分析開始...")

    companies     = fetch_financials(TARGET_TICKERS)
    macro_signals = build_macro_signals(SECTOR_CODE, cfg)

    ok = sum(1 for c in companies if c.get("fetch_ok"))
    tw = sum(1 for s in macro_signals if s["signal"] == "tailwind")
    hw = sum(1 for s in macro_signals if s["signal"] == "headwind")
    print(f"    財務データ: {ok}/{len(TARGET_TICKERS)} 成功 | マクロ: 追い風 {tw} / 逆風 {hw}")

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
