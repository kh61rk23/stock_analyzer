# ==============================================================
# sector_agents/1011_agent.py
# ガラス・土石製品 エージェント v1.0  (東証業種コード: 1011)
# ==============================================================
# 分析対象企業: 5332.T / 5333.T / 5201.T / 5214.T
# ==============================================================

import sys
from pathlib import Path

_here = Path(__file__).parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here.parent))

from sector_agents.base_agent import (
    fetch_financials, build_macro_signals, build_result
)

SECTOR_CODE = "1011"
SECTOR_NAME = "ガラス・土石製品"

TARGET_TICKERS = ['5332.T', '5333.T', '5201.T', '5214.T']

SECTOR_NOTE = """
### ガラス・土石製品 ポイント
- ガラス・セメント・セラミクス。建設・自動車向けが主需要。
- **建設需要**: 住宅・インフラ投資に連動。金利上昇は逆風。
- **エネルギーコスト**: 焼成・溶融工程でエネルギー多消費型。
- **高機能ガラス**: 車載用・OLED用ガラスは高成長領域。
"""


def run_analysis(sector_code: str, cfg) -> dict:
    """
    ガラス・土石製品 の分析を実行する。
    stock_analyzer_main.py の run_sector_analysis() から動的ロードで呼ばれる。
    """
    result = {}
    print(f"  [{SECTOR_CODE}] {SECTOR_NAME} 分析開始...")

    companies = fetch_financials(TARGET_TICKERS)
    ok = sum(1 for c in companies if c.get("fetch_ok"))
    print(f"    財務データ取得: {ok}/{len(TARGET_TICKERS)} 銘柄成功")

    macro_signals = build_macro_signals(SECTOR_CODE, cfg)
    tw = sum(1 for s in macro_signals if s["signal"] == "tailwind")
    hw = sum(1 for s in macro_signals if s["signal"] == "headwind")
    print(f"    マクロシグナル: 追い風 {tw}件 / 逆風 {hw}件")

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
