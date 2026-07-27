from base_agent import calc_composite_score


def test_real_estate_bonus_is_added_and_clipped_to_contract():
    companies = [{"fetch_ok": False}]
    base, _ = calc_composite_score(companies, [])
    plus, _ = calc_composite_score(companies, [], real_estate_bonus=2.5)
    clipped, _ = calc_composite_score(companies, [], real_estate_bonus=99)
    assert plus == base + 2.5
    assert clipped == base + 3.0
