# ==============================================================
# sector_agents/1016_agent.py
# 電気機器 エージェント v1.0  （東証業種コード: 1016）
# ==============================================================
# 【分析対象企業】ソニー / 日立 / 富士通 / キーエンス / パナソニック
# 【マクロ感応度】
#   AI・半導体ブーム → 💚 追い風（半導体・電子部品需要増）
#   円安進行        → 💚 追い風（輸出採算改善）
#   金利上昇        → 🟡 注意  （設備投資コスト上昇）
#   米景気後退      → 🟡 注意  （IT 投資縮小リスク）
# ==============================================================

import sys
from pathlib import Path

_here = Path(__file__).parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here.parent))

from sector_agents.base_agent import (
    fetch_financials, build_macro_signals, build_result
)

SECTOR_CODE  = "1016"
SECTOR_NAME  = "電気機器"

TARGET_TICKERS = [
    "6758.T",  # ソニーグループ
    "6501.T",  # 日立製作所
    "6702.T",  # 富士通
    "6861.T",  # キーエンス
    "6752.T",  # パナソニックホールディングス
]

SECTOR_NOTE = """
### 電気機器 ポイント
- **AI・データセンター投資**: GPU・HBM メモリ需要急増。関連する電源装置・冷却システム需要に注目。
- **工場自動化（FA）**: キーエンスはセンサー・計測機器でグローバル高シェア。設備投資サイクルに連動。
- **ソニーのゲーム・エンタメ**: 半導体（イメージセンサー）とエンタメ収益の2本柱。
- **日立のデジタル事業**: Lumada プラットフォームで B2B デジタル化需要を取り込む戦略。
- **注目指標**: 半導体製造装置の受注残、FA 受注指数、IT 支出見通し（IDC/Gartner）。
"""


def run_analysis(sector_code: str, cfg,
                 macro_snapshot: dict = None) -> dict:
    print(f"  ⚡️ [{SECTOR_CODE}] {SECTOR_NAME} 分析開始...")

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
