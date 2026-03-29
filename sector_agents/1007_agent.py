# ==============================================================
# sector_agents/1007_agent.py
# 化学 エージェント v1.0  （東証業種コード: 1007）
# ==============================================================
# 【分析対象企業】信越化学工業 / 花王 / 三井化学 / 三菱ケミカルグループ / レゾナック・ホールディングス
# 【マクロ感応度】
#   原油高    → 🟡 注意  （ナフサ等の原料コスト上昇）
#   円安進行  → 💚 追い風（信越化学等の輸出企業には収益増）
#   半導体需要拡大 → 💚 追い風（半導体材料・フォトレジスト需要増）
#   中国景気減速 → 🔴 逆風  （汎用化学品の価格下落・需要減）
# ==============================================================

import sys
from pathlib import Path

_here = Path(__file__).parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here.parent))

from sector_agents.base_agent import (
    fetch_financials, build_macro_signals, build_result
)

SECTOR_CODE  = "1007"
SECTOR_NAME  = "化学"

TARGET_TICKERS = [
    "4063.T",  # 信越化学工業
    "4452.T",  # 花王
    "4183.T",  # 三井化学
    "4188.T",  # 三菱ケミカルグループ
    "4004.T",  # レゾナック・ホールディングス
]

SECTOR_NOTE = """
### 化学 ポイント
- **半導体材料**: 信越化学（シリコンウェーハ世界首位）はAI・半導体ブームの直接受益者。
- **高機能材料**: 電子材料・バッテリー材料など高付加価値製品へのシフトが重要。
- **汎用化学品の厳しさ**: 中国の過剰生産が汎用品価格を押し下げ、マージン圧迫。
- **注目指標**: ナフサ価格、エチレン稼働率、半導体製造装置受注、中国PMI。
"""


def run_analysis(sector_code: str, cfg,
                 macro_snapshot: dict = None) -> dict:
    print(f"  ⚗️ [{SECTOR_CODE}] {SECTOR_NAME} 分析開始...")

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
