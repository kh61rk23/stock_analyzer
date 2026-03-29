# ==============================================================
# sector_agents/1019_agent.py
# その他製品 エージェント v1.0  （東証業種コード: 1019）
# ==============================================================
# 【分析対象企業】任天堂 / バンダイナムコホールディングス / 大日本印刷 / 富士フイルムホールディングス / 凸版印刷
# 【マクロ感応度】
#   個人消費拡大 → 💚 追い風（ゲーム・玩具・エンタメ需要増）
#   円安進行   → 💚 追い風（ゲームの海外収益円換算増）
#   デジタル化進展 → 💚 追い風（ゲームのデジタル販売・サブスク拡大）
# ==============================================================

import sys
from pathlib import Path

_here = Path(__file__).parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here.parent))

from sector_agents.base_agent import (
    fetch_financials, build_macro_signals, build_result
)

SECTOR_CODE  = "1019"
SECTOR_NAME  = "その他製品"

TARGET_TICKERS = [
    "7974.T",  # 任天堂
    "7832.T",  # バンダイナムコホールディングス
    "7912.T",  # 大日本印刷
    "4901.T",  # 富士フイルムホールディングス
    "7911.T",  # 凸版印刷
]

SECTOR_NOTE = """
### その他製品 ポイント
- **任天堂の圧倒的存在感**: 時価総額でセクター最大。Switch後継機の発売が最重要カタリスト。
- **コンテンツIP**: バンダイナムコはガンダム・ワンピース等のIPを活用した多角展開。
- **印刷の変革**: 大日本印刷・凸版はデジタル化対応でITセキュリティに事業転換中。
- **注目指標**: ゲームソフト売上、為替(USD/JPY)、個人消費支出、スマートフォン出荷台数。
"""

def run_analysis(sector_code: str, cfg,
                 macro_snapshot: dict = None) -> dict:
    print(f"  🎮 [{SECTOR_CODE}] {SECTOR_NAME} 分析開始...")

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
