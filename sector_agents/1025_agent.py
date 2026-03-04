# ==============================================================
# sector_agents/1025_agent.py
# 情報・通信業 エージェント v1.0  （東証業種コード: 1025）
# ==============================================================
# 【分析対象企業】NTT / ソフトバンクG / LYコーポレーション / NTTデータ / KDDI
# 【マクロ感応度】
#   AI・半導体ブーム → 💚 追い風（AIクラウド・データセンター投資恩恵）
#   米景気後退      → 🔴 逆風  （IT 投資縮小リスク）
#   地政学リスク    → 🟡 注意  （デカップリング・海外事業リスク）
#   円安            → 🟡 注意  （海外投資収益増 vs 海外仕入コスト増）
# ==============================================================

import sys
from pathlib import Path

_here = Path(__file__).parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here.parent))

from sector_agents.base_agent import (
    fetch_financials, build_macro_signals, build_result
)

SECTOR_CODE  = "1025"
SECTOR_NAME  = "情報・通信業"

TARGET_TICKERS = [
    "9432.T",  # 日本電信電話（NTT）
    "9984.T",  # ソフトバンクグループ
    "4689.T",  # LYコーポレーション（旧 Z ホールディングス）
    "9633.T",  # ビプロジー（旧NTTデータ系ITサービス）※9613.TはNTT TOBにより上場廃止
    "9433.T",  # KDDI
]

SECTOR_NOTE = """
### 情報・通信業 ポイント
- **生成 AI 投資**: Microsoft・Google 連携のクラウド AI 基盤構築が国内 IT 大手の追い風。
  データセンター投資・GPU クラスタ需要が増加中。
- **ソフトバンクG**: ARM / WeWork など投資ポートフォリオの評価変動が純利益を大きく左右。
  ARM の AI チップ普及率が株価の主要ドライバー。
- **通信キャリア（NTT / KDDI）**: 国内市場は成熟。ARPU 改善とコスト削減が課題。
  データセンター・法人 DX が成長ドライバー。
- **セキュリティ**: サイバー攻撃増加でセキュリティ投資が業界横断的に拡大中。
- **注目指標**: クラウド ARR（年次経常収益）、データセンター稼働率、AI 関連受注高。
"""


def run_analysis(sector_code: str, cfg) -> dict:
    print(f"  💻 [{SECTOR_CODE}] {SECTOR_NAME} 分析開始...")

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
