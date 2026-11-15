"""Tests for collectors/permits.py: permit / inspection / certificate-of-occupancy files -> scoring tables.
Made-up data only."""
from datetime import date

from collectors import permits
from collectors.common import config, inbox
from scorer import equation as eq
from scorer.group import group_properties
from scout_reader_schema import create_scout_reader_tables

PERMITS_CSV = """PermitNum,PermitClass,PermitType,IssuedDate,CompletedDate,StatusCurrent,OriginalAddress1,OriginalCity,OriginalState
BP-FAKE-1,Commercial,New Building,2024-03-01,,Issued,10 FAKE Market St,Austin,TX
BP-FAKE-2,Residential,Single Family,2024-04-01,,Issued,22 FAKE Home Ln,Austin,TX
BP-FAKE-3,Commercial,New Building,2022-01-10,2025-06-30,Final,30 FAKE Done Ave,Austin,TX
"""
INSPECTIONS_CSV = """Permit #,Insp Date,Insp Type,Result
BP-FAKE-1,2026-04-01,Framing,Pass
BP-FAKE-1,2026-05-14,Mechanical,Partial
"""


def drop(state, county, category, name, text):
    folder = config.inbox_dir() / "permit" / state / county / category
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(text)


def test_permit_files_become_scoring_inputs(con):
    create_scout_reader_tables(con)
    drop("TX", "travis", "building_permit", "permits.csv", PERMITS_CSV)
    drop("TX", "travis", "permit_inspection", "inspections.csv", INSPECTIONS_CSV)
    inbox.load_inbox(con)
    permits.collect(con)

    kinds = dict(con.execute("SELECT filing_type, count(*) FROM filing_fields GROUP BY 1").fetchall())
    assert kinds == {"building_permit": 2, "certificate_of_occupancy": 1}       # residential permit skipped
    insp = con.execute("SELECT permit_number, inspection_date FROM permit_inspections ORDER BY 2").fetchall()
    assert insp == [("BP-FAKE-1", date(2026, 4, 1)), ("BP-FAKE-1", date(2026, 5, 14))]

    # The equation now sees them: site inactivity on the open permit, and the finished building's certificate.
    cfg = eq.load_equation_config()
    _, _, inspections, _ = eq.load_inputs(con)
    props = {p.filings[0]["property_address"]: p for p in group_properties(con) if p.filings}
    active = eq.inspection_gap(props["10 FAKE Market St"], cfg["inspection_gap"], inspections, date(2026, 11, 10))
    assert active.value == 1.0 and "180 days since last inspection" in active.why
    done = props["30 FAKE Done Ave"]
    assert eq.has_opened(done)

    permits.collect(con)                                                         # running twice adds nothing
    assert con.execute("SELECT count(*) FROM permit_inspections").fetchone()[0] == 2


def test_columns_follow_blds_names_with_aliases():
    cols = permits.column_map(["Permit #", "Issue Date", "Insp Date"], {"permit_number": "Permit #"})
    assert cols["permit_number"] == "Permit #" and "inspection_date" not in cols or cols.get("inspection_date") == "Insp Date"
    cols = permits.column_map(["PermitNum", "IssuedDate", "CompletedDate", "InspectionDate"])
    assert cols == {"permit_number": "PermitNum", "issued_date": "IssuedDate", "completed_date": "CompletedDate",
                    "inspection_date": "InspectionDate"}


def test_ai_reader_skips_structured_permit_files_but_reads_scans(con):
    from reader import batch
    create_scout_reader_tables(con)
    drop("TX", "travis", "building_permit", "permits.csv", PERMITS_CSV)
    drop("TX", "travis", "certificate_of_occupancy", "co-scan.pdf", "%PDF-1.4 fake scan")
    inbox.load_inbox(con)
    cfg = {"reader": {"process_sources": ["permit"]}}
    picked = {r["file_type"] for r in batch.select_files(con, cfg)}
    assert "csv" not in picked and picked                                        # only the scan goes to the AI
