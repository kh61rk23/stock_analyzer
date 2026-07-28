"""BigQuery 費用ガード（maximum_bytes_billed）の回帰テスト。

2026-07-28 追加。GCP の課金を有効化したため、暴走クエリで無料枠（月1TB）を
食い潰さないよう 1クエリのスキャン上限を設けた。

このガードは edinet_bq_client が bigquery.Client(default_query_job_config=...)
に設定することで、各所の QueryJobConfig(query_parameters=[...]) を
書き換えずに全クエリへ効かせている。
その前提（既定 job_config が呼び出し側の未設定プロパティを補完する）が
ライブラリ更新で壊れると**黙って上限が外れる**ため、ここで検査する。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

bigquery = pytest.importorskip("google.cloud.bigquery")

import config  # noqa: E402


# ── config 側 ────────────────────────────────────────────────

def test_config_defines_max_bytes_billed():
    """SSOT である config に上限が定義されていること。"""
    assert hasattr(config, "BQ_MAX_BYTES_BILLED")
    assert isinstance(config.BQ_MAX_BYTES_BILLED, int)
    assert config.BQ_MAX_BYTES_BILLED > 0


def test_max_bytes_billed_is_within_free_tier():
    """上限が無料枠（月1TB）を1クエリで食い潰さない水準であること。

    上限そのものが 1TB を超えていたら、ガードとして意味を成さない。
    """
    one_tb = 1024 ** 4
    assert config.BQ_MAX_BYTES_BILLED < one_tb


def test_max_bytes_billed_is_large_enough_for_normal_use():
    """正常運用を妨げない水準であること（全テーブル合計は約20MB）。"""
    assert config.BQ_MAX_BYTES_BILLED >= 1024 ** 3   # 1GB以上


# ── ライブラリ挙動（ここが壊れると黙って上限が外れる）────────────

def test_default_job_config_fills_unset_properties():
    """呼び出し側が query_parameters だけ渡しても上限が生き残ること。

    edinet_bq_client の各所は
        bigquery.QueryJobConfig(query_parameters=[...])
    を渡している。既定 job_config とのマージで maximum_bytes_billed が
    引き継がれなければ、ガードは全く効かない。
    """
    default_cfg = bigquery.QueryJobConfig(
        maximum_bytes_billed=config.BQ_MAX_BYTES_BILLED
    )
    per_call = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("ticker", "STRING", "8766.T"),
    ])

    merged = per_call._fill_from_default(default_cfg)

    assert merged.maximum_bytes_billed == config.BQ_MAX_BYTES_BILLED
    # 呼び出し側のパラメータが消えていないこと
    assert len(merged.query_parameters) == 1
    assert merged.query_parameters[0].name == "ticker"


def test_explicit_per_call_value_wins():
    """呼び出し側が明示指定したときはそちらが優先されること（仕様の確認）。"""
    default_cfg = bigquery.QueryJobConfig(maximum_bytes_billed=10 * 1024 ** 3)
    per_call    = bigquery.QueryJobConfig(maximum_bytes_billed=123)

    assert per_call._fill_from_default(default_cfg).maximum_bytes_billed == 123


# ── クライアント組み立て（認証不要・bigquery.Client をモック）──────

def test_client_is_constructed_with_default_job_config(monkeypatch):
    """EDINETBQClient が default_query_job_config に上限を渡していること。"""
    import edinet_bq_client as ebc

    captured: dict = {}

    class _FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(ebc.bigquery, "Client", _FakeClient)

    # 認証を迂回する: BQ_CREDENTIALS（JSON文字列）経路に偽の資格情報を通す。
    # _init_client は環境変数 BQ_CREDENTIALS があればファイルを読まない。
    monkeypatch.setenv("BQ_CREDENTIALS", '{"project_id": "dummy-project"}')
    monkeypatch.setattr(
        ebc.service_account.Credentials,
        "from_service_account_info",
        classmethod(lambda cls, info, scopes=None: object()),
    )

    # シングルトンを毎回作り直す
    ebc.EDINETBQClient._instance = None
    try:
        client = ebc.EDINETBQClient()
    finally:
        ebc.EDINETBQClient._instance = None

    job_config = captured.get("default_query_job_config")
    assert job_config is not None, \
        "default_query_job_config が渡されていない＝費用ガードが効かない"
    assert job_config.maximum_bytes_billed == client.max_bytes_billed
    assert job_config.maximum_bytes_billed > 0
