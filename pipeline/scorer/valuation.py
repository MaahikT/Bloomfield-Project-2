"""
valuation.py - one valuation method for every property type (Part D).

    Stabilized occupancy = max(own best occupancy in the lookback, market occupancy)
    Rent                 = market rent per unit of space if known, else best-year revenue per occupied unit
    Stabilized revenue   = space x stabilized occupancy x rent      (falls back to best-year revenue if space unknown)
    Stabilized NOI       = stabilized revenue - latest 12-month expenses                      (Bloomfield's method)
    Cost to stabilize    = max(0, stabilized occ. - occupancy now) x space x (build-out per unit
                           + commission % x rent x lease years)      (config: valuation.stabilization, by type)
    Value                = NOI / cap rate for the property type - cost to stabilize
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


def _market_now(series, as_of=None):
    """Latest market (occupancy, rent per unit-year) from [(date, occ, rent), ...]."""
    if not series:
        return None, None
    pts = sorted(series, key=lambda t: t[0])
    occ = next((t[1] for t in reversed(pts) if t[1] is not None), None)
    rent = next((t[2] for t in reversed(pts) if len(t) > 2 and t[2] is not None), None)
    return occ, rent


def stabilized(rows: list[dict], ptype, vcfg: dict, capacity=None, market_series=None) -> dict:
    """Stabilized revenue / NOI / cost to stabilize from operating_metrics rows (any property type)."""
    rows = sorted([r for r in rows if as_date(r.get("period_end"))], key=lambda r: as_date(r["period_end"]))
    if not rows:
        return {}
    last_d = as_date(rows[-1]["period_end"])
    full = [r for r in rows if (r.get("period_months") or 12) >= 12
            and (last_d - as_date(r["period_end"])).days <= vcfg["lookback_months"] * 30.5]
    with_rev = [r for r in full if r.get("revenue") is not None]
    ttm = [r for r in full if r.get("expenses") is not None]
    if not with_rev or not ttm:
        return {}
    best = max(with_rev, key=lambda r: _annual(r, "revenue"))
    mkt_occ, mkt_rent = _market_now(market_series)
    own_occs = [r["occupancy"] for r in full if r.get("occupancy") is not None]
    out = {"expenses": _annual(ttm[-1], "expenses")}
    occ_now = next((r["occupancy"] for r in reversed(rows) if r.get("occupancy") is not None), None)
    if capacity and (own_occs or mkt_occ):
        stab_occ = max([o for o in (max(own_occs) if own_occs else None, mkt_occ) if o is not None])
        if mkt_rent:
            rent, rent_from = mkt_rent, "market rent"
        elif best.get("occupancy"):
            rent, rent_from = _annual(best, "revenue") / (capacity * best["occupancy"]), "best-year revenue per occupied unit"
        else:
            rent = None
        if rent:
            out.update(revenue=capacity * stab_occ * rent, stab_occ=stab_occ, rent=rent, rent_from=rent_from)
            s = vcfg["stabilization"].get(ptype or "", vcfg["stabilization"]["default"])
            gap = max(0.0, stab_occ - occ_now) if occ_now is not None else 0.0
            per_unit = s["build_out_per_unit"] + s["commission_pct"] * rent * s["lease_years"]
            out["cost_to_stabilize"] = gap * capacity * per_unit
            out["lease_up_units"] = gap * capacity
    if "revenue" not in out:
        out.update(revenue=_annual(best, "revenue"), rent_from="best-year revenue")
    out.setdefault("cost_to_stabilize", 0.0)
    out["noi"] = out["revenue"] - out["expenses"]
    if rows[-1].get("expenses") is not None:
        out["run_rate_noi"] = out["revenue"] - _annual(rows[-1], "expenses")
    noi_full = [r for r in full if r.get("noi") is not None]
    if noi_full:
        out["today_noi"] = _annual(noi_full[-1], "noi")
    return out


def noi_cases(rows: list[dict], vcfg: dict, ptype=None, capacity=None, market_series=None) -> dict:
    st = stabilized(rows, ptype, vcfg, capacity, market_series)
    return {k: v for k, v in (("stabilized", st.get("noi")), ("run_rate", st.get("run_rate_noi")),
                              ("today", st.get("today_noi"))) if v is not None}


def value_cases(rows, ptype, vcfg, capacity=None, market_series=None) -> list[dict]:
    cap = cap_rate(ptype, vcfg)
    st = stabilized(rows, ptype, vcfg, capacity, market_series)
    if not st:
        return []
    cost = st.get("cost_to_stabilize", 0.0)
    cases = [("Stabilized (Bloomfield method)", st.get("noi"), cap, cost),
             ("Run-rate expenses, cap +1%", st.get("run_rate_noi"), cap + vcfg["run_rate_cap_add"], cost),
             ("Today's NOI, cap +2% (as-is)", st.get("today_noi"), cap + vcfg["downside_cap_add"], 0.0)]
    return [{"case": name, "noi": noi, "cap_rate": c, "cost_to_stabilize": k, "value": max(0.0, noi / c - k),
             "detail": st} for name, noi, c, k in cases if noi is not None]


def property_inputs(p, ptype, state, extra) -> tuple:
    """Space (units / SF / beds) and the market series for a grouped property."""
    capacity = sum((pr.get("units_or_beds") or 0) for pr in p.projects) or None
    series = (extra.get("mkt_occ", {}).get(((state or "").upper(), ptype))
              or extra.get("mkt_occ", {}).get(("US", ptype)))
    return capacity, series


def scenario(name: str, value: float, upb: float, price: float, vcfg: dict, reserves: float = 0.0,
             units: int | None = None, noi=None, cap=None) -> dict:
    rec = recovery(value, vcfg["closing_pct"], vcfg["months_to_exit"], vcfg["legal_per_month"], reserves)
    bid = bid_for_irr(rec, vcfg["target_irr"], vcfg["months_to_exit"])
    return {"scenario": name, "noi": noi, "cap_rate": cap, "value": value,
            "value_per_unit": value / units if units else None, "ltv": upb / value if value else None,
            "recovery": rec, "multiple": rec / price, "irr": irr(rec, price, vcfg["months_to_exit"]),
            "bid_for_target": bid, "bid_pct_upb": bid / upb}
