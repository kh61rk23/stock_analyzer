# ==============================================================
# sector_agents/1012_agent.py
# 鉄鋼 エージェント v1.0  （東証業種コード: 1012）
# ==============================================================
# 【分析対象企業】日本製鉄 / JFEホールディングス / 神戸製鋼所 / 大同特殊鋼 / 東京製鐵
# 【マクロ感応度】
#   中国景気回復 → 💚 追い風（鉄鋼需要増・価格上昇）
#   中国景気減速 → 🔴 逆風  （過剰生産・ダンピング輸出が価格を押し下げ）
#   自動車生産増 → 💚 追い風（自動車向け鋼板需要増）
#   原料炭高騰  → 🔴 逆風  （コークス・鉄鉱石価格上昇でコスト増）
# ==============================================================

import sys
from pathlib import Path

_here = Path(__file__).parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here.parent))

from sector_agents.base_agent import (
    fetch_financials, build_macro_signals, build_result
)

SECTOR_CODE  = "1012"
SECTOR_NAME  = "鉄鋼"

TARGET_TICKERS = [
    "5401.T",  # 日本製鉄
    "5411.T",  # JFEホールディングス
    "5406.T",  # 神戸製鋼所
    "5471.T",  # 大同特殊鋼
    "5440.T",  # 東京製鐵
]

SECTOR_NOTE = """
### 鉄鋼 ポイント
- **中国の影響力**: 世界の鉄鋼生産の半分以上を中国が占め、過剰生産が価格を支配。
- **自動車向け高級鋼**: ハイテン材・電磁鋼板（EV用）は付加価値が高く利益貢献大。
- **脱炭素コスト**: 水素還元製鉄への転換は巨額の設備投資が必要。
- **注目指標**: 粗鋼生産量、鉄鉱石・原料炭先物価格、中国PMI製造業、自動車生産台数。
"""

def run_analysis(sector_code: str, cfg,
                 macro_snapshot: dict = None) -> dict:
    print(f"  🔩 [{SECTOR_CODE}] {SECTOR_NAME} 分析開始...")

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
