# ==============================================================
# sector_agents/1018_agent.py
# 精密機器 エージェント v1.0  （東証業種コード: 1018）
# ==============================================================
# 【分析対象企業】ニコン / オリンパス / ファナック / シチズン時計 / コニカミノルタ
# 【マクロ感応度】
#   半導体需要拡大 → 💚 追い風（半導体製造装置・計測器需要増）
#   円安進行   → 💚 追い風（輸出比率の高い精密機器メーカーに有利）
#   医療投資拡大 → 💚 追い風（医療機器・内視鏡の需要増）
#   中国景気減速 → 🟡 注意  （FA機器・工作機械向け需要減）
# ==============================================================

import sys
from pathlib import Path

_here = Path(__file__).parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here.parent))

from sector_agents.base_agent import (
    fetch_financials, build_macro_signals, build_result
)

SECTOR_CODE  = "1018"
SECTOR_NAME  = "精密機器"

TARGET_TICKERS = [
    "7731.T",  # ニコン
    "7733.T",  # オリンパス
    "6954.T",  # ファナック
    "7762.T",  # シチズン時計
    "4902.T",  # コニカミノルタ
]

SECTOR_NOTE = """
### 精密機器 ポイント
- **ファナック**: CNCシステム・産業ロボット世界最大手。工場自動化の象徴的銘柄。
- **オリンパス**: 内視鏡で世界シェア7割超。医療機器特化で安定高収益。
- **ニコン**: 半導体露光装置でASMLと競合。半導体サイクルに連動。
- **注目指標**: 半導体製造装置受注額、FA受注指数、医療機器出荷額。
"""

def run_analysis(sector_code: str, cfg,
                 macro_snapshot: dict = None) -> dict:
    print(f"  🔬 [{SECTOR_CODE}] {SECTOR_NAME} 分析開始...")

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
