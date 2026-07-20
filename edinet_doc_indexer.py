"""Maintain ``edinet.doc_index`` from EdinetDB /disclosures/search only."""
from __future__ import annotations

from edinetdb_ingestor import run_edinetdb_ingest


def run_doc_indexer(days_back: int = 1):
    import config
    previous = getattr(config, "EDINETDB_LOOKBACK_DAYS", 7)
    config.__dict__["EDINETDB_LOOKBACK_DAYS"] = max(1, int(days_back))
    try:
        return run_edinetdb_ingest(config, doc_index_only=True)
    finally:
        config.__dict__["EDINETDB_LOOKBACK_DAYS"] = previous


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="EdinetDB disclosure indexer")
    parser.add_argument("--days-back", type=int, default=1)
    print(run_doc_indexer(parser.parse_args().days_back))
