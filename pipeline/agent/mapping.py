"""
mapping.py - turn VERIFIED research evidence into scorer inputs, by deterministic rules only.

    agent (Part D research) -> evidence table -> THIS -> scorer input tables -> scorer/equation.py

The scorer's rules are unchanged. This module never decides what a fact MEANS for a score;
it only copies a fact into the exact input table the existing equation already reads, and
only when the evidence proves it:

  claim "asking_rent"       -> rent_observations   (signal R: rent decline)
                               value = rent number, notes: unit_basis, lease_term_months,
                               concession_months (optional)
  claim "market_rent_index" -> market_rents        (signal R: the city comparison)
                               value = index number, notes: city, state, period (YYYY-MM-DD)
  claim "occupancy_rate"   -> operating_metrics.occupancy (signal Q: occupancy slide)
  claim "operating_noi"    -> operating_metrics.noi       (signal V: income erosion)
                               NOI keeps its sign: a negative NOI is the distress fact itself.
  claim "operating_revenue" / "operating_expenses" -> operating_metrics (signal X: cost squeeze)
                               value = the number (occupancy 0-1 or a %); notes: period_end (YYYY-MM-DD),
                               period_months (12, 6 or 3, default 12). Same property+period rows merge.
                               Property-level rows key on the property itself when the property has
                               no linked project (CMBS-only properties); per-loan series stay separate.
  claim "market_occupancy" -> market_occupancy     (signal Q: the market comparison)
                               value = occupancy 0-1 or a %, notes: state, property_type, period (YYYY-MM-DD)
  claim "permit_inspection" -> permit_inspections  (signal I: inspection gap)
                               notes: permit_number, city, state, inspection_date (YYYY-MM-DD),
                               inspection_type, result

Rules that keep this honest:
  * The claim must be one of the types above, exactly. Everything else stays narrative.
  * Web evidence needs the saved page (file_id), its address (source_url) and the verbatim
    quote - and the reported VALUE must literally appear in that quote. A number the page
    does not say is not a fact.
  * Database evidence needs the SELECT it came from (db_query).
  * The property must resolve to real records (asking_rent: to one of the property's
    projects through Part C's property_links).
  * Never overwrite or duplicate: if a matching input row already exists (any source),
    the evidence is corroboration, not a new row.

Run:   python -m agent.mapping            (apply pending evidence, print what happened)
"""
from __future__ import annotations

import re
from datetime import date, datetime

from collectors.common.log import get_logger

log = get_logger("agent.mapping")

TYPES = ("asking_rent", "market_rent_index", "permit_inspection",
         "occupancy_rate", "operating_noi", "operating_revenue", "operating_expenses",
         "market_occupancy", "sponsor_distress_event", "physical_condition_event")

# Verified research events: distress facts a fetched page proves but no numeric input table holds.
# claim -> (research_events tag, rubric cap). The points are the researcher's rubric-band judgment
# (the recovered v2 rubric caps sponsor distress at 12 and physical condition at 8); the gates in
# apply() are what keep them honest - verbatim quote on the fetched page, cap range, currency.
EVENT_TAGS = {"sponsor_distress_event": ("sponsor_distress", 12.0),
              "physical_condition_event": ("physical_condition", 8.0)}

OPS_CLAIMS = {"occupancy_rate": "occupancy", "operating_noi": "noi",
              "operating_revenue": "revenue", "operating_expenses": "expenses"}


def _notes_dict(notes) -> dict:
    """notes is 'key=value; key=value' (the agent's save_evidence contract)."""
    out = {}
    for part in re.split(r"[;\n]", notes or ""):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip().lower()] = v.strip()
    return out


def _as_date(s):
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            d = datetime.strptime(str(s).strip(), fmt)
            return d.date()
        except ValueError:
            pass
    return None


def _number(s):
    """Parse '$1,850' / '1850.0' / '0.678' into a float, or None."""
    if s is None:
        return None
    t = re.sub(r"[$,\s]", "", str(s))
    try:
        return float(t)
    except ValueError:
        return None


UNIT_BASIS = {"unit", "bed", "sqft"}


