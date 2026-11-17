"""
edgar.py - downloads CMBS loan-level data from SEC EDGAR and writes it to the loans table.

In plain English: every month, each commercial-mortgage bond trust (CMBS) must file an "ABS-EE" form
with the SEC. Attached is a data file (exhibit EX-102, in XML) listing every loan in the trust with
its balance, maturity date, DSCR, occupancy, payment status and property. This collector
  1. reads the SEC's quarterly index of filings and picks the CMBS trusts (by name),
  2. downloads each filing's EX-102 file (and saves it unchanged in data/raw/ for evidence),
  3. writes one row per loan to `loans` (source "sec_edgar", loan_key "edgar:<trust>:<asset number>").
     The loans table keeps only each loan's MOST RECENT report; every downloaded XML stays in raw_files.

SEC FAIR ACCESS: every request carries our User-Agent with the contact email from .env, and sec.gov is
limited to 4 requests per second (the SEC's cap is 10). All of that is handled by collectors/common/fetch.py.

THE XML TAG NAMES ARE UNVERIFIED (see config/edgar.yaml). After each file the collector prints a
"tag audit" listing any field it found no value for, so a wrong tag name is easy to spot and fix.

Run from the pipeline/ folder:
    python -m collectors.edgar                                   # last 45 days, at most 20 trusts
    python -m collectors.edgar --since 2026-10-01 --max-trusts 5
    python -m collectors.edgar --cik 1234567                     # one specific trust

Manual fallback: save an EX-102 XML by hand into data/inbox/sec_edgar/ with a sidecar
<file>.json like {"notes": "deal=BANK 2026-5YR1"}; the next run parses it.

(Adapted from Neel's edgar.py: the filing selection and XML parsing are his.)
"""
from __future__ import annotations

import argparse
import re
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterator

from collectors.common import config, db, fetch
from collectors.common.log import RunTracker, get_logger, setup_logging
from collectors.common.parse import clean_str, load_yaml, to_date, to_float

COLLECTOR = "edgar.py"
CONFIG_PATH = config.CONFIG_DIR / "edgar.yaml"
log = get_logger("edgar")

_IDX_LINE = re.compile(
    r"^(?P<form>\S+(?: \S+)*?)\s{2,}(?P<name>.+?)\s{2,}(?P<cik>\d+)\s+(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<file>\S+)\s*$")


@dataclass
class Filing:
    form: str
    company: str
    cik: str
    filed: str            # YYYY-MM-DD
    accession: str

    @property
    def accession_nodash(self) -> str:
        return self.accession.replace("-", "")


