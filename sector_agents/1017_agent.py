# ==============================================================
# sector_agents/1017_agent.py
# 輸送用機器 エージェント v1.0  （東証業種コード: 1017）
# ==============================================================
# 【分析対象企業】トヨタ / ホンダ / 日産 / スズキ / マツダ
# 【マクロ感応度】
#   円安進行      → 💚 追い風（輸出採算改善・海外利益の円換算増）
#   円高進行      → 🔴 逆風  （輸出採算悪化）
#   米景気後退    → 🔴 逆風  （米国向け輸出需要の減少）
#   金利上昇      → 🟡 注意  （販売金融コスト上昇）
# ==============================================================

import sys
from pathlib import Path

_here = Path(__file__).parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here.parent))

from sector_agents.base_agent import (
    fetch_financials, build_macro_signals, build_result
)

SECTOR_CODE  = "1017"
SECTOR_NAME  = "輸送用機器"

TARGET_TICKERS = [
    "7203.T",  # トヨタ自動車
    "7267.T",  # 本田技研工業
    "7201.T",  # 日産自動車
    "7269.T",  # スズキ
    "7261.T",  # マツダ
]

SECTOR_NOTE = """
### 輸送用機器 ポイント
- **為替感応度**: トヨタは USD/JPY が 1 円動くと営業利益が約 450 億円変動（公表値ベース）。
- **EV シフト**: BYD 等の中国勢との競合激化。EV 専用プラットフォーム投資が収益を圧迫中。
- **半導体**: 車載半導体の調達リスクは 2024 年以降は緩和傾向だが在庫調整に注意。
- **米国市場**: 米 UAW 問題・関税政策変更リスクが高い。米国販売台数を月次で確認。
- **注目指標**: 北米・中国販売台数、為替感応度（各社の決算資料に記載）、EV 比率。
"""


def run_analysis(sector_code: str, cfg) -> dict:
    print(f"  🚗 [{SECTOR_CODE}] {SECTOR_NAME} 分析開始...")

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
