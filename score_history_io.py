"""score_history のうち、同一の insights 探索契約を共有するI/O（U-7残）。

本モジュールが統合するのは jp_us_sector_bridge / screener_sector_heatmap /
self_evaluator の完全同一だった3実装だけである。DATA_DIR直下も探索する4候補型や、
単一パス型の読み手は意図的に対象外とする。
"""

from __future__ import annotations

import json
from pathlib import Path


def load_score_history(cfg) -> dict:
    """score_history.json を候補順（DATA_DIR → insights）に読み、失敗時は次候補または空dictを返す。"""
    # stock_analyzer_main._append_score_history は DATA_DIR/score_history.json に書く
    # （insights 側のファイルは通常存在しない）。insights だけを見ると
    # jp_us_sector_bridge・self_evaluator・screener_sector_heatmap がスキップされるため、DATA_DIR を先に見る。
    data_dir = Path(getattr(cfg, "DATA_DIR", "stock_data/data"))
    candidates = [
        data_dir / "score_history.json",
        Path("stock_data/data/score_history.json"),
        data_dir.parent / "insights" / "score_history.json",
        Path("stock_data/insights/score_history.json"),
    ]
    for path in candidates:
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
    return {}
