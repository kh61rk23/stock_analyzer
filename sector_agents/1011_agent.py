# ==============================================================
# sector_agents/1011_agent.py
# ガラス・土石製品 エージェント v1.0  （東証業種コード: 1011）
# ==============================================================
# 【分析対象企業】AGC / TOTO / 東海カーボン / 太平洋セメント / 日本板硝子
# 【マクロ感応度】
#   建設需要増  → 💚 追い風（窓ガラス・セメント需要増）
#   エネルギー高 → 🔴 逆風  （窯業は大量のエネルギーを使用）
#   中国景気減速 → 🔴 逆風  （セメント・ガラスの需要・価格下落）
# ==============================================================

import sys
from pathlib import Path

_here = Path(__file__).parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here.parent))

from sector_agents.base_agent import (
    fetch_financials, build_macro_signals, build_result
)

SECTOR_CODE  = "1011"
SECTOR_NAME  = "ガラス・土石製品"

TARGET_TICKERS = [
    "5201.T",  # AGC
    "5332.T",  # TOTO
    "5301.T",  # 東海カーボン
    "5233.T",  # 太平洋セメント
    "5202.T",  # 日本板硝子
]

SECTOR_NOTE = """
### ガラス・土石製品 ポイント
- **建設連動**: セメント・ガラスは建設着工統計に業績が強く連動する。
- **車載ガラス**: AGCは自動車用ガラス・電子材料も展開。EV・ADAS需要が追い風。
- **中国依存リスク**: 中国の不動産不況がセメント・ガラス需要を直撃している。
- **注目指標**: セメント出荷量、建設着工統計、中国不動産投資額、エネルギー価格。
"""


def run_analysis(sector_code: str, cfg,
                 macro_snapshot: dict = None) -> dict:
    print(f"  🪟 [{SECTOR_CODE}] {SECTOR_NAME} 分析開始...")

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
