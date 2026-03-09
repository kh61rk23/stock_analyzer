# ==============================================================
# sector_agents/1006_agent.py
# パルプ・紙 エージェント v1.0  （東証業種コード: 1006）
# ==============================================================
# 【分析対象企業】王子ホールディングス / 日本製紙 / 大王製紙 / 中越パルプ工業 / 北越コーポレーション
# 【マクロ感応度】
#   円安進行  → 🔴 逆風  （木材チップ・古紙輸入コスト上昇）
#   エネルギー高 → 🔴 逆風  （製紙は大量の電力・熱エネルギーを使用）
#   紙需要構造変化 → 🟡 注意  （印刷用紙減・衛生用紙・包装紙増）
# ==============================================================

import sys
from pathlib import Path

_here = Path(__file__).parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here.parent))

from sector_agents.base_agent import (
    fetch_financials, build_macro_signals, build_result
)

SECTOR_CODE  = "1006"
SECTOR_NAME  = "パルプ・紙"

TARGET_TICKERS = [
    "3861.T",  # 王子ホールディングス
    "3863.T",  # 日本製紙
    "3880.T",  # 大王製紙
    "3877.T",  # 中越パルプ工業
    "3893.T",  # 北越コーポレーション
]

SECTOR_NOTE = """
### パルプ・紙 ポイント
- **デジタル化の逆風**: 新聞・印刷用紙の需要は長期的に縮小トレンド。
- **成長分野**: 段ボール・衛生用紙・医療用紙はECの拡大で需要増が続く。
- **エネルギー集約産業**: 電力・蒸気コストが生産コストの20〜30%を占める。
- **注目指標**: 紙・板紙生産量統計、段ボール出荷量、木材チップ輸入価格。
"""


def run_analysis(sector_code: str, cfg) -> dict:
    print(f"  📄 [{SECTOR_CODE}] {SECTOR_NAME} 分析開始...")

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
