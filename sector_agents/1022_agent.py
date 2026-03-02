# ==============================================================
# sector_agents/1022_agent.py
# 海運業 エージェント v1.0  （東証業種コード: 1022）
# ==============================================================
# 【分析対象企業】日本郵船 / 商船三井 / 川崎汽船 / 飯野海運
# 【マクロ感応度】
#   円安進行        → 💚 追い風（ドル建て運賃の円換算増）
#   円高進行        → 🔴 逆風  （ドル建て収益の円換算減）
#   米景気後退      → 🔴 逆風  （世界貿易縮小 → 海運需要急減）
#   地政学リスク    → 🔴 逆風  （航路変更・保険コスト上昇）
#   金利上昇        → 🟡 注意  （船舶調達コスト上昇）
# ==============================================================

import sys
from pathlib import Path

_here = Path(__file__).parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here.parent))

from sector_agents.base_agent import (
    fetch_financials, build_macro_signals, build_result
)

SECTOR_CODE  = "1022"
SECTOR_NAME  = "海運業"

TARGET_TICKERS = [
    "9101.T",  # 日本郵船
    "9104.T",  # 商船三井
    "9107.T",  # 川崎汽船
    "9119.T",  # 飯野海運
]

SECTOR_NOTE = """
### 海運業 ポイント
- **コンテナ運賃（SCFI）**: 上海発コンテナ運賃指数が業績の先行指標。週次で確認。
- **バルク運賃（BDI）**: バルチック海運指数がドライバルク市況を示す。鉄鉱石・石炭需要に連動。
- **ONE（Ocean Network Express）**: 3 社（郵船・商船三井・川崎汽船）共同出資のコンテナ船会社。
  ONE の収益が 3 社の持分利益に直結するため、ONE の市況が最重要。
- **紅海・スエズリスク**: 迂回航路で運賃上昇するが燃料費・日数も増加。純利益への影響はネット。
- **注目指標**: SCFI（週次）、BDI（日次）、ONE の四半期決算、原油（燃料費）。
"""


def run_analysis(sector_code: str, cfg) -> dict:
    print(f"  🚢 [{SECTOR_CODE}] {SECTOR_NAME} 分析開始...")

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
