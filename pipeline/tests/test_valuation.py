"""Tests for the general valuation (scorer/valuation.py). Made-up numbers only."""
from datetime import date

import pytest

from scorer import equation as eq
from scorer import valuation as val


@pytest.fixture
def vcfg():
    return eq.load_equation_config()["valuation"]


def row(d, months, occ, rev, exp, noi=None):
    return {"period_end": d, "period_months": months, "occupancy": occ, "revenue": rev, "expenses": exp,
            "noi": noi if noi is not None else rev - exp}


def test_no_market_data_uses_best_year_revenue(vcfg):
    rows = [row(date(2019, 12, 31), 12, 0.90, 10_000_000, 7_000_000), row(date(2021, 12, 31), 12, 0.75, 8_500_000, 7_500_000)]
    st = val.stabilized(rows, "Senior housing", vcfg, capacity=100)
    assert st["revenue"] == pytest.approx(10_000_000) and st["noi"] == pytest.approx(2_500_000)
    assert st["cost_to_stabilize"] == 0                              # no lease-up cost for senior housing


def test_stabilize_to_market_and_subtract_lease_up_cost(vcfg):
    rows = [row(date(2023, 12, 31), 12, 0.70, 2_000_000, 1_000_000)]
    market = [(date(2024, 1, 1), 0.85, 20.0)]                         # 85% occupied, $20/SF/yr
    st = val.stabilized(rows, "Office", vcfg, capacity=100_000, market_series=market)
    assert st["revenue"] == pytest.approx(100_000 * 0.85 * 20.0)
    per_sf = 40 + 0.06 * 20.0 * 5
    assert st["cost_to_stabilize"] == pytest.approx(0.15 * 100_000 * per_sf)
    v = val.value_cases(rows, "Office", vcfg, 100_000, market)[0]
    assert v["value"] == pytest.approx(st["noi"] / 0.10 - st["cost_to_stabilize"])
