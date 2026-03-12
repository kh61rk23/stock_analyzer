# ==============================================================
# sector_agents/1032_agent.py
# 不動産業 エージェント v1.0  （東証業種コード: 1032）
# ==============================================================
# 【分析対象企業】三井不動産 / 三菱地所 / 住友不動産 / 東急不動産 / 野村不動産
# 【マクロ感応度】
#   金利上昇  → 🔴 逆風  （住宅ローン金利上昇 → 需要減・資産価値圧迫）
#   金利低下  → 💚 追い風（住宅ローン低下 → 需要回復）
#   高インフレ → 🟡 注意  （建材コスト上昇 vs 地価・賃料上昇の綱引き）
# ==============================================================

import sys
from pathlib import Path

_here = Path(__file__).parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here.parent))

from sector_agents.base_agent import (
    fetch_financials, build_macro_signals, build_result
)

SECTOR_CODE  = "1032"
SECTOR_NAME  = "不動産業"

TARGET_TICKERS = [
    "8801.T",  # 三井不動産
    "8802.T",  # 三菱地所
    "8830.T",  # 住友不動産
    "3289.T",  # 東急不動産ホールディングス
    "3231.T",  # 野村不動産ホールディングス
]

SECTOR_NOTE = """
### 不動産業 ポイント
- **金利感応度が高い**: 長期金利 1% 上昇でキャップレートが上昇し、理論株価は 10〜20% 圧迫される。
- **オフィス稼働率**: テレワーク普及の影響を都心 A 級オフィスの空室率で確認。
- **物流施設**: EC 拡大で需要継続。特にドライ物流・最終配送拠点の供給動向に注意。
- **訪日観光**: インバウンド回復がホテル・商業施設 REIT に追い風。
- **注目指標**: NOI 利回り（Net Operating Income Yield）、空室率、REIT 分配金利回り。
"""


def run_analysis(sector_code: str, cfg,
                 macro_snapshot: dict = None) -> dict:
    print(f"  🏙️ [{SECTOR_CODE}] {SECTOR_NAME} 分析開始...")

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
