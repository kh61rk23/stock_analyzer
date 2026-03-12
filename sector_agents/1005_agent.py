# ==============================================================
# sector_agents/1005_agent.py
# 繊維製品 エージェント v1.0  （東証業種コード: 1005）
# ==============================================================
# 【分析対象企業】帝人 / 東レ / 日清紡ホールディングス / ユニチカ / 東洋紡
# 【マクロ感応度】
#   円安進行  → 💚 追い風（輸出繊維製品の競争力向上）
#   中国景気減速 → 🟡 注意  （繊維原料・中間材の需要減少）
#   エネルギー高 → 🟡 注意  （合成繊維は石油化学製品が原料）
# ==============================================================

import sys
from pathlib import Path

_here = Path(__file__).parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here.parent))

from sector_agents.base_agent import (
    fetch_financials, build_macro_signals, build_result
)

SECTOR_CODE  = "1005"
SECTOR_NAME  = "繊維製品"

TARGET_TICKERS = [
    "3401.T",  # 帝人
    "3402.T",  # 東レ
    "3105.T",  # 日清紡ホールディングス
    "3103.T",  # ユニチカ
    "3107.T",  # 東洋紡
]

SECTOR_NOTE = """
### 繊維製品 ポイント
- **素材転換**: 炭素繊維（東レ・帝人）は航空機・EV・スポーツ用途で高成長。
- **川下移行**: アパレルブランド化・機能性素材で付加価値を高める戦略が有効。
- **中国依存**: 原料調達・生産拠点の中国依存が地政学リスクとなっている。
- **注目指標**: 合成繊維輸出量、炭素繊維受注、綿花・ポリエステル原料価格。
"""


def run_analysis(sector_code: str, cfg,
                 macro_snapshot: dict = None) -> dict:
    print(f"  🧵 [{SECTOR_CODE}] {SECTOR_NAME} 分析開始...")

    companies     = fetch_financials(TARGET_TICKERS)
    macro_signals = build_macro_signals(SECTOR_CODE, cfg, macro_snapshot)

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
