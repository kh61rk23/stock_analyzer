# 予算枯渇・レート制限で取得ループを打ち切る契約
# 2026-08-27: 打ち切りが無く（edinetdb）／last_error が立たず（fudosan）、
# 予算切れ後も全社を空振りで回して failure だけを積み上げていた。
import edinetdb_ingestor as ei
import fudosan_ingestor as fi


class _DeadClient:
    """予算切れ・レート制限で常に None を返すクライアント"""

    def __init__(self, error="budget"):
        self.last_error = error
        self.calls = 0

    def get(self, _path, _params=None):
        self.calls += 1
        return None


def test_stop_ingest_detects_both_terminal_states():
    assert ei._stop_ingest(_DeadClient("budget")) is True
    assert ei._stop_ingest(_DeadClient("rate_limited")) is True
    # 一過性の失敗では止めない（次の企業は成功しうる）
    assert ei._stop_ingest(_DeadClient("not_found")) is False
    assert ei._stop_ingest(_DeadClient(None)) is False


def test_budget_exhaustion_sets_last_error_on_get(tmp_path):
    """get() の予算切れパスでも last_error が立つ

    ここが None のままだと呼び出し側の打ち切り判定が働かず、
    fudosan_ingestor が45社を空振りで回していた。
    """
    from db_rest_client import DBRestClient

    client = DBRestClient(
        "https://example.test/v1", "key", tmp_path,
        daily_budget=0,          # 最初から予算ゼロ
        source_name="fudosandb",
        usage_endpoint=None, min_interval=0,
    )
    assert client.get("/corporate/real-estate-holdings") is None
    assert client.last_error == "budget"


def test_rate_limited_client_makes_no_further_http_calls(tmp_path, monkeypatch):
    """429で打ち切った後は以降のGETでHTTPを一切出さない"""
    import db_rest_client as drc

    sent = 0

    def fake_get(*_a, **_k):
        nonlocal sent
        sent += 1

        class _R:
            status_code = 429

            def json(self):
                return {}
        return _R()

    monkeypatch.setattr(drc.requests, "get", fake_get)
    monkeypatch.setattr(drc.time, "sleep", lambda _s: None)
    client = drc.DBRestClient(
        "https://example.test/v1", "key", tmp_path, 90, "fudosandb",
        usage_endpoint=None, min_interval=0,
    )

    assert client.get("/corporate/real-estate-holdings") is None
    assert client.last_error == "rate_limited"
    burned = sent
    for _ in range(10):
        client.get("/corporate/real-estate-holdings")
    assert sent == burned, "打ち切り後もHTTPを出している"
    # ingestor 側はこの last_error を見て周回を止める
    assert fi and ei._stop_ingest(client) is True
