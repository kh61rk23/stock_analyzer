"""EDINETBQClientのローカル認証設定を検証する。"""

import json

import edinet_bq_client as module
from taxonomy_loader import load_to_bq


def _reset_singleton():
    module.EDINETBQClient._instance = None


def test_project_id_falls_back_to_service_account_file(tmp_path, monkeypatch):
    credential = tmp_path / "service-account.json"
    credential.write_text(
        json.dumps({"project_id": "project-from-credential"}),
        encoding="utf-8",
    )
    fake_creds = object()
    captured = {}

    monkeypatch.delenv("BQ_CREDENTIALS", raising=False)
    monkeypatch.setattr(module, "BQ_PROJECT_ID", "")
    monkeypatch.setattr(module, "BQ_CREDENTIALS_PATH", str(credential))
    monkeypatch.setattr(
        module.service_account.Credentials,
        "from_service_account_file",
        lambda *_args, **_kwargs: fake_creds,
    )
    monkeypatch.setattr(
        module.bigquery,
        "Client",
        lambda **kwargs: captured.update(kwargs) or object(),
    )
    _reset_singleton()

    client = module.EDINETBQClient()

    assert client.project == "project-from-credential"
    assert client.dataset == "project-from-credential.edinet"
    assert captured["project"] == "project-from-credential"
    assert captured["credentials"] is fake_creds
    _reset_singleton()


def test_explicit_project_id_has_priority(tmp_path, monkeypatch):
    credential = tmp_path / "service-account.json"
    credential.write_text(
        json.dumps({"project_id": "project-from-credential"}),
        encoding="utf-8",
    )

    monkeypatch.delenv("BQ_CREDENTIALS", raising=False)
    monkeypatch.setattr(module, "BQ_PROJECT_ID", "explicit-project")
    monkeypatch.setattr(module, "BQ_CREDENTIALS_PATH", str(credential))
    monkeypatch.setattr(
        module.service_account.Credentials,
        "from_service_account_file",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(module.bigquery, "Client", lambda **_kwargs: object())
    _reset_singleton()

    client = module.EDINETBQClient()

    assert client.project == "explicit-project"
    _reset_singleton()


def test_taxonomy_loader_uses_client_project_when_config_value_is_empty(monkeypatch):
    class FakeJob:
        def result(self):
            return self

    class FakeClient:
        def __init__(self):
            self.queries = []

        def query(self, sql, **_kw):
            self.queries.append(sql)
            return FakeJob()

        def create_dataset(self, ds, exists_ok=False):
            assert ds.dataset_id in ("master", "master_us") and exists_ok
            return ds

        def load_table_from_file(self, _buf, table, job_config=None, **_kw):
            assert table == "credential-project.master._staging_tax_node"
            return FakeJob()

    fake_bq = type(
        "FakeBQ",
        (),
        {"project": "credential-project", "client": FakeClient()},
    )()
    monkeypatch.setattr(module.EDINETBQClient, "_instance", None)
    monkeypatch.setattr(
        "edinet_bq_client.EDINETBQClient",
        lambda: fake_bq,
    )
    cfg = type("Cfg", (), {"BQ_PROJECT_ID": ""})()
    row = {
        "node_id": "node",
        "parent_id": None,
        "node_name_ja": "ノード",
        "node_name_en": "node",
        "node_level": 1,
        "node_type": "domain",
        "description": "",
        "taxonomy_version": "v1",
        "effective_from": "2026-07-20",
        "effective_to": None,
        "is_active": True,
        "supply_chain_pos": None,
        "related_commodities": "[]",
        "related_countries": "[]",
        "notes": "",
        "updated_at": "2026-07-20T00:00:00+00:00",
    }

    ok, count = load_to_bq([row], cfg)

    assert ok is True
    assert count == 1
    assert all("`.taxonomy" not in sql and "`.master" not in sql for sql in fake_bq.client.queries)
