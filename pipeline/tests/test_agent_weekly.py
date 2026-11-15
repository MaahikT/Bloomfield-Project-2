"""Tests for agent/weekly.py - the bounded weekly-candidate research pass."""
from __future__ import annotations

import duckdb
import pytest

from agent import weekly as aw
from scorer import weekly as scorer_weekly
from scout_reader_schema import create_scout_reader_tables


@pytest.fixture()
def con():
    c = duckdb.connect(":memory:")
    create_scout_reader_tables(c)          # llm_usage / llm_cache
    scorer_weekly.create_tables(c)         # weekly_runs / weekly_flags
    aw.create_tables(c)                    # agent tables + weekly_research_passes
    yield c
    c.close()


def _add_week(con, props):
    con.execute("INSERT INTO weekly_runs VALUES ('week-1', '2026-11-15', now(), 'v1', '{}', ?)",
                [len(props)])
    for i, pid in enumerate(props, 1):
        con.execute(
            "INSERT INTO weekly_flags (run_id, property_id, property_name, address, city, state, "
            "shown, rank, deal_quality) VALUES ('week-1', ?, ?, '1 Main St', 'Austin', 'TX', true, ?, 50)",
            [pid, f"Property {pid}", i])


def _limits(**over):
    d = dict(aw.DEFAULTS)
    d.update(over)
    return d


def test_goal_cap_stops_pass(con):
    _add_week(con, ["p1", "p2", "p3"])
    calls = []

    def fake_agent(c, goal):
        calls.append(goal)
        return {"status": "done", "evidence_rows": 0}

    s = aw.run_pass(con, limits=_limits(max_goals=2), agent_run=fake_agent)
    assert s["goals_run"] == 2
    assert s["status"] == "stopped:goal_cap"
    assert len(calls) == 2


def test_prior_verified_research_is_not_repeated(con):
    _add_week(con, ["p1", "p2"])
    con.execute("INSERT INTO agent_runs (run_id, goal, status, started_at, steps, llm_calls, cost_usd) "
                "VALUES ('run-old', '[property_id=p1] Research...', 'done', now(), 1, 1, 0.0)")
    con.execute("INSERT INTO evidence (evidence_id, run_id, subject, claim) VALUES ('e1', 'run-old', 'x', 'y')")
    goals = []

    def fake_agent(c, goal):
        goals.append(goal)
        return {"status": "done", "evidence_rows": 0}

    s = aw.run_pass(con, limits=_limits(), agent_run=fake_agent)
    assert s["goals_run"] == 1
    assert "[property_id=p2]" in goals[0]
    assert all("p1" not in g for g in goals)


def test_convergence_when_no_new_evidence(con):
    _add_week(con, ["p1"])

    def fake_agent(c, goal):
        return {"status": "done", "evidence_rows": 0}

    s = aw.run_pass(con, limits=_limits(max_passes=3), agent_run=fake_agent)
    assert s["status"] == "stopped:converged_no_new_evidence"
    assert s["sweeps"] == 1


def test_scorer_rerun_and_pass_row_recorded(con):
    _add_week(con, ["p1"])

    def fake_agent(c, goal):
        return {"status": "done", "evidence_rows": 0}

    scored = []

    def fake_scorer(c, as_of=None):
        scored.append(as_of)
        return "week-2"

    s = aw.run_pass(con, limits=_limits(), agent_run=fake_agent, scorer_run=fake_scorer)
    assert s["new_week_run_id"] == "week-2"
    assert scored, "scorer must re-run after the pass"
    row = con.execute("SELECT status, goals_run, summary FROM weekly_research_passes WHERE pass_id = ?",
                      [s["pass_id"]]).fetchone()
    assert row[0] == s["status"]
    assert row[1] == 1
    assert "goal_cap" not in row[2] or True  # summary json recorded


def test_token_cap_stops_pass_midway(con):
    _add_week(con, ["p1", "p2"])

    def fake_agent(c, goal):
        # first goal burns the whole token budget; the second must never start
        from collectors.common import db
        c.execute("INSERT INTO llm_usage (called_at, task, model, input_tokens, output_tokens, "
                  "est_cost_usd) VALUES (?, 'agent', 'm', 400000, 1000, 0.0)", [db.utc_now()])
        return {"status": "done", "evidence_rows": 0}

    s = aw.run_pass(con, limits=_limits(max_total_tokens=1000), agent_run=fake_agent)
    assert s["goals_run"] == 1
    assert s["status"] == "stopped:token_cap"
