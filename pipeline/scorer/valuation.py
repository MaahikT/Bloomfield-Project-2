"""
valuation.py - plain valuation math for a flagged deal (Part D), so the dashboard and case studies
can put a dollar value next to the Opportunity Score.

    value      = NOI / cap rate                                             (direct capitalization)
    recovery   = value x (1 - closing cost) - legal per month x months + reserves held for the lender
    multiple   = recovery / price            IRR = multiple ** (12 / months) - 1   (one exit cash flow)
    bid        = recovery / (1 + target IRR) ** (months / 12)                (price that earns the target)
"""
from __future__ import annotations


def direct_cap(noi: float, cap_rate: float) -> float:
    return noi / cap_rate


def recovery(value: float, closing_pct: float = 0.03, months: int = 24, legal_per_month: float = 25_000,
             reserves: float = 0.0) -> float:
    return value * (1 - closing_pct) - legal_per_month * months + reserves


def irr(recovered: float, price: float, months: int = 24) -> float:
    return (recovered / price) ** (12 / months) - 1


def bid_for_irr(recovered: float, target_irr: float, months: int = 24) -> float:
    return recovered / (1 + target_irr) ** (months / 12)


def scenario(name: str, noi: float | None, cap_rate: float | None, upb: float, price: float, *, closing_pct=0.03,
             months=24, legal_per_month=25_000, reserves=0.0, target_irr=0.15, units: int | None = None,
             value: float | None = None) -> dict:
    """Value from NOI / cap rate, or pass `value` directly (e.g. a sale-comps total)."""
    v = value if value is not None else direct_cap(noi, cap_rate)
    rec = recovery(v, closing_pct, months, legal_per_month, reserves)
    bid = bid_for_irr(rec, target_irr, months)
    return {
        "scenario": name, "noi": noi, "cap_rate": cap_rate, "value": v,
        "value_per_unit": v / units if units else None, "ltv_at_price": price / v, "recovery": rec,
        "multiple": rec / price, "irr": irr(rec, price, months), "bid_for_target": bid, "bid_pct_upb": bid / upb,
    }
