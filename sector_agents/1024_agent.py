# ==============================================================
# sector_agents/1024_agent.py
# 倉庫・運輸関連業 エージェント v1.0  （東証業種コード: 1024）
# ==============================================================
# 【分析対象企業】三菱倉庫 / 住友倉庫 / 渋沢倉庫 / 宇徳 / ケイヒン
# 【マクロ感応度】
#   貿易量拡大  → 💚 追い風（港湾荷役・倉庫需要増）
#   EC拡大    → 💚 追い風（物流センター需要の長期的増加）
#   金利上昇   → 🟡 注意  （倉庫・物流施設の開発コスト増）
# ==============================================================

import sys
from pathlib import Path

_here = Path(__file__).parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here.parent))

from sector_agents.base_agent import (
    fetch_financials, build_macro_signals, build_result
)

SECTOR_CODE  = "1024"
SECTOR_NAME  = "倉庫・運輸関連業"

TARGET_TICKERS = [
    "9301.T",  # 三菱倉庫
    "9302.T",  # 住友倉庫
    "9303.T",  # 渋沢倉庫
    "9304.T",  # 宇徳
    "9305.T",  # ケイヒン
]

SECTOR_NOTE = """
### 倉庫・運輸関連業 ポイント
- **3PL（3者物流）**: 企業の物流アウトソーシング需要拡大が長期的な成長ドライバー。
- **冷凍・冷蔵倉庫**: 食品・医薬品の温度管理需要増加で高付加価値化が進む。
- **港湾物流**: コンテナ取扱量・船便数に業績が連動。世界貿易量の指標に注目。
- **注目指標**: 港湾コンテナ取扱量、国際貿易統計、倉庫業月次統計、EC市場規模。
"""


def run_analysis(sector_code: str, cfg,
                 macro_snapshot: dict = None) -> dict:
    print(f"  🏭 [{SECTOR_CODE}] {SECTOR_NAME} 分析開始...")

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
