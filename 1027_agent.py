# ==============================================================
# sector_agents/1027_agent.py
# 小売業 エージェント v1.0  （東証業種コード: 1027）
# ==============================================================
# 【分析対象企業】イオン / セブン&アイ・ホールディングス / ニトリホールディングス / Jフロントリテイリング / ローソン
# 【マクロ感応度】
#   個人消費拡大 → 💚 追い風（小売全般の売上増）
#   インフレ継続  → 🟡 注意  （値上げで売上増だが節約志向で客数減も）
#   円安進行   → 🔴 逆風  （ニトリ等の輸入商品小売は仕入れコスト増）
#   EC拡大    → 🟡 注意  （実店舗型小売への長期的な構造的逆風）
# ==============================================================

import sys
from pathlib import Path

_here = Path(__file__).parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here.parent))

from sector_agents.base_agent import (
    fetch_financials, build_macro_signals, build_result
)

SECTOR_CODE  = "1027"
SECTOR_NAME  = "小売業"

TARGET_TICKERS = [
    "8267.T",  # イオン
    "3382.T",  # セブン&アイ・ホールディングス
    "9843.T",  # ニトリホールディングス
    "3086.T",  # Jフロントリテイリング
    "2651.T",  # ローソン
]

SECTOR_NOTE = """
### 小売業 ポイント
- **セブン&アイ**: コンビニ世界最大手。海外（7-Eleven Inc.）成長がカギ。
- **イオン**: GMS・SM・ドラッグ・金融の複合体。電子マネーWAONが差別化要因。
- **ニトリの輸入依存**: 製品の大半が中国・アジア製。円安は直接的な逆風。
- **注目指標**: 家計消費支出、既存店売上前年比、消費者信頼感指数、EC化率。
"""


def run_analysis(sector_code: str, cfg) -> dict:
    print(f"  🛒 [{SECTOR_CODE}] {SECTOR_NAME} 分析開始...")

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