# ---------------------------------------------------------------------------
# Finding the filings
# ---------------------------------------------------------------------------
def quarters(since: date, until: date) -> Iterator[tuple[int, int]]:
    """(year, quarter) pairs covering since..until."""
    y, q = since.year, (since.month - 1) // 3 + 1
    while (y, q) <= (until.year, (until.month - 1) // 3 + 1):
        yield y, q
        y, q = (y + 1, 1) if q == 4 else (y, q + 1)


def parse_form_index(text: str) -> list[Filing]:
    """Parse EDGAR's fixed-width form.idx into Filing records."""
    lines = text.splitlines()
    start = next((i + 1 for i, line in enumerate(lines) if set(line.strip()) == {"-"}), 0)
    out = []
    for line in lines[start:]:
        m = _IDX_LINE.match(line)
        if m:
            out.append(Filing(m["form"], m["name"].strip(), m["cik"], m["date"], Path(m["file"]).stem))
    return out


def is_cmbs_name(company: str, patterns: list[str]) -> bool:
    return any(re.search(p, company, re.IGNORECASE) for p in patterns)


def limit_trusts(filings: list[Filing], max_trusts: int | None) -> list[Filing]:
    """Keep only `max_trusts` trusts (the ones that filed most recently), with all of their filings."""
    if not max_trusts:
        return filings
    latest: dict[str, str] = {}
    for f in filings:
        latest[f.cik] = max(latest.get(f.cik, ""), f.filed)
    chosen = set(sorted(latest, key=lambda c: latest[c], reverse=True)[:int(max_trusts)])
    return [f for f in filings if f.cik in chosen]


def select_filings(filings: list[Filing], cfg: dict, since: date, until: date,
                   ciks: set[str] | None, max_trusts: int | None = None) -> list[Filing]:
    """Keep ABS-EE filings in the date range from CMBS trusts (or the given CIKs), oldest first."""
    keep = []
    for f in filings:
        if f.form not in cfg["form_types"] or not (since.isoformat() <= f.filed <= until.isoformat()):
            continue
        if ciks:
            if f.cik.lstrip("0") in ciks:
                keep.append(f)
        elif is_cmbs_name(f.company, cfg["cmbs_name_patterns"]):
            keep.append(f)
    return sorted(limit_trusts(keep, max_trusts), key=lambda f: (f.filed, f.cik))


# ---------------------------------------------------------------------------
# Reading the XML
# ---------------------------------------------------------------------------
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find_text(elem: ET.Element | None, names: list[str], skip_tag: str | None = None) -> str | None:
    """First non-empty text among `names` (in order of preference) below `elem`, not looking inside `skip_tag`."""
    if elem is None:
        return None
    found: dict[str, str] = {}
    stack = list(elem)
    while stack:
        child = stack.pop()
        name = _local(child.tag)
        if skip_tag and name == skip_tag:
            continue
        if name in names and name not in found and child.text and child.text.strip():
            found[name] = child.text.strip()
        stack.extend(child)
    return next((found[n] for n in names if n in found), None)


def _fraction(value: str | None, threshold: float) -> float | None:
    """0.95 stays 0.95; 95 becomes 0.95 (some files give percents)."""
    num = to_float(value)
    if num is None:
        return None
    return num / 100 if abs(num) > threshold else num


@dataclass
class ParseResult:
    rows: list[dict]
    found: Counter            # field -> how many loans had a value for it
    loans_seen: int
    sample_tags: list[str]    # tag names inside the first loan (to help fix wrong names in the config)


def parse_asset_xml(path: Path, cfg: dict, *, deal_name: str, file_id: str) -> ParseResult:
    """Parse one EX-102 XML file into loans rows (one per loan, first property only)."""
    record_tag, prop_tag, fields = cfg["record_tag"], cfg["property_tag"], cfg["fields"]
    pct_threshold = float(cfg.get("occupancy_percent_threshold", 1.5))
    now = db.utc_now()
    rows: dict[str, dict] = {}
    found: Counter = Counter()
    sample_tags: list[str] = []
    seen = 0
    for _event, elem in ET.iterparse(str(path), events=("end",)):
        if _local(elem.tag) != record_tag:
            continue
        seen += 1
        if not sample_tags:
            sample_tags = sorted({_local(e.tag) for e in elem.iter() if e is not elem})[:60]
        prop = next((c for c in elem.iter() if _local(c.tag) == prop_tag), None)

        def get(target: str) -> str | None:
            """Loan-level tag first, then the loan's first <property>."""
            names = fields.get(target, [])
            return _find_text(elem, names, skip_tag=prop_tag) or _find_text(prop, names)

        for target in fields:
            if get(target) is not None:
                found[target] += 1
        number, period = clean_str(get("loan_number")), to_date(get("report_period"))
        if not number or not period:
            elem.clear()
            continue
        key = f"edgar:{deal_name}:{number}"
        rows[key] = {
            "loan_key": key, "source": "sec_edgar", "deal_name": deal_name,
            "property_name": clean_str(get("property_name")),
            "property_address": clean_str(get("property_address")),
            "property_city": clean_str(get("property_city")),
            "property_state": clean_str(get("property_state")),
            "property_zip": clean_str(get("property_zip")),
            "property_type": clean_str(get("property_type")),
            "current_balance": to_float(get("current_balance")),
            "original_balance": to_float(get("original_balance")),
            "maturity_date": to_date(get("maturity_date")),
            "dscr": to_float(get("dscr")),
            "occupancy": _fraction(get("occupancy"), pct_threshold),
            "payment_status": clean_str(get("payment_status")),
            "paid_through": to_date(get("paid_through")),
            "workout_code": clean_str(get("workout_code")),
            "non_recoverability": _yes(get("non_recoverability")),
            "servicer_name": clean_str(get("servicer_name")),
            "watchlist_flag": None,          # not in ABS-EE
            "servicer_commentary": None,     # not in ABS-EE
            "report_period": period, "file_id": file_id, "collected_at": now,
            # Item 1125 extras (stored on loans; also fed to operating_metrics below)
            "origination_date": to_date(get("origination_date")),
            "modified_indicator": _yes(get("modified")),
            "post_mod_maturity": to_date(get("post_mod_maturity")),
            "value_securitization": to_float(get("sec_value")),
            "value_securitization_date": to_date(get("sec_value_date")),
            "value_recent": to_float(get("recent_value")),
            "value_recent_date": to_date(get("recent_value_date")),
            "_ops": {
                "sec_occupancy": _fraction(get("sec_occupancy"), pct_threshold),
                "sec_revenue": to_float(get("sec_revenue")), "sec_expenses": to_float(get("sec_expenses")),
                "sec_noi": to_float(get("sec_noi")),
                "recent_revenue": to_float(get("recent_revenue")),
                "recent_expenses": to_float(get("recent_expenses")), "recent_noi": to_float(get("recent_noi")),
                "fin_start": to_date(get("fin_start")), "fin_end": to_date(get("fin_end")),
                "sec_coverage": to_float(get("sec_coverage")),
                "recent_coverage": to_float(get("recent_coverage")),
                "recent_ds": to_float(get("recent_ds")),
                "io": _yes(get("io")),
                "neg_am": _yes(get("neg_am")),
                "amort_term": to_float(get("amort_term")),
                "rate_type": clean_str(get("rate_type")),
                "io_term": to_float(get("io_term")),
                "orig_term": to_float(get("orig_term")),
                "current_balance": to_float(get("current_balance")),
                "original_balance": to_float(get("original_balance")),
                "modified": _yes(get("modified")),
            },
        }
        elem.clear()
    return ParseResult(list(rows.values()), found, seen, sample_tags)


def audit_tags(result: ParseResult, cfg: dict) -> list[str]:
    """Warn about every field for which NO loan had a value (probably a wrong tag name in edgar.yaml)."""
    if result.loans_seen == 0:
        return []
    optional = set(cfg.get("optional_fields", []))
    missing = [f for f in cfg["fields"] if result.found[f] == 0 and f not in optional]
    warnings = []
    if missing:
        warnings.append(f"no value found for {', '.join(missing)} in any of {result.loans_seen} loans; the tag names in "
                        f"config/edgar.yaml may be wrong. Tags present in the first loan: {', '.join(result.sample_tags)}")
    for w in warnings:
        log.warning("TAG AUDIT: %s", w)
    return warnings


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
EXTRA_LOAN_COLUMNS = {"origination_date": "DATE", "modified_indicator": "BOOLEAN", "value_securitization": "DOUBLE",
                      "value_securitization_date": "DATE", "value_recent": "DOUBLE", "value_recent_date": "DATE",
                      "post_mod_maturity": "DATE",
                      # Research lane (U): payment-distress fields
                      "paid_through": "DATE", "workout_code": "VARCHAR", "non_recoverability": "BOOLEAN"}


def ensure_loan_columns(con) -> None:
    """Reg AB II extras on the loans table (added in place, so existing databases keep working)."""
    for col, typ in EXTRA_LOAN_COLUMNS.items():
        con.execute(f"ALTER TABLE loans ADD COLUMN IF NOT EXISTS {col} {typ}")


def _yes(v) -> bool | None:
    if v is None:
        return None
    return str(v).strip().lower() in ("y", "yes", "true", "1")


def write_loans(con, result: ParseResult) -> int:
    """Write the rows, keeping only the most recent report per loan. Returns how many were written."""
    ensure_loan_columns(con)
    return sum(1 for r in result.rows if db.upsert_loan_if_newer(con, {k: v for k, v in r.items() if k != "_ops"}))


def _rows_exist(con, file_id: str) -> bool:
    return con.execute("SELECT 1 FROM loans WHERE file_id = ? LIMIT 1", [file_id]).fetchone() is not None


def _get(url: str, session=None):
    host = url.split("/")[2]
    return fetch.polite_get(url, config.domain_settings(host), session=session)


def process_filing(con, run, filing: Filing, cfg: dict, session=None) -> None:
    """Find the EX-102 file in one filing, download it once, and load its loans."""
    idx_url = cfg["filing_index_url"].format(cik=filing.cik, accession_nodash=filing.accession_nodash)
    resp = _get(idx_url, session)
    if resp is None:
        raise RuntimeError(f"could not read the filing index {idx_url} (blocked, disallowed or not found)")
    items = resp.json().get("directory", {}).get("item", [])
    pattern = re.compile(cfg["asset_file_pattern"], re.IGNORECASE)
    assets = [i for i in items if pattern.search(i.get("name", ""))]
    if not assets:
        log.info("no EX-102 asset file in %s (%s)", filing.accession, filing.company)
        return
    for item in assets:
        size_mb = (to_float(item.get("size")) or 0) / 1e6
        if size_mb > cfg.get("max_file_mb", 200):
            run.error(f"{item['name']}: skipped, {size_mb:.0f} MB is over max_file_mb")
            continue
        url = cfg["file_url"].format(cik=filing.cik, accession_nodash=filing.accession_nodash, name=item["name"])
        known = con.execute("SELECT file_id FROM raw_files WHERE url = ?", [url]).fetchone()
        if known and _rows_exist(con, known[0]):                 # already downloaded and loaded: no new request
            run.skipped()
            continue
        res = fetch.fetch_and_store(
            con, url, config.domain_settings(url.split("/")[2]), source="sec_edgar", doc_category="other",
            collector_name=COLLECTOR, session=session, state=None, ext="xml",
            source_record_id=filing.accession, notes=f"deal={filing.company}; filed={filing.filed}")
        if res is None:
            raise RuntimeError(f"could not download {url}")
        if res.status == "duplicate" and _rows_exist(con, res.file_id):
            run.skipped()
            continue
        load_file(con, run, res.file_id, config.from_stored_path(res.file_path), filing.company, cfg)
        run.new_file() if res.status == "new" else run.skipped()


def _months(start, end) -> int:
    if not start or not end or end <= start:
        return 12
    return max(1, min(12, round((end - start).days / 30.44)))


BASIS_TOLERANCE = 0.10   # cmbs.news's published convention: the filed coverage must match within 10%


def classify_statement_basis(o: dict) -> tuple[str, str]:
    """Is this loan's 'most recent' Schedule AL operating statement a YEAR's figures or a PART-YEAR's?

    Servicers often file a trailing-twelve-months or annualized figure under the dates of the latest
    quarter or half year, so the filed start/end dates alone do not say which (cmbs.news methodology,
    https://cmbs.news/methodology/). The filing's own coverage figures tell them apart: the annual debt
    service implied at securitization (underwritten NOI / underwritten coverage) is set against the
    latest coverage; when the filed NOI reproduces that coverage within 10%, the amounts are already on
    a yearly basis; when the NOI scaled to a year does, the statement is a part-year's.

    ELIGIBILITY (checked BEFORE the numeric test, per the documented limits of the method): the implied
    debt service must be stable from securitization to the statement date. It is not when the loan is
    paid off, modified, floating-rate/ARM, or amortizing within its term (interest-only period shorter
    than the loan term with a scheduled amortization). Those statements classify 'unknown' by design.

    Returns (basis, evidence) where basis is 'annualized' (use as filed), 'period_actual' (scale to a
    year), or 'unknown' (excluded from annualized comparisons, flagged for manual review). A numeric
    match means SERVICER-BASIS-CONSISTENT, not a proven accounting fact. The filed coverage figures are
    used ONLY as basis evidence - never as a scored input (DSCR is not scored).
    """
    noi = o.get("recent_noi")
    if noi is None:
        return "unknown", "no most-recent NOI filed"
    start, end = o.get("fin_start"), o.get("fin_end")
    months = _months(start, end) if start and end and end > start else None
    if months and months >= 12:
        return "annualized", "filed statement period is 12 months"
    cb = o.get("current_balance")
    if cb is not None and cb <= 0:
        return "unknown", "loan paid off - coverage test ineligible"
    if o.get("modified"):
        return "unknown", "loan modified - debt service may have changed, coverage test ineligible"
    rate_type = (o.get("rate_type") or "").strip().upper()
    if rate_type and rate_type not in ("1", "F", "FIX", "FIXED"):
        return "unknown", "floating-rate/ARM loan - debt service not stable, coverage test ineligible"
    iot = o.get("io_term") or 0
    if o.get("orig_term") and o.get("amort_term") and iot < o["orig_term"]:
        return "unknown", "amortizing within term (IO period shorter than loan term) - coverage test ineligible"
    unoi, ucov, cov = o.get("sec_noi"), o.get("sec_coverage"), o.get("recent_coverage")
    if not (unoi and ucov and cov) or ucov == 0 or not months:
        return "unknown", "coverage figures or statement dates missing - cannot test basis"
    ads = unoi / ucov
    if ads == 0:
        return "unknown", "implied annual debt service is zero"
    r_filed, r_ann = noi / ads, noi / ads * 12.0 / months
    d_filed, d_ann = abs(r_filed - cov), abs(r_ann - cov)
    if min(d_filed, d_ann) > BASIS_TOLERANCE * max(abs(cov), 1e-9):
        return "unknown", "filed coverage matches neither the as-filed nor the annualized reading"
    if d_filed <= d_ann:
        return "annualized", (f"servicer-basis-consistent: NOI / implied annual debt service matches filed "
                              f"coverage within 10% (period {months}mo)")
    return "period_actual", (f"servicer-basis-consistent: NOI scaled to a year matches filed coverage "
                             f"within 10% (period {months}mo)")


def _put_ops(con, link, period, months, values: dict, source: str, file_id) -> bool:
    """One operating_metrics row per loan per period (merging into an existing row for the same period).

    Reference columns (debt_service, disclosed_on, basis, basis_evidence) MERGE too: a re-parse that
    lacks them keeps what an earlier parse stored instead of erasing it."""
    values = {k: v for k, v in values.items() if v is not None}
    if not period or not values:
        return False
    cols = ("occupancy", "revenue", "expenses", "noi", "debt_service", "disclosed_on", "basis", "basis_evidence")
    old = con.execute(
        f"SELECT {', '.join(cols)} FROM operating_metrics WHERE link_id = ? AND "
        "period_end = ? AND source LIKE 'sec_edgar%'", [link, period]).fetchone()
    merged = dict(zip(cols, old)) if old else {}
    merged.update(values)
    con.execute("DELETE FROM operating_metrics WHERE link_id = ? AND period_end = ? AND source LIKE 'sec_edgar%'",
                [link, period])
    con.execute("INSERT INTO operating_metrics (link_id, period_end, period_months, occupancy, revenue, expenses, noi, "
                "debt_service, disclosed_on, basis, basis_evidence, source, file_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [link, period, months, merged.get("occupancy"), merged.get("revenue"), merged.get("expenses"),
                 merged.get("noi"), merged.get("debt_service"), merged.get("disclosed_on"),
                 merged.get("basis"), merged.get("basis_evidence"), source, file_id])
    return True


