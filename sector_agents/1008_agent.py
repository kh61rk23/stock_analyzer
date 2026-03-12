# ==============================================================
# sector_agents/1008_agent.py
# 医薬品 エージェント v1.0  （東証業種コード: 1008）
# ==============================================================
# 【分析対象企業】中外製薬 / 第一三共 / エーザイ / 塩野義製薬 / 鳥居薬品
# 【マクロ感応度】
#   円安進行  → 💚 追い風（海外収益の円換算増・輸出競争力向上）
#   薬価改定  → 🔴 逆風  （国内薬価引き下げが収益圧迫）
#   高齢化進展 → 💚 追い風（医薬品需要の長期的拡大）
# ==============================================================

import sys
from pathlib import Path

_here = Path(__file__).parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here.parent))

from sector_agents.base_agent import (
    fetch_financials, build_macro_signals, build_result
)

SECTOR_CODE  = "1008"
SECTOR_NAME  = "医薬品"

TARGET_TICKERS = [
    "4519.T",  # 中外製薬
    "4568.T",  # 第一三共
    "4523.T",  # エーザイ
    "4507.T",  # 塩野義製薬
    "4151.T",  # 鳥居薬品
]

SECTOR_NOTE = """
### 医薬品 ポイント
- **グローバル展開**: 中外製薬（ロシュグループ）・第一三共（欧米で抗体薬物複合体）が高成長。
- **薬価リスク**: 日本は2年ごとの薬価改定で既収載品の価格が継続的に下がる。
- **新薬パイプライン**: 後期臨床試験（フェーズ3）の結果が株価を大きく動かす。
- **注目指標**: 薬価改定率、臨床試験フェーズ移行、海外売上高比率、R&D費用比率。
"""


def run_analysis(sector_code: str, cfg,
                 macro_snapshot: dict = None) -> dict:
    print(f"  💊 [{SECTOR_CODE}] {SECTOR_NAME} 分析開始...")

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
