"""Tests for the unreported-collateral occupancy guard in the EDGAR collector.
Companion to test_edgar.py (same con fixture from tests/conftest.py); added with the guard itself.
"""
from collections import Counter
from datetime import date
from collectors import edgar
# --- Unreported collateral occupancy guard (false occupancy-collapse suppression) -----------
def _loan_row(key, period, occupancy, fin=None):
    o = {"sec_occupancy": None, "sec_revenue": None, "sec_expenses": None, "sec_noi": None,
         "recent_revenue": None, "recent_expenses": None, "recent_noi": None, "recent_ds": None,
         "fin_start": None, "fin_end": None}
    if fin:
        o.update(fin)
    return {"loan_key": key, "source": "sec_edgar", "deal_name": "T", "property_name": key,
            "property_address": "1 Main St", "property_city": "X", "property_state": "NY",
            "property_zip": None, "property_type": "multifamily", "current_balance": 1e6,
            "original_balance": 1e6, "maturity_date": None, "dscr": None, "occupancy": occupancy,
            "payment_status": None, "servicer_name": None, "watchlist_flag": None,
            "servicer_commentary": None, "report_period": period, "file_id": "f1",
            "collected_at": None, "origination_date": None, "modified_indicator": None,
            "post_mod_maturity": None, "value_securitization": None,
            "value_securitization_date": None, "value_recent": None, "value_recent_date": None,
            "_ops": o}
def _write_rows(con, rows):
    from collections import Counter
    res = edgar.ParseResult(rows=rows, found=Counter(), loans_seen=len(rows), sample_tags=[])
    suppressed = edgar._suppress_unreported_child_occupancy(res.rows)
    edgar.write_loans(con, res)
    edgar.write_operating_metrics(con, res)
    return suppressed
def test_child_occupancy_zero_suppressed_when_parent_nonzero_same_period(con):
    p = date(2026, 10, 13)
    suppressed = _write_rows(con, [_loan_row("edgar:T:27", p, 0.97),
                                   _loan_row("edgar:T:27-001", p, 0.0),
                                   _loan_row("edgar:T:27-002", p, 0.0)])
    assert set(suppressed) == {"edgar:T:27-001", "edgar:T:27-002"}
    assert con.execute("SELECT occupancy FROM loans WHERE loan_key='edgar:T:27-001'").fetchone()[0] is None
    assert con.execute("SELECT occupancy FROM loans WHERE loan_key='edgar:T:27'").fetchone()[0] == 0.97
    assert con.execute("SELECT count(*) FROM operating_metrics WHERE link_id='edgar:T:27-001'").fetchone()[0] == 0
def test_child_occupancy_zero_kept_with_allocated_financials(con):
    p = date(2026, 10, 13)
    suppressed = _write_rows(con, [_loan_row("edgar:T:27", p, 0.97),
                                   _loan_row("edgar:T:27-001", p, 0.0,
                                             fin={"recent_revenue": 120000.0, "recent_noi": 80000.0})])
    assert suppressed == []
    assert con.execute("SELECT occupancy FROM loans WHERE loan_key='edgar:T:27-001'").fetchone()[0] == 0.0
def test_child_occupancy_zero_kept_when_parent_zero_or_absent(con):
    p = date(2026, 10, 13)
    suppressed = _write_rows(con, [_loan_row("edgar:T:23", p, 0.0),
                                   _loan_row("edgar:T:23-001", p, 0.0),
                                   _loan_row("edgar:T:9-001", p, 0.0)])    # parent 9 not in this file
    assert suppressed == []
    for k in ("edgar:T:23", "edgar:T:23-001", "edgar:T:9-001"):
        assert con.execute("SELECT occupancy FROM loans WHERE loan_key=?", [k]).fetchone()[0] == 0.0
def test_child_nonzero_occupancy_always_kept(con):
    p = date(2026, 10, 13)
    suppressed = _write_rows(con, [_loan_row("edgar:T:27", p, 0.97),
                                   _loan_row("edgar:T:27-001", p, 0.82)])
    assert suppressed == []
    assert con.execute("SELECT occupancy FROM loans WHERE loan_key='edgar:T:27-001'").fetchone()[0] == 0.82