_FIN_FIELDS = ("revenue", "expenses", "noi", "debt_service")


def _unallocated_collateral_links(rows: list[dict]) -> set:
    """Multi-property loans: some servicers file the most-recent statement only at loan level.
    The collateral blocks then carry literal zeros, an exact copy of the loan-level row, or a
    partial mix (block-1 revenue with wrong expenses). None of those is a per-property
    observation - writing one fabricates a zero-NOI year (a false -100% erosion signal) or
    double-attributes loan figures to one property.
    A collateral group is UNALLOCATED, and every collateral row in it is skipped, when the
    loan-level row carries figures and any of:
      (a) the collateral values do not sum back to the loan-level value for a filed field
          (catches partial/mixed allocations);
      (b) a collateral block is all-zero on revenue/expenses/NOI while the loan level is not
          (unreported collateral, not a zero-NOI year);
      (c) a collateral block exactly duplicates the loan-level row (block-1 inheritance).
    A REAL per-property split (blocks sum back to the loan total to the cent, no zero or
    duplicate blocks) is never skipped; neither is a group with no loan-level figures
    (nothing to reconcile against). The loan-level row is always kept, labeled loan-level."""
    fin = {}
    for r in rows:
        o = r.get("_ops") or {}
        fin[r["loan_key"]] = tuple(o.get(f"recent_{k}") if k != "debt_service" else o.get("recent_ds")
                                   for k in _FIN_FIELDS)
    bases: dict[str, list[str]] = {}
    for link in fin:
        m = re.match(r"^(.*)-(\d+)$", link)
        if m and m.group(1) in fin:
            bases.setdefault(m.group(1), []).append(link)
    skip: set = set()
    for base, collaterals in bases.items():
        base_vals = fin[base]
        if all(v is None for v in base_vals):
            continue                                    # no loan-level row to reconcile against
        unallocated = False
        for i in range(len(_FIN_FIELDS)):
            b = base_vals[i]
            col_vals = [fin[c][i] for c in collaterals]
            if b is not None and any(v is not None for v in col_vals):
                total = sum(v or 0 for v in col_vals)
                if abs(total - b) > max(1.0, abs(b) * 0.005):
                    unallocated = True                  # (a) collateral split does not reconcile
        for c in collaterals:
            vals = fin[c]
            if all(v is None for v in vals):
                continue                                # nothing filed: the all-NULL row path decides
            if vals == base_vals:
                unallocated = True                      # (c) block-1 inheritance of the loan-level row
            elif all(not v for v in vals[:3]) and any(v for v in base_vals[:3] if v):
                unallocated = True                      # (b) zeros where the loan level carries figures
        if unallocated:
            skip.update(collaterals)
    return skip


