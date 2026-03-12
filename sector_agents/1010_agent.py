# ==============================================================
# sector_agents/1010_agent.py
# ゴム製品 エージェント v1.0  （東証業種コード: 1010）
# ==============================================================
# 【分析対象企業】ブリヂストン / 住友ゴム工業 / 東洋ゴム工業 / 東海化成工業 / 横浜ゴム
# 【マクロ感応度】
#   円安進行  → 💚 追い風（タイヤの海外輸出収益増）
#   原油高    → 🟡 注意  （天然ゴム・合成ゴム原料コスト上昇）
#   自動車生産増 → 💚 追い風（新車用タイヤ需要増）
#   EV化進展  → 🟡 注意  （EVは重量増でタイヤ摩耗大・高付加価値品需要）
# ==============================================================

import sys
from pathlib import Path

_here = Path(__file__).parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here.parent))

from sector_agents.base_agent import (
    fetch_financials, build_macro_signals, build_result
)

SECTOR_CODE  = "1010"
SECTOR_NAME  = "ゴム製品"

TARGET_TICKERS = [
    "5108.T",  # ブリヂストン
    "5110.T",  # 住友ゴム工業
    "5105.T",  # 東洋ゴム工業
    "5191.T",  # 東海化成工業
    "5101.T",  # 横浜ゴム
]

SECTOR_NOTE = """
### ゴム製品 ポイント
- **タイヤ依存**: ブリヂストン・住友ゴム等の収益はほぼタイヤ事業で決まる。
- **天然ゴム価格**: タイ・インドネシアの生産量・為替が原料コストに直結。
- **EV対応タイヤ**: 低転がり抵抗・高耐荷重タイヤへの需要シフトが進行中。
- **注目指標**: 天然ゴム先物価格、新車販売台数、補修用タイヤ出荷量。
"""


def run_analysis(sector_code: str, cfg,
                 macro_snapshot: dict = None) -> dict:
    print(f"  🏎️ [{SECTOR_CODE}] {SECTOR_NAME} 分析開始...")

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
