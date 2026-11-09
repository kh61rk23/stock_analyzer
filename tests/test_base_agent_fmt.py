"""base_agent の金額表示：赤字（負の値）でも兆円・億円に丸める。"""
import base_agent as ba


def test_fmt_cap_scales_negative_values_like_positive():
    assert ba.fmt_cap(3.2e12) == "3.2兆円"
    assert ba.fmt_cap(-4.9891e10) == "-499億円"
    assert ba.fmt_cap(3e8) == "3億円"
    assert ba.fmt_cap(5000) == "5000円"
    assert ba.fmt_cap(-5000) == "-5000円"
    assert ba.fmt_cap(None) == "N/A"
