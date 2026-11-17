"""Statement-basis classification for SEC Schedule AL 'most recent' operating statements.

Real evidence: BMO 2024-5C4 (CIK 2016125) filings - 24 of 39 loans file an already-annual NOI under
6-month statement dates (e.g. loan 23: NOI -3,912,185.16 / implied annual debt service 3,788,255.62
reproduces the filed coverage -1.0312 within 10%), one loan (35) files a real 3-month part-year, and
modified / no-coverage loans are not classifiable.
"""
from datetime import date

from collectors.edgar import classify_statement_basis
from scorer import creative as cr


def loan23():   # BMO 2024-5C4 loan 23 (Euclid), accession 0001888524-26-020366 - annual as filed
    return {"recent_noi": -3912185.16, "fin_start": date(2026, 1, 1), "fin_end": date(2026, 6, 30),
            "sec_noi": 6402152.0, "sec_coverage": 1.69, "recent_coverage": -1.0312,
            "io": True, "neg_am": False, "amort_term": None, "modified": False,
            "current_balance": 15e6, "original_balance": 15e6}


def test_annual_as_filed():
    basis, evidence = classify_statement_basis(loan23())
    assert basis == "annualized", evidence


def test_part_year():
    # loan 35 shape: 3-month period whose NOI x4 reproduces the filed coverage
    o = {"recent_noi": 78338.59, "fin_start": date(2026, 1, 1), "fin_end": date(2026, 3, 31),
         "sec_noi": 313354.0, "sec_coverage": 0.8715, "recent_coverage": 0.8715,
         "io": True, "neg_am": False, "amort_term": None, "modified": False,
         "current_balance": 1e6, "original_balance": 1e6}
    basis, evidence = classify_statement_basis(o)
    assert basis == "period_actual", evidence


def test_modified_loan_is_unknown():
    o = loan23() | {"modified": True}
    basis, evidence = classify_statement_basis(o)
    assert basis == "unknown" and "modified" in evidence


def test_amortizing_loan_is_unknown():
    o = loan23() | {"io": False, "amort_term": 360, "orig_term": 60, "io_term": 0}
    basis, _ = classify_statement_basis(o)
    assert basis == "unknown"


def test_full_term_interest_only_stays_eligible():
    o = loan23() | {"amort_term": 360, "orig_term": 60, "io_term": 60}
    basis, _ = classify_statement_basis(o)
    assert basis == "annualized"


def test_floating_rate_is_unknown():
    o = loan23() | {"rate_type": "2"}
    basis, _ = classify_statement_basis(o)
    assert basis == "unknown"


def test_paid_off_is_unknown():
    o = loan23() | {"current_balance": 0.0}
    basis, _ = classify_statement_basis(o)
    assert basis == "unknown"


def test_missing_coverage_is_unknown():
    o = loan23() | {"sec_coverage": None}
    basis, _ = classify_statement_basis(o)
    assert basis == "unknown"


def test_annual_by_dates():
    o = loan23() | {"fin_start": date(2025, 1, 1), "fin_end": date(2025, 12, 31)}
    basis, _ = classify_statement_basis(o)
    assert basis == "annualized"


def test_annual_helper_honors_basis():
    assert cr._annual({"noi": -3912185.16, "period_months": 6, "basis": "annualized"}, "noi") == -3912185.16
    assert cr._annual({"noi": 78338.59, "period_months": 3, "basis": "period_actual"}, "noi") == 78338.59 * 4
    assert cr._annual({"noi": 100.0, "period_months": 6, "basis": "unknown"}, "noi") is None
    assert cr._annual({"noi": 100.0, "period_months": 6}, "noi") == 200.0        # legacy rows keep scaling


def _row(loan_key, ops):
    return {"loan_key": loan_key, "file_id": "f1", "deal_name": "T", "report_period": date(2026, 10, 13),
            "occupancy": None, "origination_date": None, "value_securitization_date": None, "_ops": ops}


def _ops(rev, exp, noi, ds):
    return {"fin_start": date(2025, 1, 1), "fin_end": date(2025, 12, 31),
            "recent_revenue": rev, "recent_expenses": exp, "recent_noi": noi, "recent_ds": ds,
            "recent_coverage": 1.33, "io": True, "neg_am": False, "amort_term": 0.0}


def test_unallocated_collateral_financials_skipped():
    """Multi-property loans whose most-recent statement is loan-level only: collateral blocks
    carrying zeros, an exact copy of the loan-level row, or a non-reconciling partial mix are
    not per-property observations. Verified splits (blocks sum to the loan total) are kept,
    including legitimate zero-expense triple-net blocks."""
    import duckdb
    from collectors import edgar
    from collections import Counter
    con = duckdb.connect()
    result = edgar.ParseResult(found=Counter(), loans_seen=9, sample_tags=[], rows=[
        # Pattern B (Bala): block-1 duplicates the loan total, block-2 all zeros -> group skipped
        _row("edgar:T:1", _ops(100.0, 60.0, 40.0, 30.0)),
        _row("edgar:T:1-001", _ops(100.0, 60.0, 40.0, 30.0)),
        _row("edgar:T:1-002", _ops(0.0, 0.0, 0.0, 0.0)),
        # Partial/mixed allocation: no zero blocks, no duplicates, but the split does not
        # reconcile with the loan-level row (loan 2024-5C3:18 pattern) -> group skipped
        _row("edgar:T:2", _ops(100.0, 60.0, 40.0, 30.0)),
        _row("edgar:T:2-001", _ops(60.0, 50.0, 10.0, 20.0)),
        _row("edgar:T:2-002", _ops(50.0, 20.0, 30.0, 15.0)),
        # Real split: blocks sum to the loan total exactly -> kept
        _row("edgar:T:3", _ops(1000.0, 570.0, 430.0, 300.0)),
        _row("edgar:T:3-001", _ops(600.0, 340.0, 260.0, 180.0)),
        _row("edgar:T:3-002", _ops(400.0, 230.0, 170.0, 120.0)),
        # Real split, triple-net: expenses legitimately 0, revenue = NOI, sums reconcile -> kept
        _row("edgar:T:4", _ops(300.0, 0.0, 300.0, 150.0)),
        _row("edgar:T:4-001", _ops(200.0, 0.0, 200.0, 100.0)),
        _row("edgar:T:4-002", _ops(100.0, 0.0, 100.0, 50.0)),
    ])
    edgar.write_operating_metrics(con, result)
    rows = con.execute("SELECT link_id, noi FROM operating_metrics WHERE source = 'sec_edgar_financials'").fetchall()
    got = {k: v for k, v in rows}
    assert got == {"edgar:T:1": 40.0, "edgar:T:2": 40.0, "edgar:T:3": 430.0,
                   "edgar:T:3-001": 260.0, "edgar:T:3-002": 170.0,
                   "edgar:T:4": 300.0, "edgar:T:4-001": 200.0, "edgar:T:4-002": 100.0}
