# ==============================================================
# sector_agents/1031_agent.py
# その他金融業 エージェント v1.0  （東証業種コード: 1031）
# ==============================================================
# 【分析対象企業】オリックス / 東京センチュリー / イオンフィナンシャルサービス / 三菱HCキャピタル / アイフル
# 【マクロ感応度】
#   金利上昇   → 🟡 注意  （調達コスト上昇・信用コスト増加リスク）
#   個人消費拡大 → 💚 追い風（クレジットカード・消費者金融需要増）
#   景気後退   → 🔴 逆風  （貸倒リスク上昇・貸出残高減少）
# ==============================================================

import sys
from pathlib import Path

_here = Path(__file__).parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here.parent))

from sector_agents.base_agent import (
    fetch_financials, build_macro_signals, build_result
)

SECTOR_CODE  = "1031"
SECTOR_NAME  = "その他金融業"

TARGET_TICKERS = [
    "8591.T",  # オリックス
    "8439.T",  # 東京センチュリー
    "8570.T",  # イオンフィナンシャルサービス
    "8593.T",  # 三菱HCキャピタル
    "8515.T",  # アイフル
]

SECTOR_NOTE = """
### その他金融業 ポイント
- **オリックスの多角化**: リース・生保・不動産・空港運営など極めて多角化。
- **信販・クレジット**: イオンFS等はグループの購買データを活用した与信管理が強み。
- **貸金業規制**: 上限金利規制・過払い金問題の動向が消費者金融業者に影響。
- **注目指標**: 消費者ローン残高、クレジットカード利用額、延滞率、長期金利。
"""


def run_analysis(sector_code: str, cfg,
                 macro_snapshot: dict = None) -> dict:
    print(f"  💳 [{SECTOR_CODE}] {SECTOR_NAME} 分析開始...")

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