def _plausible(claim, value, notes) -> str | None:
    """Context checks beyond 'the number is on the page': units, period, sane magnitude."""
    if claim == "asking_rent":
        basis = (notes.get("unit_basis") or "").lower()
        if basis not in UNIT_BASIS:
            return "asking_rent needs unit_basis (unit / bed / sqft) in notes - a rent without units is meaningless"
        lo, hi = (1, 500) if basis == "sqft" else (50, 100_000)
        if not (lo <= value <= hi):
            return f"asking_rent {value} per {basis} is outside the sane range {lo}-{hi}; double-check the page"
    if claim == "market_rent_index":
        if not (1 <= value <= 100_000):
            return f"market_rent_index {value} is outside the sane range 1-100000"
    return None


def _value_in_quote(value, quote) -> bool:
    """The reported number must literally appear in the verbatim quote ($ and commas ignored)."""
    if value is None or not quote:
        return False
    digits = re.sub(r"[^0-9.]", "", str(value))
    q = re.sub(r"[^0-9.]", "", quote)
    return bool(digits) and digits in q


def _verified(row) -> str | None:
    """Return why a row fails verification, or None when it is solid."""
    if row["source_kind"] == "web_page":
        if not (row["file_id"] and row["source_url"] and row["quote"]):
            return "web evidence needs the saved page, its URL and a verbatim quote"
    elif row["source_kind"] == "database":
        if not row["db_query"]:
            return "database evidence needs the SELECT it came from"
    else:
        return f"unknown source_kind {row['source_kind']!r}"
    return None


def _project_ids(con, subject) -> list[str]:
    """Which projects belong to the evidence's property (Part C's links)."""
    pid = str(subject or "").removeprefix("property_id=").strip()
    try:
        rows = con.execute(
            "SELECT record_id FROM property_links WHERE property_id = ? AND record_table = 'projects'",
            [pid]).fetchall()
    except Exception:
        return []
    return [r[0] for r in rows]


def _ops_link(con, subject) -> tuple[str | None, str | None]:
    """operating_metrics link_id for the evidence's property, or (None, reason).

    A property-level operating fact (NOI, revenue, expenses, occupancy) belongs to the
    property, not to a construction project: CMBS-only properties have no projects row,
    so resolving through projects alone made their verified financials unmappable.
    One linked project -> that project (original behavior). No projects -> the property
    id itself, which the scorer's link_ids always includes; _ops_by_loan then keeps this
    property-level series separate from per-loan series, never merging them (the same
    rule that keeps pari passu notes apart). Several projects -> ambiguous, skip.
    """
    pid = str(subject or "").removeprefix("property_id=").strip()
    projects = _project_ids(con, subject)
    if len(projects) == 1:
        return projects[0], None
    if len(projects) > 1:
        return None, f"property resolves to {len(projects)} projects, needs exactly 1"
    if not pid:
        return None, "evidence subject is not a property_id"
    return pid, None


def _table_has(con, table, where, params) -> bool:
    try:
        return bool(con.execute(f"SELECT 1 FROM {table} WHERE {where} LIMIT 1", params).fetchone())
    except Exception:
        return False


