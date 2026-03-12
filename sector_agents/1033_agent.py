# ==============================================================
# sector_agents/1033_agent.py
# サービス業 エージェント v1.0  （東証業種コード: 1033）
# ==============================================================
# 【分析対象企業】ソフトバンクグループ / LINEヤフー / エムスリー / リクルートホールディングス / 東宝
# 【マクロ感応度】
#   個人消費拡大 → 💚 追い風（外食・レジャー・エンタメ需要増）
#   インバウンド拡大 → 💚 追い風（ホテル・観光・エンタメ需要増）
#   デジタル化進展 → 💚 追い風（ITサービス・プラットフォーム需要増）
#   金利上昇   → 🟡 注意  （SBGの有利子負債コスト増）
# ==============================================================

import sys
from pathlib import Path

_here = Path(__file__).parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here.parent))

from sector_agents.base_agent import (
    fetch_financials, build_macro_signals, build_result
)

SECTOR_CODE  = "1033"
SECTOR_NAME  = "サービス業"

TARGET_TICKERS = [
    "9984.T",  # ソフトバンクグループ
    "4689.T",  # LINEヤフー
    "2413.T",  # エムスリー
    "6098.T",  # リクルートホールディングス
    "9602.T",  # 東宝
]

SECTOR_NOTE = """
### サービス業 ポイント
- **SBGの特殊性**: ソフトバンクGはテクノロジー投資会社。NAV（純資産価値）がカギ。
- **リクルート**: Indeedを持つグローバル求人プラットフォーム。米国雇用市場に連動。
- **エムスリー**: 医師向けデジタルプラットフォーム。医療DX推進で高成長継続。
- **注目指標**: 国内消費者信頼感、インバウンド訪日客数、IT投資計画、有効求人倍率。
"""


def run_analysis(sector_code: str, cfg,
                 macro_snapshot: dict = None) -> dict:
    print(f"  🏠 [{SECTOR_CODE}] {SECTOR_NAME} 分析開始...")

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
