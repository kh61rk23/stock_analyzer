# ==============================================================
# sector_agents/1021_agent.py
# 陸運業 エージェント v1.0  （東証業種コード: 1021）
# ==============================================================
# 【分析対象企業】東日本旅客鉄道 / 西日本旅客鉄道 / 東海旅客鉄道 / ヤマトホールディングス / 日本通運
# 【マクロ感応度】
#   個人消費拡大 → 💚 追い風（鉄道旅客・物流需要増）
#   インバウンド拡大 → 💚 追い風（外国人観光客による鉄道利用増）
#   EC拡大    → 💚 追い風（宅配便・物流需要の長期的増加）
#   燃料高騰   → 🟡 注意  （トラック・バスの燃料コスト上昇）
# ==============================================================

import sys
from pathlib import Path

_here = Path(__file__).parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here.parent))

from sector_agents.base_agent import (
    fetch_financials, build_macro_signals, build_result
)

SECTOR_CODE  = "1021"
SECTOR_NAME  = "陸運業"

TARGET_TICKERS = [
    "9020.T",  # 東日本旅客鉄道
    "9021.T",  # 西日本旅客鉄道
    "9022.T",  # 東海旅客鉄道
    "9064.T",  # ヤマトホールディングス
    "9062.T",  # 日本通運
]

SECTOR_NOTE = """
### 陸運業 ポイント
- **鉄道の回復**: コロナ後のインバウンド・国内旅行の本格回復がJR各社に追い風。
- **2024年問題**: トラックドライバーの時間外労働規制で物流コスト増・供給不足が発生。
- **宅配の値上げ**: ヤマト・佐川は単価引上げと荷物量の最適化で収益改善中。
- **注目指標**: 新幹線・在来線輸送量、宅配便取扱数量、インバウンド訪日客数、軽油価格。
"""


def run_analysis(sector_code: str, cfg) -> dict:
    print(f"  🚚 [{SECTOR_CODE}] {SECTOR_NAME} 分析開始...")

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