def _suppress_unreported_child_occupancy(rows: list[dict]) -> list[str]:
    """Collateral rows (key "<parent>-NNN") whose filing reports an explicit occupancy 0 while the
    SAME file's parent loan reports a NONZERO occupancy for the same period and the child carries
    NO allocated financials (revenue/expenses/NOI/debt service all absent). That 0 is an
    unallocated-block placeholder, not a vacancy observation: left in, it reads as a full
    occupancy collapse (a false Q slide). Suppress it to unknown (NULL) on both the loans row and
    the operating_metrics bridge.
    Kept verbatim: the parent row itself (including a parent that genuinely files 0 - when the
    parent is 0 or unreported, every child 0 stays as filed, genuine or not); any child with
    allocated financials; any child with a nonzero occupancy; any child whose parent is not in
    the same file. Mutates rows in place; returns the suppressed loan_keys."""
    by_key = {r["loan_key"]: r for r in rows}
    suppressed: list[str] = []
    for r in rows:
        m = re.match(r"^(.*)-(\d+)$", r["loan_key"])
        if not m or m.group(1) not in by_key:
            continue                                    # not a collateral-split row of a filed parent
        parent = by_key[m.group(1)]
        if parent.get("report_period") != r.get("report_period"):
            continue                                    # no same-period parent reading to judge against
        if not parent.get("occupancy"):
            continue                                    # parent 0 or unreported: child 0 stays as filed
        if r.get("occupancy") != 0:
            continue                                    # only an explicit filed 0 is a placeholder candidate
        o = r.get("_ops") or {}
        if any(o.get(k) is not None for k in ("recent_revenue", "recent_expenses", "recent_noi", "recent_ds")):
            continue                                    # allocated collateral: the 0 is a real observation
        r["occupancy"] = None
        suppressed.append(r["loan_key"])
    return suppressed


