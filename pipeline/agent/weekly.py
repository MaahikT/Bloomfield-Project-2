"""
weekly.py - a bounded research pass over this week's ranking candidates.

In plain English: the weekly ranking (scorer/weekly.py) scores every property we know about,
but some scores are low simply because we have no data. This driver hands the sourcing agent
(agent/loop.py) one research goal per candidate property, so it searches the web and saves
verified evidence - then the deterministic scorer re-runs and the ranking reflects what is
actually verifiable, not what happened to be in the database already.

It is BOUNDED on purpose. Nothing here runs on its own; a person starts a pass and the pass
stops at the FIRST of these limits (set in config/agent.yaml under weekly_loop):

  * max_goals          - how many candidate properties get one research goal each
  * max_model_calls    - total LLM calls across all goals this pass
  * max_total_tokens   - total input+output tokens across all goals this pass
  * max_search_calls   - total web searches this pass (1 search = 1 search-provider credit)
  * max_minutes        - wall-clock limit
  * max_passes         - sweeps over the candidate list (default 1)

It also stops early when a full sweep finds zero new verified evidence (convergence): more
searching is not going to change this week's ranking.

Dedup: a property is skipped when an earlier agent run already researched that exact property
marker and saved at least one evidence row - re-researching what is already verified wastes
budget. The marker is the line "[property_id=...]" inside the goal text.

It never scores anything itself and never changes scoring config. Scores come only from
scorer/weekly.py, before and after the pass, so the ranking change is attributable to the
new evidence.

Run:   python -m agent.weekly                 (uses config/agent.yaml weekly_loop limits)
       python -m agent.weekly --max-goals 5   (override any limit for this pass)
"""
from __future__ import annotations

import argparse
import json
import time
import uuid
from datetime import date, datetime, timezone

from collectors.common import db
from collectors.common.log import get_logger

from . import loop as agent_loop

log = get_logger("agent.weekly")

PASS_TASK = "agent"          # llm_usage rows written by the agent loop carry this task name

DEFAULTS = {
    "max_goals": 20,
    "max_model_calls": 60,
    "max_total_tokens": 400_000,
    "max_search_calls": 100,
    "max_minutes": 45,
    "max_passes": 1,
}


def load_limits(cfg: dict | None = None) -> dict:
    cfg = cfg if cfg is not None else agent_loop.load_agent_config()
    limits = dict(DEFAULTS)
    for k, v in (cfg.get("weekly_loop") or {}).items():
        if k in limits and v is not None:
            limits[k] = v
    return limits


def create_tables(con) -> None:
    agent_loop.create_tables(con)
    con.execute("""
        CREATE TABLE IF NOT EXISTS weekly_research_passes (
            pass_id      TEXT PRIMARY KEY,
            week_run_id  TEXT,           -- the scorer run this pass researched for
            started_at   TIMESTAMP,
            finished_at  TIMESTAMP,
            status       TEXT,           -- completed | stopped:<reason>
            goals_run    INTEGER,
            goals_skipped INTEGER,
            evidence_new INTEGER,
            llm_calls    INTEGER,
            tokens       INTEGER,
            search_calls INTEGER,
            summary      TEXT
        )""")


# ---------------------------------------------------------------------------
# Candidates and goals
# ---------------------------------------------------------------------------

def _latest_week_run(con) -> str | None:
    row = con.execute("SELECT run_id FROM weekly_runs ORDER BY as_of DESC, run_id DESC LIMIT 1").fetchone()
    return row[0] if row else None


def _candidates(con, week_run_id: str | None) -> list[dict]:
    """
    Every property the latest weekly run scored - shown or not. A property can be unshown
    exactly because missing evidence kept its score at zero, so restricting goals to the
    shown list would hide the properties research could help most. Order: shown first by
    rank, then the rest by deal quality, so limited budget goes to the visible list first.
    """
    if not week_run_id:
        return []
    cur = con.execute(
        "SELECT property_id, property_name, address, city, state, shown, rank, deal_quality "
        "FROM weekly_flags WHERE run_id = ? "
        "ORDER BY shown DESC, rank ASC NULLS LAST, deal_quality DESC, property_id",
        [week_run_id])
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _already_researched(con) -> set[str]:
    """property_id markers an earlier run researched and verified evidence for."""
    rows = con.execute(
        "SELECT DISTINCT r.goal FROM agent_runs r "
        "WHERE r.status IN ('done', 'step_cap') AND r.goal LIKE '%[property_id=%' "
        "AND EXISTS (SELECT 1 FROM evidence e WHERE e.run_id = r.run_id)").fetchall()
    found = set()
    for (goal,) in rows:
        marker = goal.split("[property_id=", 1)[1].split("]", 1)[0].strip()
        if marker:
            found.add(marker)
    return found


def _goal_for(c: dict) -> str:
    where = ", ".join(x for x in [c.get("address"), c.get("city"), c.get("state")] if x)
    name = c.get("property_name") or where or c["property_id"]
    return (
        f"[property_id={c['property_id']}] Research the property \"{name}\""
        + (f" at {where}" if where else "")
        + ". Search the public web for verifiable facts that feed Bloomfield's seven signals: "
        "loan maturity or refinance trouble, opening delays or construction status, rent or "
        "occupancy trends, health/fire inspection history, and anything else public records "
        "show about this property. Save every fact you rely on as an evidence row with its "
        "exact source URL and a verbatim quote; record nothing you cannot verify. If a source "
        "blocks you, say so honestly and move on."
    )


# ---------------------------------------------------------------------------
# Budget accounting (this pass only; hard stops, checked before every goal)
# ---------------------------------------------------------------------------