def apply(con, since: date | None = None) -> dict:
    """Map every verified, typed evidence row into the scorer's input tables.

    Returns {"mapped": n, "skipped": {reason: n}}. Idempotent: re-running adds nothing.
    """
    try:
        cols = [d[0] for d in con.execute(
            "SELECT evidence_id, subject, claim, value, source_kind, file_id, source_url, quote, "
            "db_query, observed_at, notes FROM evidence").description]
        rows = [dict(zip(cols, r)) for r in con.execute(
            "SELECT evidence_id, subject, claim, value, source_kind, file_id, source_url, quote, "
            "db_query, observed_at, notes FROM evidence").fetchall()]
    except Exception:
        return {"mapped": 0, "skipped": {"no evidence table": 1}}
    mapped, skipped = 0, {}
    for row in rows:
        claim = (row["claim"] or "").strip()
        if claim not in TYPES:
            continue                                  # narrative evidence: shown, never scored
        why = _verified(row)
        notes = _notes_dict(row["notes"])
        value = _number(row["value"])
        observed = row["observed_at"]
        observed = observed.date() if isinstance(observed, datetime) else (observed or date.today())
        if (why is None and row["source_kind"] == "web_page" and row["value"] is not None
                and claim not in EVENT_TAGS            # event points are a rubric judgment, not a page number
                and not _value_in_quote(row["value"], row["quote"])):
            why = "the reported value does not appear in the verbatim quote"
        if why is None and claim == "asking_rent":
            if value is None or value <= 0:
                why = "asking_rent needs a positive number"
            if not why:
                why = _plausible(claim, value, notes)
            projects = _project_ids(con, row["subject"])
            if not why and len(projects) != 1:
                why = f"property resolves to {len(projects)} projects, needs exactly 1"
            if not why:
                existing = con.execute(
                    "SELECT asking_rent, coalesce(unit_basis, '') FROM rent_observations "
                    "WHERE project_id = ? AND observed_at = ?", [projects[0], observed]).fetchall()
                basis = (notes.get("unit_basis") or "").lower()
                if any(abs(r - value) < 1e-9 for r, _ in existing):
                    skipped["already recorded"] = skipped.get("already recorded", 0) + 1
                    continue                    # same reading again: corroboration, not a new row
                if any(b in (basis, "") for _, b in existing):
                    skipped["existing reading kept"] = skipped.get("existing reading kept", 0) + 1
                    continue                    # same unit (or an old unlabeled row): never overwrite
            if not why:
                con.execute(
                    "INSERT INTO rent_observations (project_id, observed_at, asking_rent, "
                    "concession_months, lease_term_months, unit_basis, source_url, file_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [projects[0], observed, value, _number(notes.get("concession_months")),
                     _number(notes.get("lease_term_months")), notes.get("unit_basis"),
                     row["source_url"], row["file_id"]])
        elif why is None and claim == "market_rent_index":
            city, state, period = notes.get("city"), notes.get("state"), _as_date(notes.get("period"))
            if value is None or not (city and state and period):
                why = "market_rent_index needs a number plus city, state and period (YYYY-MM-DD) in notes"
            if not why:
                why = _plausible(claim, value, notes)
            if not why and _table_has(con, "market_rents", "city = ? AND state = ? AND period = ?",
                                      [city, state, period]):
                skipped["already recorded"] = skipped.get("already recorded", 0) + 1
                continue
            if not why:
                con.execute("INSERT INTO market_rents (city, state, period, rent_index, source) "
                            "VALUES (?, ?, ?, ?, ?)",
                            [city, state, period, value, row["source_url"] or row["db_query"]])
        elif why is None and claim in OPS_CLAIMS:
            field = OPS_CLAIMS[claim]
            period_end = _as_date(notes.get("period_end"))
            period_months = int(_number(notes.get("period_months")) or 12)
            if period_months not in (3, 6, 12):      # 6 = half-year actuals (e.g. CMBS H1/H2 reports)
                period_months = 12                   # unknown length: treat as a year, never annualize a guess
            if claim == "occupancy_rate":
                if value is not None and 1 < value <= 100:
                    value = value / 100.0           # a page says "87%"; the table stores 0-1
                if value is None or not (0 < value <= 1):
                    why = "occupancy_rate needs a value between 0 and 1 (or a percent 1-100)"
            elif claim == "operating_noi":
                # NOI can be zero or negative: a negative NOI is the distress fact itself.
                # The scorer (income_erosion) already guards the ratio (best year must be > 0).
                if value is None:
                    why = "operating_noi needs a number (keep the sign: negative NOI counts)"
            elif value is None or value <= 0:
                why = f"{claim} needs a positive number"
            if not why and not period_end:
                why = f"{claim} needs period_end (YYYY-MM-DD) in notes; period_months 12, 6 or 3 (default 12)"
            link, link_why = (None, None) if why else _ops_link(con, row["subject"])
            if not why and link is None:
                why = link_why
            if not why:
                existing = con.execute(
                    f"SELECT {field} FROM operating_metrics WHERE link_id = ? AND period_end = ? "
                    "AND period_months = ?", [link, period_end, period_months]).fetchone()
                if existing and existing[0] is not None:
                    if abs(existing[0] - value) < 1e-9:
                        skipped["already recorded"] = skipped.get("already recorded", 0) + 1
                    else:
                        skipped["existing reading kept"] = skipped.get("existing reading kept", 0) + 1
                    continue                    # never overwrite a reading, same or different
                if existing:
                    con.execute(
                        f"UPDATE operating_metrics SET {field} = ? WHERE link_id = ? AND period_end = ? "
                        "AND period_months = ?", [value, link, period_end, period_months])
                else:
                    con.execute(
                        "INSERT INTO operating_metrics (link_id, period_end, period_months, "
                        f"{field}, source, file_id, disclosed_on) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        [link, period_end, period_months, value,
                         row["source_url"] or "research", row["file_id"], observed])
        elif why is None and claim == "market_occupancy":
            state, ptype, period = notes.get("state"), notes.get("property_type"), _as_date(notes.get("period"))
            if value is not None and 1 < value <= 100:
                value = value / 100.0
            if value is None or not (0 < value <= 1) or not (state and ptype and period):
                why = ("market_occupancy needs a value between 0 and 1 (or a percent 1-100) plus "
                       "state, property_type and period (YYYY-MM-DD) in notes")
            if not why and _table_has(con, "market_occupancy",
                                      "state = ? AND property_type = ? AND period = ?",
                                      [state, ptype, period]):
                skipped["already recorded"] = skipped.get("already recorded", 0) + 1
                continue
            if not why:
                con.execute("INSERT INTO market_occupancy (state, property_type, period, occupancy, source) "
                            "VALUES (?, ?, ?, ?, ?)",
                            [state, ptype, period, value, row["source_url"] or row["db_query"]])
        elif why is None and claim == "permit_inspection":
            insp_date = _as_date(notes.get("inspection_date"))
            if not (notes.get("permit_number") and notes.get("city") and notes.get("state") and insp_date):
                why = ("permit_inspection needs permit_number, city, state and inspection_date "
                       "(YYYY-MM-DD) in notes")
            elif _table_has(con, "permit_inspections",
                            "permit_number = ? AND inspection_date = ? AND coalesce(inspection_type,'') = ?",
                            [notes["permit_number"], insp_date, notes.get("inspection_type", "")]):
                skipped["already recorded"] = skipped.get("already recorded", 0) + 1
                continue
            else:
                con.execute(
                    "INSERT INTO permit_inspections (permit_number, state, city, inspection_date, "
                    "inspection_type, result, file_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [notes["permit_number"], notes["state"], notes["city"], insp_date,
                     notes.get("inspection_type"), notes.get("result"), row["file_id"]])
        elif why is None and claim in EVENT_TAGS:
            tag, cap = EVENT_TAGS[claim]
            # Event points need the fetched page itself: a verbatim quote on a saved web page.
            # (_verified above already requires file_id + source_url + quote for web_page rows;
            # a database-only row cannot carry a verified event.)
            if row["source_kind"] != "web_page":
                why = f"{claim} needs web evidence (saved page + URL + verbatim quote)"
            pid = str(row["subject"] or "").removeprefix("property_id=").strip()
            if not why and not pid:
                why = f"{claim} needs a property_id subject"
            if not why and (value is None or not (1 <= value <= cap)):
                why = f"{claim} points must be a number 1-{cap:.0f} (the research rubric's cap)"
            pulled = _as_date(notes.get("pulled") or notes.get("as_of"))
            if not why and not pulled:
                why = f"{claim} needs pulled=YYYY-MM-DD in notes (when the source was verified)"
            if not why and claim == "physical_condition_event" and (notes.get("status") or "").lower() != "open":
                why = ("physical_condition_event needs status=open in notes - the rubric scores only "
                       "verified CURRENT conditions, not history")
            if not why and claim == "sponsor_distress_event" and (notes.get("identity") or "").lower() != "verified":
                value = min(value, 4.0)          # rubric: an unverified related-entity link caps at 4
            if not why:
                con.execute(
                    "CREATE TABLE IF NOT EXISTS research_events (property_id TEXT, tag TEXT, "
                    "points DOUBLE, event_date DATE, quote TEXT, source_url TEXT, file_id TEXT, "
                    "evidence_id TEXT, applied_at TIMESTAMP)")
                if _table_has(con, "research_events", "property_id = ? AND tag = ? AND source_url = ?",
                              [pid, tag, row["source_url"] or ""]):
                    skipped["already recorded"] = skipped.get("already recorded", 0) + 1
                    continue                    # the same verified event again: corroboration, not a new row
                con.execute(
                    "INSERT INTO research_events (property_id, tag, points, event_date, quote, "
                    "source_url, file_id, evidence_id, applied_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [pid, tag, min(value, cap), pulled, row["quote"], row["source_url"],
                     row["file_id"], row["evidence_id"], datetime.utcnow()])
        if why:
            skipped[why] = skipped.get(why, 0) + 1
            log.info("evidence %s not mapped: %s", row["evidence_id"], why)
        else:
            mapped += 1
    return {"mapped": mapped, "skipped": skipped}


def main() -> None:
    from scorer.schema import open_db
    con = open_db()
    print(apply(con))


if __name__ == "__main__":
    main()
