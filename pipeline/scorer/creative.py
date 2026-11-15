"""
creative.py - the CREATIVE signals: measurable early warnings that are not the loan clock or the opening date.

Every signal is a strength s in [0, 1] with an exact formula, a data source, and a reliability q in [0, 1]
(how much we trust that source). They combine into one Creative term:

    C = 1 - PRODUCT over j of (1 - q_j * s_j)          ("noisy-or": several weak signals add up, never past 1)

and C counts in the equation with weight 30:  Early = 50 L + 20 O + 30 C  (then anti-fluke / standout).

    Signal                     Formula for s                                            Source (part)
    R  rent vs. city           (city rent change - property rent change) / 15 pts       B rent_observations + A market_rents
    I  site inactivity         ramp(days since last city inspection, 60 -> 180)        A permit_inspections
    Q  occupancy slide         (peak occ. in last 36 mo - occ. now - market drop) / 15 pts   B/A operating_metrics + market_occupancy
    V  income erosion          (best-year NOI - NOI now) / best-year NOI / 50%  (no DSCR) A operating_metrics (public disclosures)
    X  cost squeeze            (expense growth - revenue growth since peak NOI) / 15 pts    A operating_metrics
    K  concession creep        (free months per lease now - a year ago) / 2 months      B rent_observations.concession_months
    Lq licensing / care        CMS 1-star 0.8, 2-star 0.5; abuse flag +0.2; state distress 0.8   A licensing_records

All thresholds live in config/equation.yaml under `creative:`.
"""
from __future__ import annotations

import re
from datetime import date

from .signals import as_date

CREATIVE_LABELS = {
    "R": "Rent vs. city", "I": "Site inactivity", "Q": "Occupancy slide", "V": "Income erosion",
    "X": "Cost squeeze", "K": "Concession creep", "Lq": "Licensing / care quality",
}

INPUT_SCHEMA = """
-- Operating results from public disclosures (EMMA continuing disclosures, state cost reports, leasing pages).
-- link_id = any id the property's records carry (project_id, loan_key, file_id, record_id).
CREATE TABLE IF NOT EXISTS operating_metrics (
    link_id        TEXT,
    period_end     DATE,
    period_months  INTEGER,      -- 12 = a year / trailing twelve months; 3 = a quarter (annualized x4)
    occupancy      DOUBLE,       -- 0-1
    revenue        DOUBLE,
    expenses       DOUBLE,
    noi            DOUBLE,
    debt_service   DOUBLE,       -- kept for reference only: NOT scored (DSCR needs Trepp, which we don't have)
    source         TEXT,
    file_id        TEXT,
    disclosed_on   DATE          -- when it became public (e.g. EMMA posting date); NULL = period_end
);
-- Market occupancy for the same kind of property (e.g. NIC MAP, CMS state averages). state 'US' = national.
CREATE TABLE IF NOT EXISTS market_occupancy (
    state          TEXT,
    property_type  TEXT,
    period         DATE,
    occupancy      DOUBLE,
    source         TEXT
);
"""


def clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


class Sig:
    def __init__(self, code, value=0.0, known=False, why="no data", q=1.0, counter=None):
        self.code, self.value, self.known, self.why, self.q, self.counter = code, value, known, why, q, counter

    @property
    def effective(self):
        return self.q * self.value


def link_ids(p) -> set[str]:
    ids = set()
    for r in p.projects:
        ids.add(str(r.get("project_id")))
    for r in p.cmbs_loans:
        ids.add(str(r.get("loan_key")))
    for r in p.filings:
        ids.add(str(r.get("file_id")))
    for r in p.recorded_loans:
        ids.add(str(r.get("record_id")))
    ids.add(str(p.property_id))
    return ids - {"None"}


def _annual(row, field):
    v = row.get(field)
    if v is None:
        return None
    months = row.get("period_months") or 12
    return v * 12.0 / months


