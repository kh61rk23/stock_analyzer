# ==============================================================
# sector_agents/1002_agent.py
# 鉱業 エージェント v1.0  （東証業種コード: 1002）
# ==============================================================
# 【分析対象企業】石油資源開発 / INPEX / K&Oエナジーグループ / 昭和四日市石油 / 日鐵住金建材
# 【マクロ感応度】
#   原油高    → 💚 追い風（エネルギー資源の採掘収益増加）
#   原油安    → 🔴 逆風  （採掘コストが価格を上回るリスク）
#   円安進行  → 💚 追い風（ドル建て資源収益の円換算増）
# ==============================================================

import sys
from pathlib import Path

_here = Path(__file__).parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here.parent))

from sector_agents.base_agent import (
    fetch_financials, build_macro_signals, build_result
)

SECTOR_CODE  = "1002"
SECTOR_NAME  = "鉱業"

TARGET_TICKERS = [
    "1662.T",  # 石油資源開発
    "1605.T",  # INPEX
    "1663.T",  # K&Oエナジーグループ
    "5016.T",  # 昭和四日市石油
    "1884.T",  # 日鐵住金建材
]

SECTOR_NOTE = """
### 鉱業 ポイント
- **原油・天然ガス価格**: INPEX等の収益は資源価格に直結。WTI・LNG価格に最注目。
- **エネルギー転換リスク**: 脱炭素化の長期トレンドは化石燃料関連企業に構造的逆風。
- **地政学リスク**: 中東・ロシア情勢が資源価格・供給に即影響。
- **注目指標**: WTI原油先物、LNG価格、採掘コスト(Lifting cost)、埋蔵量。
"""


def run_analysis(sector_code: str, cfg,
                 macro_snapshot: dict = None) -> dict:
    print(f"  ⛏️ [{SECTOR_CODE}] {SECTOR_NAME} 分析開始...")

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
