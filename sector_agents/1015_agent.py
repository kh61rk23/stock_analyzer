# ==============================================================
# sector_agents/1015_agent.py
# 機械 エージェント v1.0  （東証業種コード: 1015）
# ==============================================================
# 【分析対象企業】コマツ / 住友重機械工業 / クボタ / 荏原製作所 / 三菱重工業
# 【マクロ感応度】
#   中国景気回復 → 💚 追い風（建設機械・農業機械の需要増）
#   設備投資拡大 → 💚 追い風（工作機械・産業機械の受注増）
#   円安進行   → 💚 追い風（輸出機械メーカーの収益増）
#   中国景気減速 → 🔴 逆風  （コマツ・クボタの中国向け需要減）
# ==============================================================

import sys
from pathlib import Path

_here = Path(__file__).parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here.parent))

from sector_agents.base_agent import (
    fetch_financials, build_macro_signals, build_result
)

SECTOR_CODE  = "1015"
SECTOR_NAME  = "機械"

TARGET_TICKERS = [
    "6301.T",  # コマツ
    "6302.T",  # 住友重機械工業
    "6326.T",  # クボタ
    "6361.T",  # 荏原製作所
    "7011.T",  # 三菱重工業
]

SECTOR_NOTE = """
### 機械 ポイント
- **コマツの中国露出**: 建設機械大手コマツは中国売上比率が高く、中国PMIに敏感。
- **三菱重工の多角化**: 防衛・エネルギー・物流システム。防衛費増額が追い風。
- **半導体製造装置**: 一部メーカーは半導体向け精密機械で高成長分野に参入。
- **注目指標**: 機械受注統計、工作機械受注額、中国建設機械販売台数、設備投資計画。
"""


def run_analysis(sector_code: str, cfg,
                 macro_snapshot: dict = None) -> dict:
    print(f"  ⚙️ [{SECTOR_CODE}] {SECTOR_NAME} 分析開始...")

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
