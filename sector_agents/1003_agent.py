# ==============================================================
# sector_agents/1003_agent.py
# 建設業 エージェント v1.0  （東証業種コード: 1003）
# ==============================================================
# 【分析対象企業】大成建設 / 大林組 / 清水建設 / 鹿島建設 / 大和ハウス工業
# 【マクロ感応度】
#   金利低下  → 💚 追い風（建設資金調達コスト低下・着工増）
#   金利上昇  → 🔴 逆風  （住宅ローン需要減退・着工減少）
#   政府インフラ投資増 → 💚 追い風（公共工事受注増）
#   建設資材高騰 → 🟡 注意  （鉄鋼・木材価格上昇でマージン圧迫）
# ==============================================================

import sys
from pathlib import Path

_here = Path(__file__).parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here.parent))

from sector_agents.base_agent import (
    fetch_financials, build_macro_signals, build_result
)

SECTOR_CODE  = "1003"
SECTOR_NAME  = "建設業"

TARGET_TICKERS = [
    "1801.T",  # 大成建設
    "1802.T",  # 大林組
    "1803.T",  # 清水建設
    "1812.T",  # 鹿島建設
    "1925.T",  # 大和ハウス工業
]

SECTOR_NOTE = """
### 建設業 ポイント
- **国土強靱化**: 政府の防災・インフラ整備投資が継続的な受注を支える。
- **再開発需要**: 都市部の老朽化建物の建替え・大規模再開発が旺盛。
- **人手不足**: 建設技能者の高齢化・不足が工期遅延・コスト上昇リスク。
- **注目指標**: 建設工事着工統計、公共工事受注額、建設資材価格指数。
"""


def run_analysis(sector_code: str, cfg) -> dict:
    print(f"  🏗️ [{SECTOR_CODE}] {SECTOR_NAME} 分析開始...")

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