def write_operating_metrics(con, result: ParseResult) -> int:
    """Feed each loan's public Schedule AL operating data into operating_metrics (one row per loan per period):
      * the occupancy reported for this period (history for the occupancy slide, Q);
      * the SECURITIZATION baseline - underwritten revenue, expenses, NOI and occupancy, dated at origination;
      * the MOST RECENT operating statement - revenue, expenses, NOI for its own period.
    Baseline + most recent give a before/after from one public filing for Q, V (income erosion) and
    X (cost squeeze). DSCR is not used (team decision)."""
    from scorer.equation import create_equation_tables
    create_equation_tables(con)
    written = 0
    unallocated = _unallocated_collateral_links(result.rows)
    skipped_collateral = 0
    for r in result.rows:
        o, link, fid = r.get("_ops") or {}, r["loan_key"], r["file_id"]
        if r.get("occupancy") is not None:
            written += _put_ops(con, link, r.get("report_period"), 12, {"occupancy": r["occupancy"]}, "sec_edgar", fid)
        base_date = r.get("origination_date") or r.get("value_securitization_date")
        sec_values = {"occupancy": o.get("sec_occupancy"), "revenue": o.get("sec_revenue"),
                      "expenses": o.get("sec_expenses"), "noi": o.get("sec_noi")}
        if any(v is not None for v in sec_values.values()):
            sec_values["basis"] = "annualized"
            sec_values["basis_evidence"] = ("underwritten full-year figures at issuance "
                                            "(financialsSecuritizationDate is a single year-end)")
        written += _put_ops(con, link, base_date, 12, sec_values, "sec_edgar_securitization", fid)
        basis, evidence = classify_statement_basis(o)
        fin_values = {"revenue": o.get("recent_revenue"), "expenses": o.get("recent_expenses"),
                      "noi": o.get("recent_noi"), "debt_service": o.get("recent_ds")}
        if link in unallocated:
            removed = con.execute(
                "DELETE FROM operating_metrics WHERE link_id = ? AND file_id = ? AND source = 'sec_edgar_financials'",
                [link, fid]).rowcount
            if removed:
                log.info("%s: removed %d prior unallocated collateral row(s) for a quarantined link",
                         result.rows[0].get("deal_name") if result.rows else "?", removed)
            skipped_collateral += 1                     # unallocated collateral split: not a property observation
        else:
            if any(v is not None for v in fin_values.values()):
                fin_values["basis"] = basis
                fin_values["basis_evidence"] = evidence
            written += _put_ops(con, link, o.get("fin_end"), _months(o.get("fin_start"), o.get("fin_end")),
                                fin_values, "sec_edgar_financials", fid)
    if skipped_collateral:
        log.info("%s: skipped %d unallocated collateral financial row(s) (loan-level statement only)",
                 result.rows[0].get("deal_name") if result.rows else "?", skipped_collateral)
    return written


