"""score_history のうち、同一の insights 探索契約を共有するI/O（U-7残）。

本モジュールが統合するのは jp_us_sector_bridge / screener_sector_heatmap /
self_evaluator の完全同一だった3実装だけである。DATA_DIR直下も探索する4候補型や、
単一パス型の読み手は意図的に対象外とする。
"""

from __future__ import annotations

import json
from pathlib import Path


def load_score_history(cfg) -> dict:
    """insights用の2候補を従来順で読み、失敗時は次候補または空dictを返す。"""
    candidates = [
        Path(getattr(cfg, "DATA_DIR", "stock_data/data")).parent
        / "insights" / "score_history.json",
        Path("stock_data/insights/score_history.json"),
    ]
    for path in candidates:
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
    return {}