def _ops(p, ops: dict, as_of: date) -> list[dict]:
    rows = []
    for i in link_ids(p):
        rows += ops.get(i, [])
    rows = [r for r in rows if as_date(r.get("period_end"))
            and (as_date(r.get("disclosed_on")) or as_date(r["period_end"])) <= as_of]
    return sorted(rows, key=lambda r: as_date(r["period_end"]))


# ---------------------------------------------------------------------------
# Q: occupancy slide, measured against the market
# ---------------------------------------------------------------------------
def occupancy_slide(p, ptype, state, c, ops, mkt_occ, as_of) -> Sig:
    rows = [r for r in _ops(p, ops, as_of) if r.get("occupancy") is not None]
    if len(rows) < 2:
        return Sig("Q")
    now = rows[-1]
    now_d = as_date(now["period_end"])
    window = [r for r in rows if (now_d - as_date(r["period_end"])).days <= c["lookback_months"] * 30.5]
    peak = max(window, key=lambda r: r["occupancy"])
    drop = peak["occupancy"] - now["occupancy"]
    series = mkt_occ.get(((state or "").upper(), ptype)) or mkt_occ.get(("US", ptype))
    q = c["reliability"]
    if series:
        m_then = min(series, key=lambda t: abs((t[0] - as_date(peak["period_end"])).days))[1]
        m_now = min(series, key=lambda t: abs((t[0] - now_d).days))[1]
        mkt_drop = m_then - m_now
        why = (f"occupancy {peak['occupancy']:.1%} -> {now['occupancy']:.1%} ({drop * 100:.1f} pts) vs. market "
               f"{m_then:.1%} -> {m_now:.1%} ({mkt_drop * 100:.1f} pts)")
    else:
        mkt_drop = 0.0
        q *= c["no_market_penalty"]
        why = f"occupancy {peak['occupancy']:.1%} -> {now['occupancy']:.1%} ({drop * 100:.1f} pts); no market data (trusted less)"
    gap = drop - mkt_drop
    counter = (f"occupancy up {-drop * 100:.1f} pts" if -drop >= c["counter_rise"] else None)
    return Sig("Q", clamp(gap / c["full_gap"]), True, why, q, counter)


# ---------------------------------------------------------------------------
# V: income erosion (NOI now vs. its best year in the lookback) - no DSCR: that needs Trepp
# ---------------------------------------------------------------------------
def income_erosion(p, c, ops, as_of) -> Sig:
    rows = [r for r in _ops(p, ops, as_of) if r.get("noi") is not None]
    if not rows:
        return Sig("V")
    now = rows[-1]
    now_d = as_date(now["period_end"])
    noi_now = _annual(now, "noi")
    annual = [_annual(r, "noi") for r in rows if (r.get("period_months") or 12) >= 12
              and (now_d - as_date(r["period_end"])).days <= c["lookback_months"] * 30.5]
    peak = max(annual) if annual else None
    if not peak or peak <= 0:
        return Sig("V")
    erosion = (peak - noi_now) / peak
    return Sig("V", clamp(erosion / c["full_noi_drop"]), True,
               f"NOI ${noi_now / 1e6:.2f}M/yr vs. best year ${peak / 1e6:.2f}M ({-erosion:+.0%})", c["reliability"])


# ---------------------------------------------------------------------------
# X: cost squeeze (expenses growing faster than revenue since the best year)
# ---------------------------------------------------------------------------
def cost_squeeze(p, c, ops, as_of) -> Sig:
    rows = [r for r in _ops(p, ops, as_of) if r.get("revenue") and r.get("expenses")]
    if len(rows) < 2:
        return Sig("X")
    full_years = [r for r in rows if (r.get("period_months") or 12) >= 12] or rows
    base = max(full_years, key=lambda r: _annual(r, "revenue") - _annual(r, "expenses"))
    now = rows[-1]
    if base is now:
        return Sig("X", 0.0, True, "best year is the latest year", c["reliability"])
    rev_g = _annual(now, "revenue") / _annual(base, "revenue") - 1
    exp_g = _annual(now, "expenses") / _annual(base, "expenses") - 1
    gap = exp_g - rev_g
    return Sig("X", clamp(gap / c["full_gap"]), True,
               f"since {as_date(base['period_end']):%Y}: expenses {exp_g:+.1%}, revenue {rev_g:+.1%}", c["reliability"])