def _pass_usage(con, since: datetime) -> dict:
    calls, tokens = con.execute(
        "SELECT count(*), COALESCE(sum(input_tokens + output_tokens), 0) "
        "FROM llm_usage WHERE task = ? AND called_at >= ?", [PASS_TASK, since]).fetchone()
    searches = con.execute(
        "SELECT count(*) FROM agent_steps WHERE kind = 'tool_call' AND name = 'web_search' "
        "AND created_at >= ?", [since]).fetchone()[0]
    return {"llm_calls": int(calls), "tokens": int(tokens or 0), "search_calls": int(searches)}


def _budget_stop(limits: dict, usage: dict, started: float, goals_run: int) -> str | None:
    if goals_run >= limits["max_goals"]:
        return "goal_cap"
    if usage["llm_calls"] >= limits["max_model_calls"]:
        return "model_call_cap"
    if usage["tokens"] >= limits["max_total_tokens"]:
        return "token_cap"
    if usage["search_calls"] >= limits["max_search_calls"]:
        return "search_cap"
    if (time.monotonic() - started) / 60 >= limits["max_minutes"]:
        return "time_cap"
    return None


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------

def run_pass(con, *, cfg: dict | None = None, limits: dict | None = None,
             agent_run=agent_loop.run, scorer_run=None) -> dict:
    """
    One bounded research pass. agent_run/scorer_run are injectable for tests.
    Returns a summary dict; also writes a weekly_research_passes row.
    """
    limits = limits or load_limits(cfg)
    create_tables(con)
    pass_id = f"pass-{uuid.uuid4().hex[:12]}"
    started_clock = time.monotonic()
    started_at = db.utc_now()
    week_run_id = _latest_week_run(con)
    con.execute(
        "INSERT INTO weekly_research_passes (pass_id, week_run_id, started_at, status, "
        "goals_run, goals_skipped, evidence_new, llm_calls, tokens, search_calls) "
        "VALUES (?,?,?,?,0,0,0,0,0,0)",
        [pass_id, week_run_id, started_at, "running"])

    candidates = _candidates(con, week_run_id)
    researched = _already_researched(con)
    ev_before = con.execute("SELECT count(*) FROM evidence").fetchone()[0]

    goals_run = goals_skipped = 0
    stop = None
    sweeps = 0
    while sweeps < max(1, int(limits["max_passes"])):
        sweeps += 1
        sweep_new = 0
        pending = [c for c in candidates if c["property_id"] not in researched]
        if not pending:
            stop = "all_candidates_researched"
            break
        for c in pending:
            usage = _pass_usage(con, started_at)
            stop = _budget_stop(limits, usage, started_clock, goals_run)
            if stop:
                break
            before = con.execute("SELECT count(*) FROM evidence").fetchone()[0]
            result = agent_run(con, _goal_for(c))
            goals_run += 1
            gained = con.execute("SELECT count(*) FROM evidence").fetchone()[0] - before
            sweep_new += max(0, gained)
            if result.get("evidence_rows", 0) > 0:
                researched.add(c["property_id"])
            log.info("pass %s goal %s: %s, +%d evidence", pass_id, c["property_id"],
                     result.get("status"), gained)
        else:
            # full sweep finished without hitting a cap
            if sweep_new == 0:
                stop = "converged_no_new_evidence"
                break
            stop = None
            continue
        break
    goals_skipped = len([c for c in candidates if c["property_id"] in researched]) - goals_run
    stop = stop or ("passes_done" if sweeps >= int(limits["max_passes"]) else "completed")

    # Re-score: the deterministic scorer re-reads the database, so the ranking now reflects
    # the new verified evidence. Scoring config is never touched here.
    new_run_id = None
    if scorer_run is not None:
        new_run_id = scorer_run(con, as_of=date.today())

    usage = _pass_usage(con, started_at)
    ev_new = con.execute("SELECT count(*) FROM evidence").fetchone()[0] - ev_before
    summary = {
        "pass_id": pass_id, "week_run_id": week_run_id, "new_week_run_id": new_run_id,
        "status": f"stopped:{stop}" if stop not in ("completed", "passes_done") else stop,
        "candidates": len(candidates), "goals_run": goals_run,
        "goals_skipped_already_researched": max(0, goals_skipped),
        "evidence_new": ev_new, "llm_calls": usage["llm_calls"],
        "tokens": usage["tokens"], "search_calls": usage["search_calls"],
        "sweeps": sweeps, "limits": limits,
    }
    con.execute(
        "UPDATE weekly_research_passes SET finished_at=?, status=?, goals_run=?, "
        "goals_skipped=?, evidence_new=?, llm_calls=?, tokens=?, search_calls=?, summary=? "
        "WHERE pass_id=?",
        [db.utc_now(), summary["status"], goals_run, summary["goals_skipped_already_researched"],
         ev_new, usage["llm_calls"], usage["tokens"], usage["search_calls"],
         json.dumps(summary), pass_id])
    log.info("weekly research pass %s: %s", pass_id, json.dumps(summary))
    return summary


def main() -> None:
    p = argparse.ArgumentParser(description="Bounded weekly-candidate research pass.")
    for k in DEFAULTS:
        p.add_argument(f"--{k.replace('_', '-')}", type=int, default=None)
    args = p.parse_args()
    overrides = {k: v for k, v in vars(args).items() if v is not None}
    limits = load_limits()
    limits.update(overrides)
    con = db.connect()
    from scorer import weekly as scorer_weekly
    summary = run_pass(con, limits=limits, scorer_run=scorer_weekly.run)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
