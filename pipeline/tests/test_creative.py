"""Tests for the creative signals (scorer/creative.py): each formula, and how they combine."""
from datetime import date

import pytest

from scorer import creative as cr
from scorer import equation as eq
from scorer.group import Property

AS_OF = date(2022, 8, 1)


@pytest.fixture
def c():
    return eq.load_equation_config()["creative"]


def prop():
    return Property("p", projects=[{"project_id": "x", "property_type": "senior housing", "state": "MI"}])


def ops(*rows):
    return {"x": [dict(zip(("period_end", "period_months", "occupancy", "revenue", "expenses", "noi", "debt_service"), r))
                  for r in rows]}


def test_occupancy_slide_is_measured_against_the_market(c):
    o = ops((date(2019, 12, 31), 12, 0.85, None, None, None, None), (date(2022, 3, 31), 12, 0.72, None, None, None, None))
    alone = cr.occupancy_slide(prop(), "Senior housing", "MI", c["occupancy_slide"], o, {}, AS_OF)
    market = {("US", "Senior housing"): [(date(2019, 12, 31), 0.88), (date(2022, 3, 31), 0.80)]}
    vs_mkt = cr.occupancy_slide(prop(), "Senior housing", "MI", c["occupancy_slide"], o, market, AS_OF)
    assert alone.value == pytest.approx(0.13 / 0.15, abs=0.01) and alone.q == pytest.approx(0.9 * 0.7)
    assert vs_mkt.value == pytest.approx((0.13 - 0.08) / 0.15, abs=0.01) and vs_mkt.q == 0.9


def test_income_erosion_uses_noi_only_no_dscr(c):
    o = ops((date(2019, 12, 31), 12, None, None, None, 4_000_000, 3_000_000),
            (date(2022, 3, 31), 12, None, None, None, 3_000_000, 3_000_000))
    v = cr.income_erosion(prop(), c["income_erosion"], o, AS_OF)
    assert v.value == pytest.approx(0.25 / 0.5) and "DSCR" not in v.why   # debt service is ignored


def test_cost_squeeze(c):
    o = ops((date(2019, 12, 31), 12, None, 20_000_000, 16_000_000, None, None),
            (date(2022, 3, 31), 3, None, 18_000_000 / 4, 17_600_000 / 4, None, None))
    x = cr.cost_squeeze(prop(), c["cost_squeeze"], o, AS_OF)
    assert x.value == 1.0 and "expenses +10.0%" in x.why


def test_concession_creep(c):
    k = cr.concession_creep(prop(), c["concession_creep"],
                            {"x": [(date(2021, 6, 1), 0.0), (date(2022, 6, 1), 1.0)]}, AS_OF)
    assert k.value == pytest.approx(0.5)


def test_licensing_quality_from_cms_stars_and_abuse_flag(c):
    p = Property("p", projects=[{"project_id": "x", "property_address": "100 FAKE Sample Ave", "state": "MI"}])
    lic = {"100 fake|MI": [{"facility_name": "SNF", "status": "CMS overall 1-star",
                                  "distress_reason": "abuse icon", "distress_flag": True}]}
    s = cr.licensing_quality(p, c["licensing_quality"], lic, AS_OF)
    assert s.value == 1.0 and s.q == 0.6


def test_noisy_or_combination_and_confirmation_cap(c):
    sigs = [cr.Sig("Q", 0.5, True, "", 0.9), cr.Sig("V", 1.0, True, "", 1.0), cr.Sig("X", 1.0, True, "", 0.6)]
    value, showing, _ = cr.combine(sigs, c)
    assert value == pytest.approx(1 - (1 - 0.45) * 0 * (1 - 0.6))
    assert showing == 3 and c["max_confirmations"] == 2