# ---------------------------------------------------------------------------
# K: concession creep (free rent per lease, from leasing pages)
# ---------------------------------------------------------------------------
def concession_creep(p, c, concessions, as_of) -> Sig:
    obs = []
    for pr in p.projects:
        obs += concessions.get(str(pr.get("project_id")), [])
    obs = sorted((d, m) for d, m in obs if d and d <= as_of and m is not None)
    if len(obs) < 2:
        return Sig("K")
    now_d, now_m = obs[-1]
    prior = [m for d, m in obs if (now_d - d).days >= c["min_days_between"]]
    if not prior:
        return Sig("K")
    then_m = prior[-1]
    return Sig("K", clamp((now_m - then_m) / c["full_months"]), True,
               f"free rent {then_m:.1f} -> {now_m:.1f} months per lease", c["reliability"])


# ---------------------------------------------------------------------------
# Lq: licensing / care quality (CMS stars, abuse flag, state license distress)
# ---------------------------------------------------------------------------
_STARS = re.compile(r"(\d)\s*-?\s*star", re.I)


def licensing_quality(p, c, licenses, as_of) -> Sig:
    rows = []
    for key in _license_keys(p):
        rows += licenses.get(key, [])
    rows = [r for r in rows if not as_date(r.get("as_of_date")) or as_date(r["as_of_date"]) <= as_of]
    if not rows:
        return Sig("Lq")
    best = Sig("Lq", 0.0, True, "licensed, no quality flags", c["reliability"])
    for r in rows:
        text = f"{r.get('status') or ''} {r.get('distress_reason') or ''}"
        s, bits = 0.0, []
        m = _STARS.search(text)
        if m:
            stars = int(m.group(1))
            s = c["star_strength"].get(stars, c["star_strength"].get(str(stars), 0.0))
            bits.append(f"CMS {stars}-star")
        if r.get("distress_flag"):
            s = max(s, c["state_distress"])
            bits.append(r.get("distress_reason") or "state license distress")
        if "abuse" in text.lower():
            s += c["abuse_bonus"]
            bits.append("abuse flag")
        if s > best.value:
            best = Sig("Lq", clamp(s), True, f"{r.get('facility_name') or 'facility'}: " + ", ".join(bits), c["reliability"])
    return best


def _license_keys(p) -> set[str]:
    keys = set()
    for r in p.projects + p.filings + p.cmbs_loans + p.recorded_loans:
        a = (r.get("property_address") or "").lower()
        num = re.match(r"\s*(\d+)\s+(\w+)", a)
        st = (r.get("state") or r.get("property_state") or "").upper()
        if num:
            keys.add(f"{num.group(1)} {num.group(2)}|{st}")
    return keys


def license_key(address, state) -> str | None:
    num = re.match(r"\s*(\d+)\s+(\w+)", (address or "").lower())
    return f"{num.group(1)} {num.group(2)}|{(state or '').upper()}" if num else None


# ---------------------------------------------------------------------------
# Combine
# ---------------------------------------------------------------------------
def combine(sigs: list[Sig], cfg: dict) -> tuple[float, int, str]:
    """C = 1 - prod(1 - q*s). Returns (C, how many creative signals 'show', explanation)."""
    prod = 1.0
    for s in sigs:
        prod *= 1 - s.effective
    value = 1 - prod
    showing = sum(1 for s in sigs if s.effective >= cfg["shows_at"])
    on = [f"{s.code} {s.effective:.2f}" for s in sigs if s.effective > 0]
    why = ("creative signals: " + ", ".join(on)) if on else "no creative signals"
    return value, showing, why
