"""us_segments の純粋ロジック（四半期の並び・セグメント文字列の解釈・行の抽出と畳み込み）。"""
import io
from datetime import date

import us_segments as us


def test_quarters_back_covers_five_years_oldest_first():
    qs = us.quarters_back(date(2026, 10, 10))
    assert qs[0] == (2021, 4) and qs[-1] == (2026, 4)
    assert len(qs) == 21 and qs == sorted(qs)


def test_parse_segments_single_known_axis_only():
    assert us.parse_segments("ProductOrService=IPhoneMember;") == ("ProductOrService", "IPhoneMember")
    assert us.parse_segments("Geographical=AmericasSegmentMember;BusinessSegments=X;") is None
    assert us.parse_segments("StatementEquityComponents=RetainedEarningsMember;") is None
    assert us.parse_segments("") is None and us.parse_segments(None) is None


def _tsv(rows):
    head = list(rows[0])
    return "\t".join(head) + "\n" + "\n".join("\t".join(str(r[h]) for h in head) for r in rows) + "\n"


def test_extract_rows_filters_and_keeps_latest_filing():
    sub = _tsv([
        {"adsh": "A1", "cik": "320193", "form": "10-K", "filed": "20251031", "fy": "2025", "fp": "FY"},
        {"adsh": "A2", "cik": "320193", "form": "10-K", "filed": "20261030", "fy": "2026", "fp": "FY"},
        {"adsh": "B1", "cik": "999", "form": "10-K", "filed": "20251031", "fy": "2025", "fp": "FY"},
        {"adsh": "C1", "cik": "320193", "form": "8-K", "filed": "20251031", "fy": "2025", "fp": "FY"},
    ])
    base = {"coreg": "", "uom": "USD", "qtrs": "4", "ddate": "20250930", "tag": "Revenues", "value": "100"}
    num = _tsv([
        dict(base, adsh="A1", segments="ProductOrService=IPhoneMember;", value="100"),
        dict(base, adsh="A2", segments="ProductOrService=IPhoneMember;", value="101"),   # 翌年の比較列（新しい提出が勝つ）
        dict(base, adsh="A1", segments="", value="500"),                                   # 全社合計は入れない
        dict(base, adsh="A1", segments="ProductOrService=MacMember;", uom="EUR"),          # USD 以外
        dict(base, adsh="A1", segments="ProductOrService=MacMember;", qtrs="2"),           # 半期累計
        dict(base, adsh="A1", segments="ProductOrService=MacMember;", tag="Assets"),       # 対象外タグ
        dict(base, adsh="A1", segments="ProductOrService=MacMember;", ddate="20190930"),   # 5年より古い
        dict(base, adsh="B1", segments="ProductOrService=XMember;"),                       # 上場ユニバース外
        dict(base, adsh="C1", segments="ProductOrService=XMember;"),                       # 対象外の提出種別
        dict(base, adsh="A1", segments="BusinessSegments=WearablesMember;", tag="OperatingIncomeLoss", value="7"),
    ])
    rows = us.extract_rows(sub, io.StringIO(num), {320193}, "2021-10-01")
    got = {(r["member"], r["tag"]): r for r in rows}
    assert set(got) == {("IPhoneMember", "Revenues"), ("WearablesMember", "OperatingIncomeLoss")}
    iphone = got[("IPhoneMember", "Revenues")]
    assert iphone["val"] == 101 and iphone["accn"] == "A2" and iphone["filed"] == "2026-10-30"
    assert iphone["period_end"] == "2025-09-30" and iphone["qtrs"] == 4 and iphone["fy"] == 2026
