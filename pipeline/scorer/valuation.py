"""
valuation.py - one valuation method for every property type (Part D).

    Stabilized NOI = best full-year revenue in the lookback - latest 12-month expenses     (Bloomfield's method)
    Value          = NOI / cap rate for the property type                                   (config: valuation.cap_rates)
    Stress cases   = run-rate expenses at cap + 1%;  today's NOI at cap + 2%
    Recovery       = value x (1 - closing) - legal per month x months + reserves held for lenders
    Multiple       = recovery / price;   IRR = multiple ** (12 / months) - 1
    Bid            = recovery / (1 + target IRR) ** (months / 12)
    LTV            = what's owed / value
"""
from __future__ import annotations

from .signals import as_date


def direct_cap(noi: float, cap_rate: float) -> float:
    return noi / cap_rate


def recovery(value: float, closing_pct: float = 0.03, months: int = 24, legal_per_month: float = 25_000,
             reserves: float = 0.0) -> float:
    return value * (1 - closing_pct) - legal_per_month * months + reserves


def irr(recovered: float, price: float, months: int = 24) -> float:
    return (recovered / price) ** (12 / months) - 1 if recovered > 0 else -1.0


def bid_for_irr(recovered: float, target_irr: float, months: int = 24) -> float:
    return recovered / (1 + target_irr) ** (months / 12)


def cap_rate(ptype: str | None, vcfg: dict) -> float:
    return vcfg["cap_rates"].get(ptype or "", vcfg["cap_rates"]["default"])


def _annual(row, field):
    v = row.get(field)
    return None if v is None else v * 12.0 / (row.get("period_months") or 12)


def noi_cases(rows: list[dict], vcfg: dict) -> dict:
    """Stabilized / run-rate / today's NOI from operating_metrics rows (any property type)."""
    rows = sorted([r for r in rows if as_date(r.get("period_end"))], key=lambda r: as_date(r["period_end"]))
    if not rows:
        return {}
    last_d = as_date(rows[-1]["period_end"])
    full = [r for r in rows if (r.get("period_months") or 12) >= 12
            and (last_d - as_date(r["period_end"])).days <= vcfg["lookback_months"] * 30.5]
    out = {}
    revs = [_annual(r, "revenue") for r in full if r.get("revenue") is not None]
    ttm = [r for r in full if r.get("expenses") is not None]
    if revs and ttm:
        out["stabilized"] = max(revs) - _annual(ttm[-1], "expenses")
        if rows[-1].get("expenses") is not None:
            out["run_rate"] = max(revs) - _annual(rows[-1], "expenses")
    noi_full = [r for r in full if r.get("noi") is not None]
    if noi_full:
        out["today"] = _annual(noi_full[-1], "noi")
    return out


def value_cases(rows, ptype, vcfg) -> list[dict]:
    cap = cap_rate(ptype, vcfg)
    n = noi_cases(rows, vcfg)
    cases = [("Stabilized (Bloomfield method)", n.get("stabilized"), cap),
             ("Run-rate expenses, cap +1%", n.get("run_rate"), cap + vcfg["run_rate_cap_add"]),
             ("Today's NOI, cap +2%", n.get("today"), cap + vcfg["downside_cap_add"])]
    return [{"case": name, "noi": noi, "cap_rate": c, "value": max(0.0, noi / c)} for name, noi, c in cases
            if noi is not None]


def scenario(name: str, value: float, upb: float, price: float, vcfg: dict, reserves: float = 0.0,
             units: int | None = None, noi=None, cap=None) -> dict:
    rec = recovery(value, vcfg["closing_pct"], vcfg["months_to_exit"], vcfg["legal_per_month"], reserves)
    bid = bid_for_irr(rec, vcfg["target_irr"], vcfg["months_to_exit"])
    return {"scenario": name, "noi": noi, "cap_rate": cap, "value": value,
            "value_per_unit": value / units if units else None, "ltv": upb / value if value else None,
            "recovery": rec, "multiple": rec / price, "irr": irr(rec, price, vcfg["months_to_exit"]),
            "bid_for_target": bid, "bid_pct_upb": bid / upb}
