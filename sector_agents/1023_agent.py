# ==============================================================
# sector_agents/1023_agent.py
# 空運業 エージェント v1.0  （東証業種コード: 1023）
# ==============================================================
# 【分析対象企業】ANAホールディングス / 日本航空 / スカイマーク / エアアジア・ジャパン / スターフライヤー
# 【マクロ感応度】
#   インバウンド拡大 → 💚 追い風（国際線旅客需要増）
#   原油高    → 🔴 逆風  （航空燃料費がコストの30〜35%）
#   円安進行   → 🔴 逆風  （ドル建て燃料費・リース料の円換算増）
#   個人消費拡大 → 💚 追い風（国内線旅行需要増）
# ==============================================================

import sys
from pathlib import Path

_here = Path(__file__).parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here.parent))

from sector_agents.base_agent import (
    fetch_financials, build_macro_signals, build_result
)

SECTOR_CODE  = "1023"
SECTOR_NAME  = "空運業"

TARGET_TICKERS = [
    "9202.T",  # ANAホールディングス
    "9201.T",  # 日本航空
    "9206.T",  # スカイマーク
    "9233.T",  # エアアジア・ジャパン
    "9207.T",  # スターフライヤー
]

SECTOR_NOTE = """
### 空運業 ポイント
- **燃料費の直撃**: ジェット燃料はコストの3分の1を占め、原油価格変動が業績を直撃。
- **ヘッジ戦略**: ANAもJALも燃料ヘッジを実施するが、相場急変時はリスクが残る。
- **インバウンド需要**: 訪日外国人の急増で国際線が大幅回復。円安が追い風になる局面も。
- **注目指標**: 国際線旅客数、国内線旅客数、ジェット燃料スポット価格、USD/JPY。
"""


def run_analysis(sector_code: str, cfg,
                 macro_snapshot: dict = None) -> dict:
    print(f"  ✈️ [{SECTOR_CODE}] {SECTOR_NAME} 分析開始...")

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