def load_file(con, run, file_id: str, path: Path, deal_name: str, cfg: dict) -> int:
    result = parse_asset_xml(path, cfg, deal_name=deal_name, file_id=file_id)
    if not result.rows:
        run.error(f"no loan rows parsed from {path.name} ({deal_name}): not CMBS, or the tag names in config/edgar.yaml differ")
        audit_tags(result, cfg)
        return 0
    audit_tags(result, cfg)
    suppressed = _suppress_unreported_child_occupancy(result.rows)
    if suppressed:
        log.info("%s: suppressed unreported collateral occupancy on %d row(s) (parent nonzero, no allocated financials): %s",
                 deal_name, len(suppressed), ", ".join(sorted(suppressed)[:8]))
    written = write_loans(con, result)
    write_operating_metrics(con, result)
    db.set_state(con, COLLECTOR, f"schedule_al_v2:{file_id}", "done")
    log.info("%s: %d loans read, %d written", deal_name, len(result.rows), written)
    return written


def load_unparsed_manual_files(con, run, cfg: dict) -> int:
    """
    Parse EX-102 XML files that were saved by hand (data/inbox/sec_edgar/), whose loans were never
    written because a run crashed, or that predate the operating_metrics occupancy bridge (backfill).
    The trust name comes from the notes: "deal=<name>".
    """
    # Files with no operating_metrics row get (re)parsed: that covers hand-saved files, crashed runs,
    # and the one-time backfill of files collected before the occupancy bridge existed.
    from scorer.equation import create_equation_tables
    create_equation_tables(con)
    rows = con.execute(
        "SELECT r.file_id, r.file_path, r.notes FROM raw_files r WHERE r.source = 'sec_edgar' AND r.file_type = 'xml'"
    ).fetchall()
    # Re-read a file once for the Schedule AL operating/valuation/basis fields (bookmark
    # "schedule_al_v3"), so files collected before those fields were captured get them too (one-time
    # backfill; v3 adds the statement-basis classification and reference columns).
    rows = [r for r in rows
            if not db.get_state(con, COLLECTOR, f"schedule_al_v3:{r[0]}")
            or not con.execute("SELECT 1 FROM operating_metrics m WHERE m.file_id = ? LIMIT 1", [r[0]]).fetchone()]
    n = 0
    for file_id, file_path, notes in rows:
        m = re.search(r"deal=([^;]+)", notes or "")
        deal = m.group(1).strip() if m else "UNKNOWN TRUST (manual upload)"
        try:
            n += 1 if load_file(con, run, file_id, config.from_stored_path(file_path), deal, cfg) else 0
            db.set_state(con, COLLECTOR, f"schedule_al_v3:{file_id}", "done")
        except Exception as e:                                   # a bad file must not stop the rest
            run.error(f"could not parse {file_id}: {e}")
    return n


