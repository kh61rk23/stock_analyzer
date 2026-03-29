# ==============================================================
# sector_agents/1030_agent.py
# 保険業 エージェント v1.0  （東証業種コード: 1030）
# ==============================================================
# 【分析対象企業】MS&ADインシュアランスグループ / 第一生命ホールディングス / 東京海上ホールディングス / かんぽ生命保険 / SOMPOホールディングス
# 【マクロ感応度】
#   金利上昇   → 💚 追い風（生保は長期金利上昇で運用利回り改善）
#   株式相場好調  → 💚 追い風（保有株式・投信の評価益増）
#   大規模自然災害 → 🔴 逆風  （損保の保険金支払い増・収益悪化）
#   円安進行   → 💚 追い風（海外再保険収益の円換算増）
# ==============================================================

import sys
from pathlib import Path

_here = Path(__file__).parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here.parent))

from sector_agents.base_agent import (
    fetch_financials, build_macro_signals, build_result
)

SECTOR_CODE  = "1030"
SECTOR_NAME  = "保険業"

TARGET_TICKERS = [
    "8725.T",  # MS&ADインシュアランスグループ
    "8750.T",  # 第一生命ホールディングス
    "8766.T",  # 東京海上ホールディングス
    "7181.T",  # かんぽ生命保険
    "8630.T",  # SOMPOホールディングス
]

SECTOR_NOTE = """
### 保険業 ポイント
- **東京海上の強さ**: 海外事業比率が高く、グローバルに分散されたポートフォリオ。
- **金利感応度（生保）**: 長期金利上昇で予定利率の問題が緩和され、逆ザヤ解消につながる。
- **自然災害リスク**: 台風・地震・洪水の損害額が損保収益を大きく左右する。
- **注目指標**: 長期金利（10年国債）、日経平均、自然災害発生状況、為替(USD/JPY)。
"""


def run_analysis(sector_code: str, cfg,
                 macro_snapshot: dict = None) -> dict:
    print(f"  🛡️ [{SECTOR_CODE}] {SECTOR_NAME} 分析開始...")

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
