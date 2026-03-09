# ==============================================================
# sector_agents/1013_agent.py
# 非鉄金属 エージェント v1.0  （東証業種コード: 1013）
# ==============================================================
# 【分析対象企業】住友金属鉱山 / 三井金属鉱業 / 三菱マテリアル / 住友電気工業 / 日本軽金属ホールディングス
# 【マクロ感応度】
#   銅・金価格上昇 → 💚 追い風（資源収益・製錬利益増）
#   EV化進展   → 💚 追い風（銅・リチウム・コバルト需要急増）
#   円安進行   → 💚 追い風（ドル建て資源収益の円換算増）
#   中国景気減速 → 🔴 逆風  （銅等ベースメタルの需要・価格低下）
# ==============================================================

import sys
from pathlib import Path

_here = Path(__file__).parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here.parent))

from sector_agents.base_agent import (
    fetch_financials, build_macro_signals, build_result
)

SECTOR_CODE  = "1013"
SECTOR_NAME  = "非鉄金属"

TARGET_TICKERS = [
    "5713.T",  # 住友金属鉱山
    "5706.T",  # 三井金属鉱業
    "5711.T",  # 三菱マテリアル
    "5802.T",  # 住友電気工業
    "5703.T",  # 日本軽金属ホールディングス
]

SECTOR_NOTE = """
### 非鉄金属 ポイント
- **銅の重要性**: EV1台に銅60〜80kg使用。EV普及と再エネ設備拡大で銅需要は長期増加。
- **住友金属鉱山**: 金・銅の鉱山権益保有。資源価格上昇の直接受益者。
- **レアメタル**: リチウム・コバルト・ニッケルはバッテリー需要で供給不足懸念。
- **注目指標**: LME銅・金・ニッケル先物価格、EV販売台数、中国非鉄金属在庫。
"""


def run_analysis(sector_code: str, cfg) -> dict:
    print(f"  🥇 [{SECTOR_CODE}] {SECTOR_NAME} 分析開始...")

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
