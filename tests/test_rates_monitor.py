# 2026-10-05: 国債利回り（日米3/5/10年）と政策金利
from datetime import date

import rates_monitor as r


def test_wareki_and_mof_csv():
    assert r.parse_wareki("R8.10.1") == date(2026, 10, 1)
    txt = "国債金利情報,,\n基準日,1年,3年,5年,10年\nR8.10.1,1.6,2.077,2.407,3.092\nR8.10.2,1.6,2.065,2.397,3.097\nR8.10.2,1.6,2.065,2.397,3.097\n"
    rows = r.parse_mof_csv(txt)
    assert len(rows) == 2 and rows[-1][1]["10年"] == 3.097


def test_change_summary_and_last_change():
    s = [(date(2026, 9, 24), 3.0), (date(2026, 9, 30), 3.05), (date(2026, 10, 1), 3.1)]
    c = r.change_summary(s)
    assert c["d1"] == 0.05 and c["w1"] == 0.1 and c["w1_date"] == "2026-09-24"
    p = [(date(2026, 7, 1), 0.5), (date(2026, 8, 1), 0.75), (date(2026, 9, 1), 0.749)]
    assert r.last_change(p, 0.1) == {"date": "2026-08-01", "from": 0.5, "to": 0.75}
