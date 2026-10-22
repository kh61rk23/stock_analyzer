"""edinet_gov_docs: 書類一覧の行・中身（CSV）の読み取り・遡る日の決め方。BigQuery・API には触らない。"""
import io
import zipfile
from datetime import date, datetime

import edinet_gov_docs as g
import ops_health as oh


def _zip(rows):
    head = '"要素ID"\t"項目名"\t"コンテキストID"\t"相対年度"\t"連結・個別"\t"期間・時点"\t"ユニットID"\t"単位"\t"値"'
    body = "\n".join("\t".join(f'"{c}"' for c in r) for r in rows)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("XBRL_TO_CSV/jplvh-test.csv", (head + "\n" + body).encode("utf-16"))
    return buf.getvalue()


META = {"docID": "S100TEST", "docTypeCode": "360", "edinetCode": "E00001", "issuerEdinetCode": "E00002"}


def test_facts_skip_dashes_split_context_and_type_values():
    blob = _zip([
        ["jplvh_cor:HoldingRatioOfShareCertificatesEtc", "株券等保有割合", "FilingDateInstant_x_E00001-000FilerLargeVolumeHolder1Member", "", "", "", "pure", "", "0.0512"],
        ["jplvh_cor:PurposeOfHolding", "保有目的", "FilingDateInstant_x_E00001-000FilerLargeVolumeHolder1Member", "", "", "", "", "", "純投資" * 10],
        ["jplvh_cor:TotalArticle27233Item1", "合計", "FilingDateInstant", "", "", "", "shares", "", "－"],
        ["jplvh_cor:NameOfIssuer", "発行者の名称", "FilingDateInstant", "", "", "", "", "", "テスト株式会社"],
    ])
    rows, labels = g.facts_from_csv_zip(blob, META, date(2026, 10, 21), text_max=12)
    assert len(rows) == 3                                     # 「－」は入れない
    ratio = next(r for r in rows if r["element_id"] == g.element_id("jplvh_cor:HoldingRatioOfShareCertificatesEtc"))
    assert ratio["value_num"] == 0.0512 and ratio["value_text"] is None
    assert (ratio["period"], ratio["member"]) == (None, "FilerLargeVolumeHolder1Member")   # 既定の時点と提出者コードは持たない
    purpose = next(r for r in rows if r["element_id"] == g.element_id("jplvh_cor:PurposeOfHolding"))
    assert purpose["value_num"] is None and len(purpose["value_text"]) == 12   # 文字は上限で切る
    issuer = next(r for r in rows if r["element_id"] == g.element_id("jplvh_cor:NameOfIssuer"))
    assert issuer["member"] is None and issuer["issuer_edinet_code"] == "E00002"
    assert labels["jplvh_cor:PurposeOfHolding"] == "保有目的"


def test_doc_rows_and_targets():
    res = [{"seqNumber": 1, "docID": "A", "docTypeCode": "350", "csvFlag": "1", "withdrawalStatus": "0", "filerName": "x"},
           {"seqNumber": 2, "docID": "B", "docTypeCode": "120", "csvFlag": "1", "withdrawalStatus": "0"},
           {"seqNumber": 3, "docID": "C", "docTypeCode": "360", "csvFlag": "0", "withdrawalStatus": "0"},
           {"seqNumber": 4, "docID": "D", "docTypeCode": "360", "csvFlag": "1", "withdrawalStatus": "1"}]
    rows = g.doc_rows(res, date(2026, 10, 21), "now")
    assert rows[0]["doc_id"] == "A" and rows[0]["seq_number"] == 1 and rows[0]["submit_date"] == "2026-10-21"
    assert {"doc_type_code", "csv_flag", "issuer_edinet_code", "jcn", "submit_date_time"} <= set(rows[0])
    assert [t["docID"] for t in g.fact_targets(res, ("350", "360"))] == ["A"]   # CSV なし・取り下げは取らない
    assert g.targets_hash(res[:2]) == g.targets_hash(list(reversed(res[:2])))


def test_dates_recent_then_backfill_from_oldest_done():
    st = {"dates": {"2026-10-10": {}, "2026-10-21": {}}}
    recent, back = g.dates_to_run(st, date(2026, 10, 22), 3, date(2026, 10, 7))
    assert recent == [date(2026, 10, 22), date(2026, 10, 21), date(2026, 10, 20)]
    assert back == [date(2026, 10, 9), date(2026, 10, 8), date(2026, 10, 7)]   # 済んだ最も古い日の前日から下限まで
    recent, back = g.dates_to_run({}, date(2026, 10, 22), 2, date(2026, 10, 19))
    assert back == [date(2026, 10, 20), date(2026, 10, 19)]


def test_storage_warning_levels():
    now = datetime(2026, 10, 23, 9, 0)
    ok = {"measured_at": "2026-10-23T05:00:00", "total_gb": 3.4, "top_tables": {}}
    assert oh.bq_storage_issues(ok, now, 7.0, 9.0, 8.0) == []
    warn = oh.bq_storage_issues({**ok, "total_gb": 7.5}, now, 7.0, 9.0, 8.0)
    assert warn[0]["severity"] == "medium" and "8GB で止まる" in warn[0]["text"]
    assert oh.bq_storage_issues({**ok, "total_gb": 9.2}, now, 7.0, 9.0, 8.0)[0]["severity"] == "high"
    stale = oh.bq_storage_issues({**ok, "measured_at": "2026-10-18T05:00:00"}, now, 7.0, 9.0, 8.0)
    assert "測っていない" in stale[0]["text"]


def test_element_id_fits_int64_and_view_uses_ids():
    eid = g.element_id("jplvh_cor:HoldingRatioOfShareCertificatesEtc")
    assert -2**63 <= eid < 2**63 and eid == g.element_id("jplvh_cor:HoldingRatioOfShareCertificatesEtc")
    sql = g.large_holdings_view_sql("p")
    assert f"element_id={eid}" in sql and "holding_ratio" in sql and "filer_name" in sql
