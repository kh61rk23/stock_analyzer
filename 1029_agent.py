# ==============================================================
# sector_agents/1029_agent.py
# 証券・商品先物取引業 エージェント v1.0  （東証業種コード: 1029）
# ==============================================================
# 【分析対象企業】大和証券グループ本社 / 野村ホールディングス / 松井証券 / マネックスグループ / JAFCO
# 【マクロ感応度】
#   株式相場好調  → 💚 追い風（売買代金増・投信販売増・IPO活発化）
#   金利上昇    → 💚 追い風（債券ビジネス活性化）
#   株式相場低迷  → 🔴 逆風  （売買代金減・運用残高減・手数料収入減）
# ==============================================================

import sys
from pathlib import Path

_here = Path(__file__).parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here.parent))

from sector_agents.base_agent import (
    fetch_financials, build_macro_signals, build_result
)

SECTOR_CODE  = "1029"
SECTOR_NAME  = "証券・商品先物取引業"

TARGET_TICKERS = [
    "8601.T",  # 大和証券グループ本社
    "8604.T",  # 野村ホールディングス
    "8628.T",  # 松井証券
    "8698.T",  # マネックスグループ
    "8595.T",  # JAFCO
]

SECTOR_NOTE = """
### 証券・商品先物取引業 ポイント
- **売買代金との連動**: 東証の1日平均売買代金が業績の最重要指標。3兆円超が好況の目安。
- **オンライン証券の台頭**: 松井・マネックスはコスト競争力で業績堅調。
- **新NISA効果**: 投資信託残高が急増中。手数料収入の安定化に期待。
- **注目指標**: 東証売買代金（日次）、日経平均水準、新規口座開設数、投信設定・解約額。
"""


def run_analysis(sector_code: str, cfg) -> dict:
    print(f"  📊 [{SECTOR_CODE}] {SECTOR_NAME} 分析開始...")

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
