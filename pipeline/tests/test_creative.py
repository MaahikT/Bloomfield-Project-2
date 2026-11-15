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


def test_concession_creep_is_per_year_of_lease(c):
    # 3 free months on a 60-month lease (0.6/yr) -> 11 on an 83-month lease (1.59/yr): +0.99/yr
    k = cr.concession_creep(prop(), c["concession_creep"],
                            {"x": [(date(2021, 6, 1), 3.0, 60), (date(2022, 6, 1), 11.0, 83)]}, AS_OF)
    assert k.value == pytest.approx(11 / (83 / 12) - 3 / 5, abs=0.01)
    apt = cr.concession_creep(prop(), c["concession_creep"], {"x": [(date(2021, 6, 1), 0.0, None),
                                                                     (date(2022, 6, 1), 1.0, None)]}, AS_OF)
    assert apt.value == 1.0                       # 1-year lease: +1 free month = full strength


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


def multi_loan_prop(*loan_keys):
    return Property("p", projects=[{"project_id": "proj-x", "property_type": "retail", "state": "NY"}],
                    cmbs_loans=[{"loan_key": k} for k in loan_keys])


def test_occupancy_slide_never_compares_across_loans(c):
    # BMO 2023-5C1 Gateway Center South: controlling note 1A-1 reports 0.9793 while pari passu
    # companions 1A-3/1A-5 file a literal 0.0 for the SAME period - a non-report filler, not a slide.
    p = multi_loan_prop("edgar:BMO 2023-5C1 Mortgage Trust:1A-1",
                        "edgar:BMO 2023-5C1 Mortgage Trust:1A-3",
                        "edgar:BMO 2023-5C1 Mortgage Trust:1A-5")
    o = {"edgar:BMO 2023-5C1 Mortgage Trust:1A-1": [{"period_end": date(2022, 6, 30), "occupancy": 0.9793}],
         "edgar:BMO 2023-5C1 Mortgage Trust:1A-3": [{"period_end": date(2022, 6, 30), "occupancy": 0.0}],
         "edgar:BMO 2023-5C1 Mortgage Trust:1A-5": [{"period_end": date(2022, 6, 30), "occupancy": 0.0}]}
    q = cr.occupancy_slide(p, "Retail", "NY", c["occupancy_slide"], o, {}, AS_OF)
    assert not q.known or q.value == 0.0


def test_occupancy_slide_real_trend_survives_companion_zero_notes(c):
    # A genuine slide on the controlling note still fires when companion notes file zeros.
    p = multi_loan_prop("loan-1A-1", "loan-1A-3")
    o = {"loan-1A-1": [{"period_end": date(2021, 12, 31), "occupancy": 0.85},
                       {"period_end": date(2022, 6, 30), "occupancy": 0.75}],
         "loan-1A-3": [{"period_end": date(2021, 12, 31), "occupancy": 0.0},
                       {"period_end": date(2022, 6, 30), "occupancy": 0.0}]}
    q = cr.occupancy_slide(p, "Retail", "NY", c["occupancy_slide"], o, {}, AS_OF)
    assert q.known and q.value == pytest.approx(0.10 / 0.15, abs=0.01)


def test_occupancy_slide_treats_literal_zero_as_non_report(c):
    # 0.0 after real readings is the filers' filler, not proven vacancy: BMO 2024-5C8 loan 16
    # went 0.0 -> 1.0 in one month with payments current.
    o = {"proj-x": [{"period_end": date(2021, 12, 31), "occupancy": 0.8786},
                    {"period_end": date(2022, 6, 30), "occupancy": 0.0}]}
    q = cr.occupancy_slide(prop(), "Senior housing", "MI", c["occupancy_slide"], o, {}, AS_OF)
    assert not q.known or q.value == 0.0


def test_nursing_benchmark_never_serves_assisted_living(c):
    o = ops((date(2022, 1, 1), 12, 0.95, 0, 0, 0, 0), (date(2022, 7, 1), 12, 0.70, 0, 0, 0, 0))
    nursing_mkt = {("MI", "Senior housing (nursing)"): [(date(2022, 1, 1), 0.90, None), (date(2022, 7, 1), 0.88, None)]}
    assisted = Property("p", projects=[{"project_id": "x", "property_type": "Assisted living", "state": "MI"}])
    nursing = Property("p", projects=[{"project_id": "x", "property_type": "Skilled nursing facility", "state": "MI"}])
    a = cr.occupancy_slide(assisted, "Senior housing", "MI", c["occupancy_slide"], o, nursing_mkt, AS_OF)
    n = cr.occupancy_slide(nursing, "Senior housing", "MI", c["occupancy_slide"], o, nursing_mkt, AS_OF)
    assert "no market data" in a.why            # honest miss, not a nursing-facility comparison
    assert "nursing" in n.why and "no market data" not in n.why