def run_edgar(con, run, since: date, until: date, *, ciks: set[str] | None = None, max_trusts: int | None = None,
              cfg: dict | None = None, session=None) -> int:
    """Collect every CMBS ABS-EE filing filed between `since` and `until`. Returns the number of filings."""
    cfg = cfg or load_yaml(CONFIG_PATH)
    max_trusts = max_trusts if max_trusts is not None else cfg.get("max_trusts")
    load_unparsed_manual_files(con, run, cfg)

    selected: list[Filing] = []
    for year, q in quarters(since, until):
        url = cfg["index_url"].format(year=year, quarter=q)
        resp = _get(url, session)
        if resp is None:
            run.error(f"could not read the SEC filing index {url}")
            continue
        selected += select_filings(parse_form_index(resp.text), cfg, since, until, ciks, None)
    selected = sorted(limit_trusts(selected, max_trusts), key=lambda f: (f.filed, f.cik))   # limit across all quarters
    log.info("%d CMBS ABS-EE filings selected from %d trusts", len(selected), len({f.cik for f in selected}))

    for filing in selected:
        try:
            process_filing(con, run, filing, cfg, session)
        except Exception as e:                                   # one failing filing must not stop the rest
            run.error(f"{filing.company} {filing.accession}: {e}")
    for domain, reason in fetch.get_fallbacks().items():
        if domain.endswith("sec.gov"):
            run.manual_inbox(f"sec_edgar ({domain})", reason)
    return len(selected)


