"""W5 BigQuery schema compatibility entrypoint.

The first implementation used incompatible columns in the unversioned tables.
New deployments use the non-destructive ``*_p1`` tables from
``setup_db_integration_bq.py``.
"""
from __future__ import annotations

from setup_db_integration_bq import _statements


def create_all() -> None:
    from edinet_bq_client import EDINETBQClient

    client = EDINETBQClient()
    statements = [sql for sql in _statements(client.project) if ".real_estate" in sql]
    for sql in statements:
        client.client.query(sql).result()


if __name__ == "__main__":
    create_all()
