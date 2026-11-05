"""bq_compact: 表を書き換える SQL の形・見積もりの上限・取り込み側の絞り込み。BigQuery・API には触らない。"""
import io
import zipfile
from datetime import date
from types import SimpleNamespace

import bq_compact as bc
import edinet_gov_docs as g
import sec_filings as s


def test_price_dedupe_keeps_partition_cluster_and_not_null():
    sql = bc.price_dedupe_sql("p")
    assert "CREATE OR REPLACE TABLE `p.analytics.price_daily`" in sql
    assert "trade_date DATE NOT NULL" in sql and "ticker STRING NOT NULL" in sql
    assert "PARTITION BY trade_date CLUSTER BY ticker" in sql
    assert "ROW_NUMBER() OVER (PARTITION BY ticker, trade_date ORDER BY ingested_at DESC) = 1" in sql
    # price_daily_latest view と同じ「新しい1行」の規則
    import setup_db_integration_bq as st
    assert "PARTITION BY ticker, trade_date ORDER BY ingested_at DESC" in open(st.__file__, encoding="utf-8").read()


def test_sec_forms_delete_uses_config_prefixes():
    sql = bc.sec_forms_sql("p", ("8-K", "SC 13"))
    assert sql.startswith("DELETE FROM `p.us.sec_filings`")
    assert "STARTS_WITH(UPPER(form), '8-K')" in sql and "STARTS_WITH(UPPER(form), 'SC 13')" in sql
    try:
        bc.sec_forms_sql("p", ())
        assert False
    except ValueError:
        pass


def test_gov_facts_delete_only_lvh_types_and_keeps_view_elements():
    sql = bc.gov_facts_sql("p")
    assert "doc_type_code IN ('350', '360', '370')" in sql and "'180'" not in sql
    ids = g.lvh_element_ids()
    assert g.element_id("jplvh_cor:HoldingRatioOfShareCertificatesEtc") in ids
    assert all(str(i) in sql for i in ids)


def test_keep_form_prefix_and_master_filter():
    assert s.keep_form("8-K/A", ("8-K",)) and s.keep_form("SCHEDULE 13G/A", ("SCHEDULE 13",))
    assert not s.keep_form("4", ("8-K", "10-K")) and not s.keep_form("424B2", ("8-K",))
    assert s.keep_form("4", ())
    text = ("CIK|Company Name|Form Type|Date Filed|File Name\n-----\n"
            "1|A|4|20261021|edgar/data/1/0001-26-1.txt\n2|B|8-K|20261021|edgar/data/2/0002-26-1.txt\n")
    rows, names = s.parse_master(text, date(2026, 10, 21), ("8-K",))
    assert [r["form"] for r in rows] == ["8-K"] and names == {2: "B"}
    assert len(s.parse_master(text, date(2026, 10, 21))[0]) == 2


def _zip(rows):
    head = '"要素ID"\t"項目名"\t"コンテキストID"\t"相対年度"\t"連結・個別"\t"期間・時点"\t"ユニットID"\t"単位"\t"値"'
    body = "\n".join("\t".join(f'"{c}"' for c in r) for r in rows)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("XBRL_TO_CSV/x.csv", (head + "\n" + body).encode("utf-16"))
    return buf.getvalue()


def test_facts_lvh_only_filters_350_but_not_180():
    blob = _zip([["jplvh_cor:NameOfIssuer", "発行者", "FilingDateInstant", "", "", "", "", "", "A社"],
                 ["jplvh_cor:TelephoneNumber", "電話", "FilingDateInstant", "", "", "", "", "", "03"]])
    day = date(2026, 10, 21)
    assert len(g.facts_from_csv_zip(blob, {"docID": "d", "docTypeCode": "350"}, day, 100, True)[0]) == 1
    assert len(g.facts_from_csv_zip(blob, {"docID": "d", "docTypeCode": "350"}, day, 100, False)[0]) == 2
    assert len(g.facts_from_csv_zip(blob, {"docID": "d", "docTypeCode": "180"}, day, 100, True)[0]) == 2


class _Job:
    total_bytes_processed = 5 * 1024 ** 3
    total_bytes_billed = 0

    def result(self):
        return None


class _Client:
    project = "p"

    def __init__(self):
        self.queries = []

    def query(self, sql, job_config=None, location=None):
        self.queries.append(job_config.dry_run)
        return _Job()

    def get_table(self, ref):
        return SimpleNamespace(num_rows=1, num_bytes=1)


def test_run_refuses_when_estimate_exceeds_limit():
    c = _Client()
    r = bc.run(c, SimpleNamespace(BQ_COMPACT_MAX_BYTES=1024 ** 3), "price_dedupe")
    assert r["refused"] and not r["ran"] and c.queries == [True]   # 見積もりの1本だけ
