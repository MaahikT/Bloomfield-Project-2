"""Direct-research ingestion: same verification standard as the agent, no LLM."""
from __future__ import annotations

import duckdb
import pytest

from collectors.common.files import store_file
from agent import ingest, mapping, tools
from matcher.schema import SCHEMA_SQL as MATCHER_SCHEMA
from scorer import equation as eq

PAGE = ("Westshore Crossing in Tampa lists one-bedroom apartments from $1,850 per month. "
        "The property reports 82% occupancy for the period ending 2026-09-30, down from 94%.")


@pytest.fixture()
def con():
    from collectors.common import db as cdb
    c = duckdb.connect(":memory:")
    cdb.create_tables(c)
    from scorer import creative as cr
    for schema in (eq.INPUT_SCHEMA, cr.INPUT_SCHEMA):
        no_comments = "\n".join(l.split("--", 1)[0] for l in schema.splitlines())
        for stmt in no_comments.split(";"):    # duckdb execute() runs one statement at a time
            if stmt.strip():
                c.execute(stmt)
    c.execute(MATCHER_SCHEMA)
    tools.create_tables(c)
    c.execute("CREATE TABLE projects (project_id TEXT, project_name TEXT)")
    c.execute("INSERT INTO projects VALUES ('proj-1', 'Westshore Crossing')")
    c.execute("INSERT INTO property_links VALUES ('prop-1', 'projects', 'proj-1', 'address', 100.0, now())")
    yield c
    c.close()


def fake_fetcher(c, url):
    stored = store_file(c, PAGE.encode(), source="agent_web", doc_category="web_page",
                        collector_name="agent", url=url, notes="test")
    return {"ok": True, "url": url, "file_id": stored.file_id}


ROW = {"id": "t-0001", "property_id": "prop-1", "claim": "asking_rent", "value": "1850",
       "url": "https://example.com/westshore", "quote": "one-bedroom apartments from $1,850 per month",
       "notes": "unit_basis=unit", "summary": "Verified rent evidence ingested."}


def test_verified_row_is_saved_and_shows_like_agent_research(con):
    out = ingest.process_rows(con, [ROW], fetcher=fake_fetcher)
    assert out["saved"] == 1
    run = con.execute("SELECT goal, status, llm_calls, cost_usd FROM agent_runs WHERE run_id = 'ingest-t-0001'").fetchone()
    assert "[property_id=prop-1]" in run[0] and run[1] == "done" and run[2] == 0 and run[3] == 0.0
    ev = con.execute("SELECT claim, source_url FROM evidence WHERE run_id = 'ingest-t-0001'").fetchone()
    assert ev == ("asking_rent", "https://example.com/westshore")
    # the scorer-consumable claim maps into the formula's input table
    applied = mapping.apply(con)
    assert applied["mapped"] == 1
    rent = con.execute("SELECT asking_rent FROM rent_observations WHERE project_id = 'proj-1'").fetchone()
    assert rent[0] == 1850.0


def test_unverifiable_quote_is_refused(con):
    row = dict(ROW, id="t-0002", quote="these words are not on the page at all")
    out = ingest.process_rows(con, [row], fetcher=fake_fetcher)
    assert out["refused"] == 1 and out["saved"] == 0
    assert con.execute("SELECT count(*) FROM evidence").fetchone()[0] == 0


def test_unknown_property_is_invalid(con):
    out = ingest.process_rows(con, [dict(ROW, id="t-0003", property_id="nope")], fetcher=fake_fetcher)
    assert out["invalid"] == 1


def test_idempotent(con):
    ingest.process_rows(con, [ROW], fetcher=fake_fetcher)
    out = ingest.process_rows(con, [ROW], fetcher=fake_fetcher)
    assert out["skipped"] == 1
    assert con.execute("SELECT count(*) FROM evidence").fetchone()[0] == 1


def test_fetch_failure_is_recorded(con):
    out = ingest.process_rows(con, [dict(ROW, id="t-0004")],
                              fetcher=lambda c, u: {"ok": False, "error": "blocked"})
    assert out["fetch_failed"] == 1


def _ops_evidence(con, claim, value, quote):
    stored = store_file(con, PAGE.encode(), source="agent_web", doc_category="web_page",
                        collector_name="agent", url="https://example.com/o", notes="t")
    con.execute(
        "INSERT INTO evidence (evidence_id, run_id, subject, claim, value, source_kind, file_id, "
        "source_url, quote, db_query, source_title, observed_at, notes, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,current_timestamp,?,current_timestamp)",
        [f"ev-{claim}-{value}", "r1", "prop-1", claim, value, "web_page", stored.file_id,
         "https://example.com/o", quote, None, None,
         "period_end=2026-09-30; period_months=12"])


def test_occupancy_percent_maps_to_operating_metrics(con):
    _ops_evidence(con, "occupancy_rate", "82", "reports 82% occupancy")
    applied = mapping.apply(con)
    assert applied["mapped"] == 1, applied
    row = con.execute("SELECT occupancy, period_months FROM operating_metrics WHERE link_id = 'proj-1'").fetchone()
    assert abs(row[0] - 0.82) < 1e-9 and row[1] == 12


def test_ops_metrics_merge_into_one_period_row_and_never_overwrite(con):
    _ops_evidence(con, "operating_revenue", "1850", "from $1,850 per month")
    _ops_evidence(con, "operating_expenses", "850", "from $1,850 per month")
    applied = mapping.apply(con)
    assert applied["mapped"] == 2, applied
    row = con.execute("SELECT revenue, expenses FROM operating_metrics WHERE link_id = 'proj-1'").fetchone()
    assert row == (1850.0, 850.0)
    # a different value for the same field+period is kept, not overwritten
    _ops_evidence(con, "operating_revenue", "9999", "revenue of $9,999 this period")
    applied2 = mapping.apply(con)
    assert applied2["skipped"].get("existing reading kept") == 1
    assert con.execute("SELECT revenue FROM operating_metrics WHERE link_id = 'proj-1'").fetchone()[0] == 1850.0


def test_market_occupancy_maps(con):
    _ops_evidence(con, "market_occupancy", "82", "reports 82% occupancy")
    con.execute("UPDATE evidence SET notes = 'state=FL; property_type=Multifamily; period=2026-09-01' "
                "WHERE claim = 'market_occupancy'")
    applied = mapping.apply(con)
    assert applied["mapped"] == 1, applied
    row = con.execute("SELECT occupancy FROM market_occupancy WHERE state = 'FL'").fetchone()
    assert abs(row[0] - 0.82) < 1e-9
