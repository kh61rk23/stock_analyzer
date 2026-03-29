# ==============================================================
# sector_agents/1020_agent.py
# 電気・ガス業 エージェント v1.0  （東証業種コード: 1020）
# ==============================================================
# 【分析対象企業】東京電力ホールディングス / 中部電力 / 関西電力 / 東京ガス / 大阪ガス
# 【マクロ感応度】
#   エネルギー価格高騰 → 🟡 注意（燃料費上昇→料金値上げ申請→時間差でコスト転嫁）
#   金利上昇   → 🔴 逆風  （大量の有利子負債を抱えるため利息負担増）
#   原発再稼働  → 💚 追い風（燃料費削減・収益改善）
# ==============================================================

import sys
from pathlib import Path

_here = Path(__file__).parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here.parent))

from sector_agents.base_agent import (
    fetch_financials, build_macro_signals, build_result
)

SECTOR_CODE  = "1020"
SECTOR_NAME  = "電気・ガス業"

TARGET_TICKERS = [
    "9501.T",  # 東京電力ホールディングス
    "9502.T",  # 中部電力
    "9503.T",  # 関西電力
    "9531.T",  # 東京ガス
    "9532.T",  # 大阪ガス
]

SECTOR_NOTE = """
### 電気・ガス業 ポイント
- **規制産業の特性**: 料金改定は政府認可が必要。コスト増をすぐに価格転嫁できない。
- **原発再稼働**: 関西電力は複数基再稼働済みで収益改善。東京電力は柏崎刈羽が焦点。
- **再エネ投資**: 再生可能エネルギーへの大規模投資が続く。
- **注目指標**: LNG価格、原油価格、電力需要量、原発稼働率、再エネ電力コスト。
"""

def run_analysis(sector_code: str, cfg,
                 macro_snapshot: dict = None) -> dict:
    print(f"  💡 [{SECTOR_CODE}] {SECTOR_NAME} 分析開始...")

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
