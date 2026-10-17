"""政策DBの週次節・日次アラート・候補への付与（ボーナスOFFでスコア不変）。"""
import types

import alert_notifier
import stock_screener
import weekly_report_generator as wrg


def test_weekly_section_empty_input():
    assert wrg._section_seisakudb({}, None) == []
    assert wrg._section_seisakudb({"seisakudb": {"skipped": True}}, None) == []


def test_weekly_section_has_source_and_ranking():
    res = {
        "seisakudb": {
            "rank_top": [{"code": "7011", "listed_name": "三菱重工業", "total_oku": 100.5,
                          "subsidy_oku": 10.0, "procurement_oku": 90.5, "via_subsidiary_oku": 1.0}],
            "open_subsidies": [{"title": "補助金A", "institution_name": "経産省",
                                "acceptance_end": "2026-11-01", "subsidy_max_limit": "1億円"}],
        },
        "public_funding": {"7011.T": {"total_yen": 1e10, "recent_1y_yen": 1e9, "n_events": 3,
                                      "ratio_to_sales": 0.05, "econ_security_materials": ["半導体"]}},
    }
    out = wrg._section_seisakudb(res, None)
    text = "\n".join(out)
    assert out[-1].startswith("出典: 政策DB")
    assert "三菱重工業" in text and "補助金A" in text and "半導体" in text


def test_alert_new_keys():
    sk = {"recent": 1, "companies_ok": 1, "calls": 3, "budget": 1000, "certs_mapped": 4,
          "rank_top": [{"code": "7011", "listed_name": "三菱重工業", "total_oku": 12.0,
                        "subsidy_oku": 2.0, "procurement_oku": 10.0}],
          "open_subsidies": [{"title": "補助金A", "institution_name": "経産省", "acceptance_end": "2026-11-01"}]}
    a = alert_notifier.build_seisakudb_alert(sk)
    assert "公的支出の多い上場企業" in a["body"] and "締切が近い補助金" in a["body"]
    assert "出典: 政策DB" in a["body"]
    assert alert_notifier.build_seisakudb_alert({"skipped": True}) is None


def _cands():
    return [{"ticker": "1111.T", "composite_score": 50.0}, {"ticker": "2222.T", "composite_score": 51.0}]


FEATS = {"1111.T": {"econ_security_materials": ["半導体"], "ratio_to_sales": 0.05,
                    "recent_1y_yen": 1.0, "last_award": None}}


def test_bonus_off_keeps_score():
    cfg = types.SimpleNamespace(ENABLE_PUBLIC_FUNDING_BONUS=False)
    c = stock_screener.attach_public_funding(_cands(), FEATS, cfg)
    assert c[0]["ticker"] == "1111.T" and c[0]["composite_score"] == 50.0
    assert c[0]["public_funding_bonus"] == 0.0 and c[0]["public_funding"] is FEATS["1111.T"]
    assert "public_funding" not in c[1]


def test_bonus_on_adds_and_resorts():
    cfg = types.SimpleNamespace(ENABLE_PUBLIC_FUNDING_BONUS=True)
    c = stock_screener.attach_public_funding(_cands(), FEATS, cfg)
    assert c[0]["ticker"] == "1111.T" and c[0]["composite_score"] > 51.0
    assert c[0]["public_funding_reasons"]


def test_db_usage_lines_for_real_estate_and_seisakudb(tmp_path):
    """不動産DB・政策DB の節に API の使用回数と使い道（呼び出し元・種類・結果）が出る。"""
    import api_call_log
    import alert_notifier
    for st in (200, 200, 429):
        api_call_log.note("seisakudb", "/v1/funding/E01234", st, caller="seisakudb_ingestor:run", data_dir=tmp_path)
    from datetime import date
    today = date.today().isoformat()
    u = alert_notifier.load_db_usage("seisakudb", {"date": today, "calls": 2}, 1000, tmp_path, day=today)
    body = "\n".join(alert_notifier.db_usage_lines(u))
    assert "【API使用】2/1000回" in body and "seisakudb_ingestor:run 3回" in body and "HTTP 429×1" in body
    a = alert_notifier.build_seisakudb_alert({"recent": 1, "companies_ok": 1, "calls": 2, "budget": 1000}, u)
    assert "【API使用】2/1000回" in a["body"] and " / API 2/1000回" not in a["body"]
    r = alert_notifier.build_real_estate_alert({"skipped": False, "success": 1, "holdings": 3}, {},
                                               {"used": 49, "budget": 900, "calls": None, "stopped": None})
    assert "【API使用】49/900回" in r["body"]


def test_policy_change_line_shows_from_to():
    import alert_notifier
    rate = {"bank": "FRB", "label": "米FFレート(3M T-bill proxy)", "prev_value": 3.943, "curr_value": 4.0,
            "change_pt": 0.057, "direction": "引き締め方向（利上げ）", "prev_date": "2026-10-14", "curr_date": "2026-10-15"}
    fx = {"bank": "為替", "label": "USD/JPY（円安↑ 円高↓）", "ticker": "JPY=X", "prev_value": 151.40,
          "curr_value": 151.20, "change_pt": -0.198, "direction": "円高進行"}
    assert "3.943% → 4.000%（+0.057%pt）（2026-10-14→2026-10-15） → 引き締め方向" in alert_notifier.policy_change_line(rate)
    assert "151.40円 → 151.20円（-0.20円） → 円高進行" in alert_notifier.policy_change_line(fx)
    assert "+0.057%pt →" in alert_notifier.policy_change_line({**rate, "prev_value": None})


def test_own_prior_use_is_not_warned():
    """賃貸等不動産の取得で先に使った分だけ残りが減った日は、警告も内訳も出さない。"""
    import alert_notifier
    caps = {"caps": {"config": 97, "daily_remaining": 91}, "limiter": "daily_remaining", "final": 91}
    calls = {"total": 20, "by_caller": [("edinetdb_ingestor:run_edinetdb_ingest", 10),
                                        ("rental_property_ingestor:run", 15), ("api_key_health:_probe_edinetdb", 1)],
             "by_path": [], "by_status": [("200", 26)]}
    lines, warns = alert_notifier.api_usage_lines(calls, caps, 89)
    body = "\n".join(lines)
    assert not any("他で使われ" in w for w in warns) and "本日の枠の内訳" not in body
    assert "先に 16回使用" in body
    other = {**calls, "by_caller": [("edinetdb_ingestor:run_edinetdb_ingest", 10)]}
    _, warns2 = alert_notifier.api_usage_lines(other, caps, 89)
    assert any("他で使われ" in w for w in warns2)


def test_seisakudb_note_records_real_caller(tmp_path, monkeypatch):
    import api_call_log
    import seisakudb_ingestor as sk
    c = sk.SeisakuClient("k", tmp_path / "seisakudb_usage.json")

    def get():                      # 本番では SeisakuClient.get の中から _note を呼ぶ
        c._note("/companies/542A0/public-funding", 200)

    def run_x():
        get()
    run_x()
    s = api_call_log.summarize("seisakudb", None, tmp_path)
    assert s["by_caller"][0][0].endswith(":run_x") and s["by_path"][0][0] == "/companies/{code}/public-funding"
