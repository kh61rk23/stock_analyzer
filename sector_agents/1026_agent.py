# ==============================================================
# sector_agents/1026_agent.py
# 卸売業 エージェント v1.0  （東証業種コード: 1026）
# ==============================================================
# 【分析対象企業】伊藤忠商事 / 丸紅 / 三井物産 / 住友商事 / 三菱商事
# 【マクロ感応度】
#   資源価格上昇 → 💚 追い風（資源系商社の権益収益増）
#   円安進行   → 💚 追い風（ドル建て収益の円換算増）
#   中国景気回復 → 💚 追い風（中国向け資源・食料需要増）
#   資源価格下落 → 🔴 逆風  （資源権益の評価損・収益減）
# ==============================================================

import sys
from pathlib import Path

_here = Path(__file__).parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here.parent))

from sector_agents.base_agent import (
    fetch_financials, build_macro_signals, build_result
)

SECTOR_CODE  = "1026"
SECTOR_NAME  = "卸売業"

TARGET_TICKERS = [
    "8001.T",  # 伊藤忠商事
    "8002.T",  # 丸紅
    "8031.T",  # 三井物産
    "8053.T",  # 住友商事
    "8058.T",  # 三菱商事
]

SECTOR_NOTE = """
### 卸売業（総合商社）ポイント
- **資源ポートフォリオ**: 三菱商事・三井物産は資源比率が高く、原油・LNG・鉄鉱石に敏感。
- **非資源の成長**: 伊藤忠はファミリーマート・ユニー等の生活消費分野で安定収益。
- **バフェット効果**: バークシャーが大量保有し、日本株の割安・高配当として注目。
- **注目指標**: 原油・鉄鉱石・LNG価格、USD/JPY、中国PMI、各社の資源権益比率。
"""


def run_analysis(sector_code: str, cfg,
                 macro_snapshot: dict = None) -> dict:
    print(f"  🏢 [{SECTOR_CODE}] {SECTOR_NAME} 分析開始...")

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
