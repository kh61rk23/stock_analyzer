# ==============================================================
# sector_agents/1014_agent.py
# 金属製品 エージェント v1.0  （東証業種コード: 1014）
# ==============================================================
# 【分析対象企業】東洋製罐グループホールディングス / NHKスプリング / リンナイ / LIXIL / ノーリツ
# 【マクロ感応度】
#   建設需要増  → 💚 追い風（住宅設備・建材需要増）
#   自動車生産増 → 💚 追い風（スプリング・部品需要増）
#   鋼材価格高騰 → 🔴 逆風  （原材料コスト上昇）
# ==============================================================

import sys
from pathlib import Path

_here = Path(__file__).parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here.parent))

from sector_agents.base_agent import (
    fetch_financials, build_macro_signals, build_result
)

SECTOR_CODE  = "1014"
SECTOR_NAME  = "金属製品"

TARGET_TICKERS = [
    "5901.T",  # 東洋製罐グループホールディングス
    "5991.T",  # NHKスプリング
    "5947.T",  # リンナイ
    "5938.T",  # LIXIL
    "5943.T",  # ノーリツ
]

SECTOR_NOTE = """
### 金属製品 ポイント
- **多様な川下市場**: 缶詰容器・住宅設備・自動車部品・産業機械用部品など幅広い。
- **住宅設備**: LIXIL・リンナイは住宅着工数と連動。金利・不動産市況に影響される。
- **缶・包装**: 食品・飲料の需要は景気に左右されにくいディフェンシブ的性格もある。
- **注目指標**: 住宅着工統計、自動車生産台数、熱延鋼板価格、産業機械受注額。
"""


def run_analysis(sector_code: str, cfg,
                 macro_snapshot: dict = None) -> dict:
    print(f"  🔧 [{SECTOR_CODE}] {SECTOR_NAME} 分析開始...")

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