def collect(con, since: date | None = None, until: date | None = None, ciks=None, max_trusts=None,
            session=None) -> RunTracker:
    """The entry point used by run_all.py."""
    cfg = load_yaml(CONFIG_PATH)
    until = until or date.today()
    since = since or until - timedelta(days=int(cfg.get("lookback_days", 45)))
    with RunTracker(con, COLLECTOR) as run:
        run_edgar(con, run, since, until, ciks=ciks, max_trusts=max_trusts, cfg=cfg, session=session)
    return run


def main() -> None:
    parser = argparse.ArgumentParser(description="Load CMBS loan-level data from SEC EDGAR (Form ABS-EE).")
    parser.add_argument("--since", type=date.fromisoformat, help="first filing date YYYY-MM-DD (default: 45 days ago)")
    parser.add_argument("--until", type=date.fromisoformat, help="last filing date (default: today)")
    parser.add_argument("--cik", action="append", help="only this trust CIK (repeatable); skips the name filter")
    parser.add_argument("--max-trusts", type=int, help="read at most this many trusts (default from edgar.yaml: 20)")
    args = parser.parse_args()
    setup_logging()
    con = db.connect()
    ciks = {c.lstrip("0") for c in args.cik} if args.cik else None
    run = collect(con, args.since, args.until, ciks, args.max_trusts)
    print(f"EDGAR: {run.status}, {run.files_new} new, {run.files_skipped} skipped, {len(run.errors)} errors")


if __name__ == "__main__":
    main()
