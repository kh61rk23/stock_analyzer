# ==============================================================
# sector_agents/1001_agent.py
# 水産・農林業 エージェント v1.0  （東証業種コード: 1001）
# ==============================================================
# 【分析対象企業】日本水産 / 日本ハム / 極洋 / サカタのタネ / ヤクルト本社
# 【マクロ感応度】
#   円安進行  → 🟡 注意  （輸入飼料・原材料コスト上昇）
#   原油高    → 🟡 注意  （漁船燃料・輸送コスト上昇）
#   食料品インフレ → 💚 追い風（価格転嫁が進む局面）
# ==============================================================

import sys
from pathlib import Path

_here = Path(__file__).parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here.parent))

from sector_agents.base_agent import (
    fetch_financials, build_macro_signals, build_result
)

SECTOR_CODE  = "1001"
SECTOR_NAME  = "水産・農林業"

TARGET_TICKERS = [
    "1332.T",  # 日本水産
    "2282.T",  # 日本ハム
    "1301.T",  # 極洋
    "1377.T",  # サカタのタネ
    "2267.T",  # ヤクルト本社
]

SECTOR_NOTE = """
### 水産・農林業 ポイント
- **食料安全保障**: 政府の国内農林水産業強化政策が追い風。補助金・価格支持制度に注目。
- **円安の二面性**: 輸出水産物には有利だが、輸入飼料・肥料コストが上昇する。
- **気候変動リスク**: 漁獲量の変動・農作物の収量変化が業績に直結。
- **注目指標**: 農業産出額、漁業生産量、飼料価格指数、食品物価指数。
"""


def run_analysis(sector_code: str, cfg,
                 macro_snapshot: dict = None) -> dict:
    print(f"  🐟 [{SECTOR_CODE}] {SECTOR_NAME} 分析開始...")

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
