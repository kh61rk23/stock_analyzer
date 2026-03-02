# ==============================================================
# sector_agents/1028_agent.py
# 銀行業 エージェント v1.0  （東証業種コード: 1028）
# ==============================================================
# 【分析対象企業】三菱UFJ / 三井住友 / みずほ / りそな / 三菱UFJ信託
# 【マクロ感応度】
#   金利上昇  → 💚 追い風（預貸金利ザヤ拡大）
#   金利低下  → 🔴 逆風  （預貸金利ザヤ縮小）
#   円安進行  → 🟡 注意  （海外収益の円換算増、為替リスクも拡大）
# ==============================================================

import sys
from pathlib import Path

# base_agent を import するためのパス解決
_here = Path(__file__).parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here.parent))

from sector_agents.base_agent import (
    fetch_financials, build_macro_signals, build_result
)

# ── 業種固有設定 ──────────────────────────────────────────────
SECTOR_CODE  = "1028"
SECTOR_NAME  = "銀行業"

# 分析対象銘柄（時価総額上位・代表的企業）
TARGET_TICKERS = [
    "8306.T",  # 三菱UFJフィナンシャル・グループ
    "8316.T",  # 三井住友フィナンシャルグループ
    "8411.T",  # みずほフィナンシャルグループ
    "8308.T",  # りそなホールディングス
    "8309.T",  # 三井住友トラスト・ホールディングス
]

# 業種特化の補足インサイト（マクロシグナルに加えて表示）
SECTOR_NOTE = """
### 銀行業 ポイント
- **NIM（純利鞘）**: 政策金利と短期市場金利の動向が収益の核。
- **信用コスト**: 景気悪化局面では不良債権増加リスク。
- **海外収益**: メガバンクは米国・アジア向け与信が大きく、ドル金利・景気に連動。
- **注目指標**: 貸出金利回り、預金金利、貸倒引当金、BIS 自己資本比率。
"""


def run_analysis(sector_code: str, cfg) -> dict:
    """
    銀行業の分析を実行する。
    stock_analyzer_main.py の run_sector_analysis() から動的ロードで呼ばれる。

    Args:
        sector_code: "1028"
        cfg: config モジュール

    Returns:
        SECTOR_AGENT_RESULT_SCHEMA 準拠の辞書
    """
    print(f"  🏦 [{SECTOR_CODE}] {SECTOR_NAME} 分析開始...")

    # 財務データ取得
    companies = fetch_financials(TARGET_TICKERS)
    ok = sum(1 for c in companies if c.get("fetch_ok"))
    print(f"    財務データ取得: {ok}/{len(TARGET_TICKERS)} 銘柄成功")

    # マクロシグナル評価
    macro_signals = build_macro_signals(SECTOR_CODE, cfg)
    tw = sum(1 for s in macro_signals if s["signal"] == "tailwind")
    hw = sum(1 for s in macro_signals if s["signal"] == "headwind")
    print(f"    マクロシグナル: 追い風 {tw}件 / 逆風 {hw}件")

    # 結果を組み立てて返す
    result = build_result(SECTOR_CODE, SECTOR_NAME, companies, macro_signals, cfg)

    # 業種固有のノートを追記
    result["insight_text"] += "\n" + SECTOR_NOTE
    result["sector_note"]   = SECTOR_NOTE

    print(f"    ✅ {SECTOR_NAME}: スコア {result['composite_score']}点 / リスク {result['risk_level']}")
    return result


# 単体テスト用
if __name__ == "__main__":
    import importlib.util, sys
    for p in ["/content/config.py", str(Path(__file__).parents[1] / "config.py")]:
        if Path(p).exists():
            spec = importlib.util.spec_from_file_location("config", p)
            cfg  = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cfg)
            break
    result = run_analysis(SECTOR_CODE, cfg)
    print(result["insight_text"])
