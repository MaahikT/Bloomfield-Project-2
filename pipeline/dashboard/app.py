"""
app.py - the researcher's dashboard (Part D).

    cd pipeline
    python -m scorer.weekly                     # this week's run on the real database
    streamlit run dashboard/app.py
    # made-up demo:
    python -m scorer.demo_week
    BLOOMFIELD_DB=data/demo.duckdb streamlit run dashboard/app.py

Pages: Ranked flags · Flag details · What changed this week · Research log · How the score works
Reads weekly_flags / weekly_runs (scorer/weekly.py). Writes only research_log.
"""
from __future__ import annotations

import hmac
import html
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

PIPELINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PIPELINE))

from scorer import equation as eq  # noqa: E402
from scorer import weekly  # noqa: E402
from scorer.schema import open_db  # noqa: E402

st.set_page_config(page_title="Bloomfield Flags", page_icon="🏚️", layout="wide")

from dashboard import style as _style  # noqa: E402
_style.inject()  # visual theme only; no data or scoring logic

TERMS = [("l_loan_clock", "L", "Loan clock", "loan_clock"), ("o_opening", "O", "Opening delay", "opening_delay"),
         ("c_creative", "C", "Creative signals", "creative")]
EX_KEY = {"L": "L_loan_clock", "O": "O_opening", "C": "C_creative"}


# --- Researcher decisions (browser-local, per the approved demo behavior) ----------
# Weekly-list decisions are personal triage state: they live in the researcher's
# own browser (localStorage), never in the shared database. The LocalStorage
# constructor waits for the browser to answer with the real stored state before
# returning, so a first render can never overwrite stored decisions with an empty
# not-yet-loaded state. If the component is missing or fails, decisions fall back
# to the Streamlit session and the UI says "session only" instead of claiming
# browser persistence.
DECISION_LABEL = {"approved": "On weekly list", "digging": "Needs digging", "dismissed": "Dismissed"}
DECISION_BADGE = {"approved": ":green-badge[On weekly list]",
                  "digging": ":orange-badge[Needs digging]",
                  "dismissed": ":gray-badge[Dismissed]"}
_DECISIONS_KEY = "bloomfield-decisions-v1"
import time as _time
try:
    # The package's LocalStorage class raised silently on the live deployment
    # (its constructor polls st.session_state in a way that never completes
    # there). Use its declared component primitive directly, with bounded
    # hydration and an inspectable error surface instead.
    from streamlit_local_storage import _st_local_storage as _sls_raw
except Exception:  # optional dependency missing: session fallback
    _sls_raw = None
_LS_GETALL_KEY = "bloomfield-ls-getall-v1"
_LS_SET_KEY = "bloomfield-ls-set-v1"


def _ls_get_all():
    """The browser's full localStorage dict once it answers, else None.

    Only the first call of a session waits (bounded ~2s); the hydrated dict is
    cached afterwards. None means storage is unavailable: callers must label
    decisions as session-only, never browser-persisted.
    """
    cached = st.session_state.get("_ls_store_data")
    if isinstance(cached, dict):
        return cached
    if _sls_raw is None:
        return None
    try:
        v = _sls_raw(method="getAll", key=_LS_GETALL_KEY, default=None)
        if isinstance(v, dict):
            st.session_state["_ls_store_data"] = v
            return v
        for _ in range(20):  # browser answers over the websocket mid-run
            _time.sleep(0.1)
            v = st.session_state.get(_LS_GETALL_KEY)
            if isinstance(v, dict):
                st.session_state["_ls_store_data"] = v
                return v
    except Exception as e:
        st.session_state["_ls_error"] = f"{type(e).__name__}: {e}"
    return None


def _ls_set(item_key, value_json):
    """Write one key to browser localStorage. True when the call was accepted."""
    if _sls_raw is None:
        return False
    try:
        _sls_raw(method="setItem", itemKey=item_key, itemValue=value_json, key=_LS_SET_KEY)
        data = st.session_state.get("_ls_store_data")
        if isinstance(data, dict):
            data[item_key] = value_json
        return True
    except Exception as e:
        st.session_state["_ls_error"] = f"{type(e).__name__}: {e}"
        return False


def _storage():
    """Hydrated browser localStorage dict, or None when unavailable.

    Also maintains _ls_ok: True once the browser has answered and no write has
    failed since, False on any failure. The UI labels persistence from _ls_ok.
    """
    store = _ls_get_all()
    if "_ls_ok" not in st.session_state:
        st.session_state["_ls_ok"] = store is not None
    elif store is not None and not st.session_state.get("_ls_write_failed"):
        st.session_state["_ls_ok"] = True
    return store


def _decisions():
    """This browser's researcher decisions: {property_id: {"action":..., "note":...}}."""
    cache = st.session_state.setdefault("_decisions_cache", {})
    store = _storage()
    if store is not None:
        raw = store.get(_DECISIONS_KEY)
        try:
            data = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        if st.session_state.get("_decisions_dirty"):
            # Decisions changed while storage was unavailable: session wins,
            # then the merged state is pushed back to the browser once.
            merged = {**data, **cache}
            if merged != data:
                if _ls_set(_DECISIONS_KEY, json.dumps(merged)):
                    st.session_state["_ls_ok"] = True  # storage recovered
                    st.session_state["_ls_write_failed"] = False
                else:
                    # Still failing: keep the session copy, stay dirty and
                    # degraded so the UI keeps saying "session only".
                    st.session_state["_ls_ok"] = False
                    st.session_state["_decisions_cache"] = merged
                    return merged
            st.session_state["_decisions_dirty"] = False
            cache = merged
        else:
            cache = data
        st.session_state["_decisions_cache"] = cache
    return st.session_state["_decisions_cache"]


def _save_decisions(data):
    st.session_state["_decisions_cache"] = data
    if _storage() is not None:
        if _ls_set(_DECISIONS_KEY, json.dumps(data)):
            st.session_state["_decisions_dirty"] = False
            st.session_state["_ls_ok"] = True  # a real write landed: storage is healthy
            st.session_state["_ls_write_failed"] = False
            return
        # Hydrated but the write failed: degrade honestly so the UI stops
        # claiming browser persistence; the resync path retries.
        st.session_state["_ls_ok"] = False
        st.session_state["_ls_write_failed"] = True
    st.session_state["_decisions_dirty"] = True


def _set_decision(pid, action):
    # Demo behavior: clicking the active action again clears it.
    data = dict(_decisions())
    cur = dict(data.get(pid) or {})
    cur["action"] = None if cur.get("action") == action else action
    data[pid] = cur
    _save_decisions(data)


def _set_decision_note(pid):
    data = dict(_decisions())
    cur = dict(data.get(pid) or {})
    cur["note"] = st.session_state.get(f"dec-note-{pid}", "")
    data[pid] = cur
    _save_decisions(data)


def _persistence_wording():
    """Honest label for where decisions are kept right now."""
    if st.session_state.get("_ls_ok"):
        return "saved in this browser only"
    return "saved for this session only (browser storage unavailable)"


def score_bands(hot, watch):
    """Display buckets for a stored model score. The bands mirror the model's internal
    tier thresholds (passed in from the run config) named with the user-facing
    tier words (high priority / watch / low), not the internal formula labels;
    unscored stays explicit because missing data is not proof of absence."""
    return [f"High priority: score {hot:.0f} or higher",
            f"Watch: score {watch:.0f} to under {hot:.0f}",
            f"Low: score under {watch:.0f}", "Unscored"]


def score_band(score, hot, watch):
    if score is None or pd.isna(score):
        return "Unscored"
    if score >= hot:
        return score_bands(hot, watch)[0]
    if score >= watch:
        return score_bands(hot, watch)[1]
    return score_bands(hot, watch)[2]

# One strong color per score band, drawn from the app palette (navy, maize, cool
# grays), so high priority, watch and low scan differently at a glance. The maize
# chip carries navy text (and vice versa) because maize text on white is unreadable.
BAND_CHIP = {
    0: ("#00274C", "#FFCB05", "\u25b2 HIGH PRIORITY"),
    1: ("#FFCB05", "#00274C", "\u25cf WATCH"),
    2: ("#E5E7EB", "#4B5563", "\u25bc LOW"),
}


def _chip_html(bg, fg, label):
    return (f"<span style='display:inline-block;background:{bg};color:{fg};"
            f"padding:2px 10px;border-radius:10px;font-size:12px;font-weight:700;"
            f"letter-spacing:0.4px'>{label}</span>")


def tier_chip(score, hot, watch):
    """Colored band chip for one card. Called only for scored properties."""
    if score >= hot:
        bg, fg, label = BAND_CHIP[0]
    elif score >= watch:
        bg, fg, label = BAND_CHIP[1]
    else:
        bg, fg, label = BAND_CHIP[2]
    return _chip_html(bg, fg, label)


def band_score_color(score, hot, watch):
    """Score number tint matching its band chip (maize darkened for white cards)."""
    if score >= hot:
        return "#00274C"
    if score >= watch:
        return "#8A6D00"
    return "#6B7280"


def display_summary(text):
    """Stored run summaries can carry an internal standout marker as its own sentence.
    It is formula bookkeeping, not a review verdict, so it is stripped for display
    (the stored row is unchanged). A summary that was ONLY the marker gets an honest
    placeholder."""
    t = re.sub(r"\bSTANDOUT\.\s*", "", (text or "").strip())
    t = re.sub(r"\s{2,}", " ", t).replace(". .", ". ").strip()
    return t or "Flagged on a single strong model signal - see the equation below for the basis."


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
def db_path():
    return os.environ.get("BLOOMFIELD_DB") or None


def get_con():
    if "con" not in st.session_state:
        con = open_db(db_path())
        weekly.create_tables(con)
        st.session_state.con = con
    return st.session_state.con


def runs(con):
    return [r[0] for r in con.execute("SELECT run_id FROM weekly_runs ORDER BY as_of DESC").fetchall()]


def flags(con, run_id):
    return con.execute("SELECT * FROM weekly_flags WHERE run_id = ? ORDER BY rank NULLS LAST, score DESC",
                       [run_id]).df()


def run_cfg(con, run_id):
    return json.loads(con.execute("SELECT settings_json FROM weekly_runs WHERE run_id = ?", [run_id]).fetchone()[0])


def latest_status(con):
    return dict(con.execute("""
        SELECT property_id, status FROM (SELECT property_id, status,
          row_number() OVER (PARTITION BY property_id ORDER BY logged_at DESC) n FROM research_log) WHERE n = 1
    """).fetchall())


def money(x):
    if x is None or pd.isna(x):
        return "size unknown"
    return f"${x / 1e6:.1f}M" if x >= 1e6 else f"${x:,.0f}"


# ---------------------------------------------------------------------------
# Token-gated research-ingest console (agent/ingest.py). Not linked anywhere;
# reachable only at ?ingest_key=<BLOOMFIELD_INGEST_KEY> while BLOOMFIELD_INGEST=1.
# Lets the authorized researcher download the flagged list (with property IDs)
# and upload evidence batches onto the volume, where the ingest thread verifies
# each row (fetch the cited page, verbatim-quote check) before storing it.
# No LLM calls and no paid search anywhere on this path.
# ---------------------------------------------------------------------------
def maybe_ingest_console():
    key = st.query_params.get("ingest_key")
    if key is None:
        return
    expected = os.environ.get("BLOOMFIELD_INGEST_KEY", "")
    st.title("Research ingest console")
    if os.environ.get("BLOOMFIELD_INGEST") != "1" or not expected or not hmac.compare_digest(str(key), expected):
        st.error("Not authorized.")
        st.stop()
    con = get_con()
    all_runs = runs(con)
    if all_runs:
        run_id = all_runs[0]
        df = flags(con, run_id)
        shown = df[df["shown"]]
        st.subheader(f"Flagged list - {len(shown)} shown flags (run {run_id})")
        cols = [c for c in ["property_id", "rank", "property_name", "address", "city", "state",
                            "property_type", "borrower", "lender", "loan_amount", "score", "tier",
                            "deal_quality", "est_value", "ltv", "filter_reason", "summary", "explanation"]
                if c in shown.columns]
        st.download_button(f"Download flagged list (CSV, {len(shown)} rows)", shown[cols].to_csv(index=False),
                           f"flagged-{run_id}.csv", "text/csv")
        st.download_button(f"Download FULL universe (CSV, {len(df)} rows)", df[cols].to_csv(index=False),
                           f"universe-{run_id}.csv", "text/csv")
    st.subheader("Research ledger (latest assessment per property)")
    try:
        ra = con.execute(
            "SELECT property_id, confidence, coverage, "
            "one_line, generated_at, ingested_at FROM research_assessments "
            "QUALIFY row_number() OVER (PARTITION BY property_id ORDER BY ingested_at DESC) = 1 "
            "ORDER BY property_id").fetchall()
        if ra:
            radf = pd.DataFrame(ra, columns=["property_id", "confidence", "coverage",
                                             "one_line", "generated_at", "ingested_at"])
            st.download_button(f"Download research ledger (CSV, {len(radf)} properties)",
                               radf.to_csv(index=False), "research-ledger.csv", "text/csv")
        else:
            st.caption("No research assessments yet.")
    except Exception as e:
        st.caption(f"Could not read the research ledger: {e}")
    st.subheader("Database snapshot")
    try:
        p = db_path()
        if not p or not os.path.exists(p):
            st.caption("Database file path not available on this deployment.")
        else:
            st.caption("Runs CHECKPOINT so the file is consistent, then offers it for download.")
            if st.button("Prepare snapshot"):
                con.execute("CHECKPOINT")
                with open(p, "rb") as fh:
                    st.session_state["db_snapshot"] = fh.read()
            if "db_snapshot" in st.session_state:
                st.download_button(
                    f"Download live DB snapshot ({len(st.session_state['db_snapshot']) / 1e6:.1f} MB)",
                    st.session_state["db_snapshot"], "live-snapshot.duckdb", "application/octet-stream")
    except Exception as e:
        st.caption(f"Snapshot failed: {e}")
    st.subheader("Plain-English deal reads (top-ranked flags)")
    st.caption("JSON array of {rank, property_id, name, score, summary_md}; each row replaces the "
               "stored read for that property. Shown above the equation on the property page. "
               "Properties absent from the file keep their existing read.")
    dx_file = st.file_uploader("Deal reads JSON", type=["json"], key="dx_file")
    if st.button("Store deal reads", key="dx_go") and dx_file is not None:
        try:
            rows = json.loads(dx_file.getvalue().decode("utf-8"))
            if not isinstance(rows, list) or not rows:
                raise ValueError("expected a non-empty JSON array")
            # Validate EVERY row before touching the table: a bad file must land nothing.
            for i, row in enumerate(rows):
                if not isinstance(row, dict) or not row.get("property_id") \
                        or not isinstance(row.get("rank"), int) \
                        or not isinstance(row.get("summary_md"), str) or not row["summary_md"].strip():
                    raise ValueError(f"row {i + 1}: missing or invalid property_id, rank or summary_md")
            if not all_runs:
                raise ValueError("no weekly run exists to scope the reads to")
            load_run = all_runs[0]
            con.execute("CREATE TABLE IF NOT EXISTS deal_explanations ("
                        "property_id TEXT PRIMARY KEY, rank INTEGER, name TEXT, score DOUBLE, "
                        "summary_md TEXT, run_id TEXT, loaded_at TEXT)")
            con.execute("BEGIN")
            try:
                for row in rows:
                    con.execute("DELETE FROM deal_explanations WHERE property_id = ?", [row["property_id"]])
                    con.execute("INSERT INTO deal_explanations VALUES (?,?,?,?,?,?,?)",
                                [row["property_id"], int(row["rank"]), row.get("name", ""), row.get("score"),
                                 row["summary_md"], load_run,
                                 datetime.now(timezone.utc).isoformat(timespec="seconds")])
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
            st.success(f"stored {len(rows)} deal reads for run {load_run}; the property pages show "
                       "them immediately.")
        except Exception as e:
            st.error(f"deal reads load failed: {type(e).__name__}: {e}")
    st.subheader("Case studies (memo-based examples)")
    st.caption("JSON array of {case_id, position, title, subtitle, body_md}; each row replaces the stored "
               "case with the same case_id. Shown on the Case studies page. Case text is deal-memo content: "
               "it lives only in this database, never in the code repository.")
    cs_file = st.file_uploader("Case studies JSON", type=["json"], key="cs_file")
    if st.button("Store case studies", key="cs_go") and cs_file is not None:
        try:
            rows = json.loads(cs_file.getvalue().decode("utf-8"))
            if not isinstance(rows, list) or not rows:
                raise ValueError("expected a non-empty JSON array")
            for i, row in enumerate(rows):
                if not isinstance(row, dict) or not isinstance(row.get("case_id"), str) or not row["case_id"].strip() \
                        or not isinstance(row.get("position"), int) or not isinstance(row.get("title"), str) \
                        or not row["title"].strip() or not isinstance(row.get("body_md"), str) \
                        or not row["body_md"].strip():
                    raise ValueError(f"row {i + 1}: missing or invalid case_id, position, title or body_md")
            con.execute("CREATE TABLE IF NOT EXISTS case_studies ("
                        "case_id TEXT PRIMARY KEY, position INTEGER, title TEXT, subtitle TEXT, "
                        "body_md TEXT, loaded_at TEXT)")
            con.execute("BEGIN")
            try:
                for row in rows:
                    con.execute("DELETE FROM case_studies WHERE case_id = ?", [row["case_id"]])
                    con.execute("INSERT INTO case_studies VALUES (?,?,?,?,?,?)",
                                [row["case_id"], row["position"], row["title"], row.get("subtitle") or "",
                                 row["body_md"], datetime.now(timezone.utc).isoformat(timespec="seconds")])
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
            st.success(f"stored {len(rows)} case studies; the Case studies page shows them immediately.")
        except Exception as e:
            st.error(f"case studies load failed: {type(e).__name__}: {e}")
    st.subheader("Upload a verified-evidence batch (JSONL)")
    st.caption("One JSON object per line: id, property_id, claim, value, url, quote, notes, summary. "
               "The ingest thread fetches each cited page and stores the claim only if the quote "
               "appears verbatim on the saved page - the same standard the research agent is held to.")
    up = st.file_uploader("JSONL batch", type=["jsonl"])
    if up is not None:
        from agent import ingest
        dest = ingest.incoming_dir(db_path()) / (
            f"ingest-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}.jsonl")
        dest.write_bytes(up.getvalue())
        st.success(f"Saved {dest.name}. The ingest thread verifies and stores each row within about "
                   "a minute; refusals and fetch failures are listed in the ingest_rows table.")
    st.subheader("SEC EX-102 backfill (official filings -> loans + operating metrics)")
    st.caption("For manually obtained SEC ABS-EE Exhibit 102 XML. A file named like an existing "
               "sec-edgar-doc-YYYY-NNNNN record tops up that record (sha256 checked against raw_files); "
               "anything else is stored as a NEW filing under a server-generated id - upload names are "
               "never used as paths. The database file is backed up before any parsing, and only the "
               "edgar backfill writes scorer inputs from these files.")
    bf_deal = st.text_input("Deal/trust name for new filings (one deal per batch, e.g. BMO 2024-5C4)", key="bf_deal")
    bf_prov = st.text_area("Optional provenance for new filings, one per line: filename accession source-url",
                           key="bf_prov", height=80)
    bf_files = st.file_uploader("EX-102 XML files", type=["xml"], accept_multiple_files=True, key="bf_files")
    if st.button("Store files + run edgar backfill", key="bf_go") and bf_files:
        from dashboard import sec_backfill
        try:
            prov = {}
            for line in bf_prov.splitlines():
                parts = line.split()
                if len(parts) == 3:
                    prov[parts[0]] = (parts[1], parts[2])
            report = sec_backfill.stage_files(con, [(f.name, f.getvalue()) for f in bf_files],
                                              deal=bf_deal, provenance=prov)
            st.success(f"stored {len(report['written'])}, already present {len(report['skipped'])}: "
                       + ", ".join(report["written"] + report["skipped"]))
            res = sec_backfill.run_backfill(con, Path(db_path()).resolve())
            st.success(f"backfill parsed {res['parsed']} file(s); status {res['status']}; backup {res['backup']}")
            if res["errors"]:
                st.warning("some files failed: " + "; ".join(str(e)[:160] for e in res["errors"][:5]))
        except Exception as e:
            st.session_state.pop("con", None)   # reopen cleanly (covers the restore path)
            st.error(f"backfill aborted: {type(e).__name__}: {e}")
            st.stop()
    st.divider()
    st.subheader("Recompute one property with the current model")
    st.caption("Re-scores a single weekly_flags row with the code and inputs as they are now - the same "
               "pipeline the weekly run uses. Every other property keeps its stored score until the "
               "full recompute.")
    bf_pid = st.text_input("property_id", key="bf_pid")
    if st.button("Recompute this property only", key="bf_one") and bf_pid.strip():
        try:
            out = weekly.rescore_one(con, bf_pid.strip())
            st.success(f"{out['name']}: score {out['score']} - {out['tier']} / {out['stage']} "
                       f"(early {out['early']} + late {out['late']})")
        except Exception as e:
            st.error(f"recompute failed: {type(e).__name__}: {e}")
    st.divider()
    st.subheader("Full weekly scoring run (all properties)")
    st.caption("Runs the same scorer.weekly entrypoint as the command line (python -m scorer.weekly) "
               "inside this process - the one process allowed to write the database. Scoring only: "
               "no collectors, no AI reader, no scheduler, no network or paid calls. Take the database "
               "snapshot above first; it is the rollback. The run takes about a minute and the page "
               "shows the result when it finishes.")
    if st.button("Run full weekly scoring now", key="bf_all"):
        try:
            from datetime import date as _date
            new_run = weekly.run(con, None, _date.today(), include_demo=None)
            fs = weekly.filter_summary(con, new_run)
            ch = weekly.changes(con, new_run)
            st.success(f"run {new_run}: {fs['shown']} shown, {fs['hidden']} hidden; "
                       f"since {ch['previous']}: {len(ch['new'])} new, {len(ch['worse'])} worse, "
                       f"{len(ch['better'])} better, {len(ch['dropped'])} dropped")
        except Exception as e:
            st.error(f"weekly run failed: {type(e).__name__}: {e}")
    st.subheader("Recent ingest rows")
    st.caption("Assessment rows (type=research_assessment) store the research-lane judgment; evidence "
               "rows are stored only after the quote is found verbatim on the fetched page.")
    try:
        led = con.execute(
            "SELECT id, property_id, status, detail, processed_at FROM ingest_rows "
            "ORDER BY processed_at DESC LIMIT 60").fetchall()
        if led:
            st.dataframe(pd.DataFrame(led, columns=["id", "property_id", "status", "detail", "processed_at"]),
                         hide_index=True, use_container_width=True)
        else:
            st.caption("Nothing ingested yet.")
    except Exception as e:
        st.caption(f"Could not read the ingest ledger: {e}")
    st.stop()


def esc(x):
    """Records come from outside documents: escape before showing."""
    return "" if x is None or (isinstance(x, float) and pd.isna(x)) else html.escape(str(x))


def research_assessments(con) -> dict:
    """Latest research-lane assessment per property: {property_id: (score, tier)}.
    Provisional judgment under rubric v2 - never the deterministic formula score."""
    try:
        rows = con.execute(
            "SELECT property_id, research_score, research_tier, one_line FROM research_assessments "
            "QUALIFY row_number() OVER (PARTITION BY property_id ORDER BY ingested_at DESC) = 1").fetchall()
        # tier stored may carry a flag suffix ("Low (pre-default ...)"); the word is the tier
        # null-score rows are explicit unscored/conflict markers: researched properties whose
        # filings cannot yet be reconciled into one honest number. Visible on the property
        # page; never counted as reviewed research and never a displayed score.
        return {r[0]: (r[1], (r[2] or "").split(" ")[0], r[3] or "") for r in rows if r[1] is not None}
    except Exception:
        return {}


def conflict_properties(con) -> set:
    """property_ids whose latest research assessment is an explicit unscored/conflict marker."""
    try:
        rows = con.execute(
            "SELECT property_id FROM research_assessments "
            "QUALIFY row_number() OVER (PARTITION BY property_id ORDER BY ingested_at DESC) = 1 "
            "AND research_score IS NULL").fetchall()
        return {r[0] for r in rows}
    except Exception:
        return set()


def assessed_only_properties(con, flag_ids: set) -> dict:
    """property_id -> display name for reviewed properties NOT in this weekly run
    (e.g. senior-housing reviews). Same resolution chain as the ingest path:
    property_links -> projects, then weekly_flags, then projects."""
    out = {}
    try:
        pids = [r[0] for r in con.execute("SELECT DISTINCT property_id FROM research_assessments").fetchall()]
    except Exception:
        return out
    for p in pids:
        if p in flag_ids:
            continue
        name = None
        for q in ("SELECT p.project_name FROM property_links pl "
                  "JOIN projects p ON p.project_id = pl.record_id AND pl.record_table = 'projects' "
                  "WHERE pl.property_id = ? LIMIT 1",
                  "SELECT property_name FROM weekly_flags WHERE property_id = ? LIMIT 1",
                  "SELECT project_name FROM projects WHERE project_id = ? LIMIT 1"):
            try:
                row = con.execute(q, [p]).fetchone()
            except Exception:
                row = None
            if row and row[0]:
                name = row[0]
                break
        out[p] = name or p
    return out


def _evidence_caveat(claim) -> str:
    """Inline honesty label: quote-matching proves the words are on the page, not that
    the claim is current or true. Allegations, leads and dated news stay labeled."""
    c = (claim or "").lower()
    if "allegation" in c:
        return "  :orange[allegation - a request or claim, not an adjudicated finding]"
    if "lead" in c:
        return "  :orange[unconfirmed lead - identity or facts not fully established]"
    if "news" in c:
        return "  :gray[dated news report - may not reflect current conditions]"
    return ""


def deal_explanations(con, run_id):
    """Plain-English investment reads for the top-ranked flags, written to live storage
    by the ops console (never the repo - they are deal memos, not code). Scoped to the
    displayed run: a read written against one week's score and evidence is never shown
    against a different week's. {property_id: (rank, summary_markdown)}; empty when the
    table is absent."""
    try:
        rows = con.execute("SELECT property_id, rank, summary_md FROM deal_explanations "
                           "WHERE run_id = ?", [run_id]).fetchall()
    except Exception:
        return {}
    return {pid: (rk, md) for pid, rk, md in rows}


def open_flag(pid):
    st.session_state.selected = pid
    st.session_state.selected_request = pid
    st.session_state.page = "Flag details"


def tags(r, assessed=False, r_tier=""):
    out = []
    if "COUNTER-SIGNAL" in (r["flags"] or ""):
        out.append(":blue-badge[↩ COUNTER-SIGNAL (formula)]")
    if assessed:
        # One score only: the card shows the model score; research stays a neutral
        # "on file" marker, never a second score or tier.
        out.append(":gray-badge[Reviewed research on file]")
        return " ".join(out)
    if r["stage"] == "ready_to_act":
        out.append(":red-badge[✅ READY TO ACT (formula, unreviewed)]")
    elif r["score"] and r["score"] > 0:
        out.append(":green-badge[Early warning (formula, unreviewed)]")
    return " ".join(out)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
def research_status(con) -> str:
    """One honest line about the sourcing agent's work on THIS database: how many research
    goals finished, how much verified evidence they saved, and the estimated model cost so
    far. Shown on the weekly list so research coverage is visible next to the ranks. The
    agent writes its own tables; if they do not exist here, it has never run on this data."""
    try:
        goals, ev = con.execute(
            "SELECT (SELECT count(*) FROM agent_runs WHERE status IN ('done', 'step_cap')), "
            "       (SELECT count(*) FROM evidence)").fetchone()
        usd = con.execute("SELECT COALESCE(sum(est_cost_usd), 0) FROM llm_usage WHERE task = 'agent'").fetchone()[0]
    except Exception:
        goals, ev, usd = 0, 0, 0.0
    reviewed = len(research_assessments(con))
    n_conf = len(conflict_properties(con))
    conf_tail = (f"; {n_conf} conflict{'s' if n_conf != 1 else ''} under review, "
                 "unscored until reconciled" if n_conf else "")
    tail = (f" · Properties with reviewed research: {reviewed} propert{'y' if reviewed == 1 else 'ies'} "
            "(research assessments are context on each page; depth varies - each page shows its coverage" + conf_tail + ")"
            if reviewed else "")
    if not goals:
        return ("🔎 Automated first pass (earlier, unreviewed): has not run on this database yet "
                "(0 properties researched, 0 evidence records)." + tail)
    return (f"🔎 Automated first pass (earlier, unreviewed): {goals} propert{'y' if goals == 1 else 'ies'} researched · "
            f"{ev} evidence record{'s' if ev != 1 else ''} · model usage ≈ ${usd:.2f} (estimate)" + tail)


def research_diagnostics(con) -> str:
    """Non-secret live diagnostics for the research pass: whether the search service and the
    model key are CONFIGURED (yes/no only - never the values), the latest pass row, the most
    recent goals and the most recent FAILED tool calls (which carry the plain-words reason,
    e.g. 'No search service is configured'). Read-only; safe to show on the public app."""
    lines = []
    try:
        import yaml
        bot_cfg = yaml.safe_load((PIPELINE / "config" / "bot.yaml").read_text()) or {}
        provider = ((bot_cfg.get("search") or {}).get("provider", "none"))
    except Exception as e:
        provider = f"unreadable ({e})"
    key_env = "TAVILY_API_KEY" if provider == "tavily" else "BRAVE_SEARCH_API_KEY"
    key_set = bool(os.environ.get(key_env, "").strip())
    try:
        from collectors.common import config as _c  # loads .env into os.environ
        _c.load_env()
    except Exception:
        pass
    llm_set = any(os.environ.get(n, "").strip()
                  for n in ("LLM_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"))
    lines.append(f"search provider: {provider} · {key_env} set: {'yes' if key_set else 'NO'} · "
                 f"LLM key set: {'yes' if llm_set else 'NO'}")
    try:
        row = con.execute("SELECT started_at, status, goals_run, goals_skipped, evidence_new, "
                          "llm_calls, search_calls FROM weekly_research_passes ORDER BY started_at DESC "
                          "LIMIT 1").fetchone()
        if row:
            lines.append(f"latest pass: started {row[0]} · status {row[1]} · goals {row[2]} "
                         f"(skipped {row[3]}) · new evidence {row[4]} · llm calls {row[5]} · searches {row[6]}")
    except Exception:
        pass
    try:
        nfiles = con.execute("SELECT count(*) FROM raw_files").fetchone()[0]
        cruns = con.execute("SELECT collector_name, status, started_at, files_new, errors "
                            "FROM collector_runs ORDER BY started_at DESC LIMIT 4").fetchall()
        lines.append(f"collected documents in this database: {nfiles}")
        for c in cruns:
            lines.append(f"collector {c[0]}: {c[1]} at {c[2]} · {c[3]} new · {c[4]} errors")
    except Exception:
        pass
    try:
        runs = con.execute("SELECT started_at, status, llm_calls, cost_usd, "
                           "substr(goal, 1, 90), substr(coalesce(final_answer, ''), 1, 220) "
                           "FROM agent_runs ORDER BY started_at DESC LIMIT 3").fetchall()
        for r in runs:
            lines.append(f"goal {r[0]}: {r[1]} · {r[2]} calls · ${r[3]:.4f} · {r[4]}")
            if r[5]:
                lines.append(f"  last words: {r[5]}")
    except Exception:
        pass
    try:
        fails = con.execute("SELECT created_at, name, substr(detail, 1, 220) FROM agent_steps "
                            "WHERE kind = 'tool_result' AND detail LIKE '%\"ok\": false%' "
                            "ORDER BY created_at DESC LIMIT 3").fetchall()
        for f_ in fails:
            lines.append(f"FAILED tool {f_[1]} at {f_[0]}: {f_[2]}")
    except Exception:
        pass
    try:
        evs = con.execute("SELECT subject, claim, source_url FROM evidence "
                          "ORDER BY observed_at DESC LIMIT 3").fetchall()
        for e in evs:
            lines.append(f"latest evidence: {e[0]} - {e[1]} - {e[2]}")
    except Exception:
        pass
    return "\n".join(lines) if len(lines) > 1 else lines[0]


def page_ranked(con, run_id):
    df, cfg = flags(con, run_id), run_cfg(con, run_id)
    f = cfg["filters"]
    status = latest_status(con)
    st.title("This week's flags")
    st.caption(f"Bloomfield Opportunity Score {cfg['version']} · {run_id.replace('week-', 'week of ')} · "
               f"{(', '.join(f['property_types_excluded']) + ' hidden') if f['property_types_excluded'] else 'all property types shown'} · loans from {money(f['min_loan_size'])}"
               f"{' to ' + money(f['max_loan_size']) if f.get('max_loan_size') else ' (no upper limit)'} · {'states ' + ', '.join(f['states']) if f['states'] else 'all U.S.'}")
    try:
        from collectors.common import db as _cdb
        _scoped = _cdb.get_state(con, "dashboard", "scoped_recompute")
        if _scoped:
            _info = json.loads(_scoped)
            st.info(f"{_info['name']} was re-scored with the current model at {_info['at']} "
                    f"(score {_info['score']} · {_info['tier']}). Every other property still shows "
                    "the stored run; a full recompute follows the input-validation checks.")
    except Exception:
        pass
    with st.expander("Research diagnostics"):
        st.text(research_diagnostics(con))
        if st.session_state.get("_ls_error"):
            st.text(f"Browser storage component: {st.session_state['_ls_error']}")
    hide_office = st.toggle("Hide office properties", value=False,
                            help="Office is shown by default. Turn this on to filter it out of the list and the counts.")
    if hide_office:
        df = df[df["property_type"].fillna("") != "Office"]
    decisions = _decisions()
    approved_count = sum(1 for d in decisions.values() if (d or {}).get("action") == "approved")
    only_mine = st.toggle(f"Only my weekly list ({approved_count})", value=False,
                          help="Show only the properties you added to your weekly list. "
                               f"The list is {_persistence_wording()}.")
    ranked = df[df["rank"].notna()]
    live = eq.load_equation_config()["filters"]
    if {k: f.get(k) for k in ("min_loan_size", "max_loan_size", "property_types_excluded")} != \
            {k: live.get(k) for k in ("min_loan_size", "max_loan_size", "property_types_excluded")}:
        st.warning("This run was made with older filter settings than the ones now in config/equation.yaml "
                   f"(it used loans {money(f['min_loan_size'])}–{money(f['max_loan_size']) if f.get('max_loan_size') else 'no limit'}, "
                   f"hiding {', '.join(f['property_types_excluded']) or 'nothing'}). "
                   "Pull the latest code and run `python -m scorer.weekly` again to apply the current settings.")
    if len(ranked) and not (ranked["score"] > 0).any():
        st.info("None of these properties has an early warning sign yet. They are listed because they pass the filters; "
                "they are known only from public loan data. Add county records or run the web bot to find signals.")
    # Displayed scores/tiers/ranking are the chosen scoring model's current run
    # (Bloomfield Opportunity Score rows in weekly_flags). Reviewed research
    # assessments stay as context (counts, badges, per-property narrative);
    # they never replace the model score.
    rassess = research_assessments(con)
    conflicts = conflict_properties(con)
    ranked = ranked.assign(
        _eff_score=ranked["score"],
        _eff_tier=ranked["tier"].map(lambda t: ((t or "unscored") if isinstance(t, str) else "unscored").lower().split(" ")[0]))
    c = st.columns(4)
    c[0].metric("Listed", len(ranked))
    c[1].metric("Scored (this run)", int(ranked["_eff_score"].notna().sum()))
    c[2].metric("Reviewed research on file", int(ranked["property_id"].isin(list(rassess)).sum()))
    c[3].metric("Filtered out", int((~df["shown"]).sum()))
    if rassess:
        st.caption("Scores are the scoring model's latest stored run (Bloomfield "
                   "Opportunity Score formula, recomputed with the current model and verified "
                   "SEC inputs). Reviewed AI research stays as context on each "
                   "property page; it does not set the displayed score. "
                   "Coverage depth varies by property; each assessment lists its checked lanes. "
                   f"{len(rassess)} properties have "
                   "been reviewed in total, including unflagged senior-housing properties (open them "
                   "from the Flag details page)." +
                   (f" {len(conflicts)} conflict{'s' if len(conflicts) != 1 else ''} under review - "
                    "unscored until the filings reconcile, never counted as reviewed research."
                    if conflicts else ""))

    if len(ranked) == 0 and len(df) and not df["shown"].any():
        why = df["filter_reason"].fillna("unknown").value_counts()
        st.warning("Every flag is hidden by the filters. Reasons: " + "; ".join(f"{n} × {r}" for r, n in why.items())
                   + ". Turn on 'Show filtered-out' to see them, or change `filters:` in config/equation.yaml.")

    assessed_first = ranked.assign(_a=ranked["_eff_score"].notna()).sort_values(
        ["_a", "_eff_score", "score"], ascending=[False, False, False])
    _tiers = cfg["tiers"]
    _hot, _watch = float(_tiers["hot"]), float(_tiers["watch"])
    _bands = score_bands(_hot, _watch)
    # Band totals in the same chip colors, so the mix is visible before any filtering.
    _cnt = ranked["_eff_score"].map(lambda v: score_band(v, _hot, _watch)).value_counts()
    st.markdown(" &nbsp; ".join(
        _chip_html(bg, fg, f"{label} \u00b7 {int(_cnt.get(_bands[i], 0))}")
        for i, (bg, fg, label) in BAND_CHIP.items()),
        unsafe_allow_html=True)
    band_pick = st.multiselect("Score bands", _bands, default=_bands)
    view = assessed_first[assessed_first["_eff_score"].map(lambda v: score_band(v, _hot, _watch)).isin(band_pick)]
    if only_mine:
        view = view[view["property_id"].map(lambda p: (decisions.get(p) or {}).get("action") == "approved")]
    # Cards are expensive to render; hundreds of rows kept the script "Running" for
    # minutes and every button stayed a skeleton. Show the strongest flags as cards
    # and point to the All properties table for the full scored universe.
    MAX_CARDS = 50
    if len(view) > MAX_CARDS:
        st.caption(f"Showing the top {MAX_CARDS} of {len(view)} ranked flags. "
                   "The **All properties** page lists every scored property.")
        view = view.head(MAX_CARDS)
    if (df["rank"].isna()).any():
        st.caption(f"{int(df['rank'].isna().sum())} more properties were screened by the deterministic formula "
                   "(no distress signals or context found yet) and not yet reviewed in depth "
                   "(missing data is not proof of absence). They are on the **All properties** page.")
    for pos, r in enumerate(view.to_dict("records"), start=1):
        with st.container(border=True):
            a, b, c3 = st.columns([0.7, 5, 1.8])
            a.markdown(f"### {pos}")
            place = ", ".join(x for x in [r["city"], r["state"]] if x and not pd.isna(x))
            b.markdown(f"**{esc(r['property_name'])}** — {esc(r['property_type'] or 'type unknown')} · "
                       f"{esc(place)} · {money(r['loan_amount'])}")
            eff_score, eff_tier = r["_eff_score"], r["_eff_tier"]
            assessed = pd.notna(eff_score) and eff_score is not None
            has_research = r["property_id"] in rassess
            if assessed:
                b.markdown(tier_chip(eff_score, _hot, _watch), unsafe_allow_html=True)
            b.markdown(tags(r, has_research, eff_tier))
            _dec = (decisions.get(r["property_id"]) or {}).get("action")
            if _dec in DECISION_BADGE:
                b.markdown(DECISION_BADGE[_dec])
            if has_research:
                one = rassess.get(r["property_id"], (None, None, ""))[2]
                b.caption(esc(one) if one else "Reviewed research on file - see the assessment on the property page.")
            elif r["property_id"] in conflicts:
                b.caption("Conflicting filings under review - no honest single score yet; see the property page.")
            else:
                parts = [f"{code} {r[col]:.0%}" for col, code, _, _ in TERMS if r.get(col) and r[col] > 0]
                b.caption(("Model signals: " + " · ".join(parts)) if parts else ("No model signals (late filings only)" if r["score"] and r["score"] > 0 else "No model signals found in available public data - unknown, not a clean bill of health"))
            if not r["shown"]:
                b.markdown(f":gray[Hidden: {esc(r['filter_reason'])}]")
            if r["property_id"] in status:
                b.markdown(f":green[Research: {weekly.STATUS_LABELS[status[r['property_id']]]}]")
            if assessed:
                c3.markdown(f"<span style='color:{band_score_color(eff_score, _hot, _watch)};"
                            f"font-size:20px;font-weight:700'>{eff_score:.0f}</span> / 100",
                            unsafe_allow_html=True)
                c3.progress(min(1.0, eff_score / 100))
                c3.caption("Model score (stored run)")
            elif r["property_id"] in conflicts:
                c3.markdown("**—** / 100")
                c3.progress(0.0)
                c3.caption("Filings conflict - under review, unscored")
            else:
                c3.markdown("**—** / 100")
                c3.progress(0.0)
                c3.caption("Not researched yet - not a clean bill of health")
            c3.caption(f"Deal quality {r['deal_quality']}")
            c3.button("Open", key=f"open-{r['property_id']}", on_click=open_flag, args=(r["property_id"],),
                      use_container_width=True)


def page_detail(con, run_id):
    df = flags(con, run_id)
    if df.empty:
        st.info("No flags in this run.")
        return
    ids = df["property_id"].tolist()
    names = dict(zip(df["property_id"], df["property_name"]))
    extra = assessed_only_properties(con, set(ids))
    names.update(extra)
    ids = ids + sorted(extra, key=lambda p: names[p].lower())
    # Unique labels keyed on property_id: name + address + loan amount disambiguate
    # rows that share a property name (each option routes to its own row).
    amt = dict(zip(df["property_id"], df["loan_amount"]))
    addr = dict(zip(df["property_id"], df["address"]))
    def _flag_label(k):
        base = names[k]
        if k in extra:
            return base
        av = addr.get(k)
        a = av.strip() if isinstance(av, str) else ""
        if len(a) > 42:
            a = a[:39].rstrip() + "..."
        v = amt.get(k)
        m = money(v) if isinstance(v, (int, float)) and not pd.isna(v) else ""
        parts = [base] + ([a] if a else []) + ([m] if m else [])
        return " - ".join(parts)
    # Widget keyed on property_id. External "Open" buttons set selected_request
    # (one-shot), applied to the widget state before the widget is created this
    # run; the widget itself owns the state afterwards, so a user's pick can
    # never be forced back to a stale value on rerun.
    req = st.session_state.pop("selected_request", None)
    if req in ids:
        st.session_state["flag_pid"] = req
    if st.session_state.get("flag_pid") not in ids:
        st.session_state["flag_pid"] = ids[0]
    pid = st.selectbox("Property", ids, key="flag_pid", format_func=_flag_label)
    st.session_state.selected = pid
    if pid in extra:
        # Reviewed property outside the weekly run: no formula audit row exists.
        # The reviewed assessment and evidence sections below work off property_id alone.
        r = {"property_id": pid, "property_name": names[pid],
             "property_type": "Senior housing / care", "address": "",
             "borrower": "", "lender": "", "flags": "", "stage": "", "score": None,
             "shown": True, "filter_reason": None, "est_value": None, "ltv": None,
             "loan_amount": None, "deal_quality": "-", "summary": ""}
        ex = None
    else:
        r = df[df["property_id"] == pid].iloc[0].to_dict()
        ex = json.loads(r["explanation"])
    cfg = run_cfg(con, run_id)
    w = cfg["weights"]

    st.title(r["property_name"])
    st.caption(f"{esc(r['property_type'] or 'type unknown')} · {esc(r['address'] or '')} · "
               f"borrower {esc(r['borrower']) or 'unknown'} · lender {esc(r['lender']) or 'unknown'}")
    st.markdown(tags(r))

    # Researcher decision: personal weekly-list triage saved in this browser only
    # (matches the approved demo controls: weekly list / digging / dismiss + note).
    _d = _decisions().get(pid) or {}
    _on = _d.get("action")
    with st.container(border=True):
        st.subheader("Researcher decision")
        b1, b2, b3 = st.columns(3)
        b1.button("\u2713 On the weekly list" if _on == "approved" else "Add to weekly list",
                  key=f"dec-approve-{pid}", on_click=_set_decision, args=(pid, "approved"),
                  type="primary" if _on == "approved" else "secondary", use_container_width=True)
        b2.button("Needs more digging" + (" \u2713" if _on == "digging" else ""),
                  key=f"dec-digging-{pid}", on_click=_set_decision, args=(pid, "digging"),
                  type="primary" if _on == "digging" else "secondary", use_container_width=True)
        b3.button("Dismiss" + (" \u2713" if _on == "dismissed" else ""),
                  key=f"dec-dismiss-{pid}", on_click=_set_decision, args=(pid, "dismissed"),
                  type="primary" if _on == "dismissed" else "secondary", use_container_width=True)
        st.text_area(f"Notes for the team ({_persistence_wording()})",
                     value=_d.get("note") or "", key=f"dec-note-{pid}",
                     on_change=_set_decision_note, args=(pid,))
        st.caption(f"Your decisions and notes are {_persistence_wording()} - never sent to the team "
                   "or stored in the shared database. "
                   "The tool never contacts anyone. Bloomfield handles all outreach.")

    ra_head = research_assessments(con).get(pid)
    conflict = pid in conflict_properties(con)
    c = st.columns(3)
    if r.get("score") is not None and not pd.isna(r["score"]):
        c[0].metric("Bloomfield Opportunity Score", f"{r['score']:.1f}")
    else:
        c[0].metric("Bloomfield Opportunity Score", "—")
    c[1].metric("Deal quality", r["deal_quality"] or "-")
    c[2].metric("Loan", money(r["loan_amount"]) if r.get("loan_amount") else "-")
    if ra_head:
        st.caption("Reviewed AI research on file for this property - the narrative, evidence and coverage "
                   "below are kept for context alongside the one displayed score above.")
    if conflict:
        st.warning("Conflicting filings under review - no honest single score yet. This is NOT a "
                   "scored or cleared review: the unreconciled debt scopes are laid out in the research "
                   "assessment below. The formula summary and equation further down are the automated "
                   "audit view only, not a reviewed score.")
    else:
        st.caption("Displayed score is the scoring model's latest stored run for this property "
                   "(Bloomfield Opportunity Score formula). Signals with no verified data are shown as "
                   "unknown, not as absence of risk; underwritten (UW) baselines are labeled as "
                   "underwriting, not history.")
    if ex and ex.get("recomputed_at"):
        st.caption(f"This property was re-scored with the current model at {ex['recomputed_at']} "
                   "after a verified SEC data import; properties without this note show the stored run.")
    if r.get("est_value") and not pd.isna(r["est_value"]):
        st.caption(f"Estimated stabilized value {money(r['est_value'])} (revenue at stabilized occupancy − latest "
                   f"12-month expenses, ÷ the {r['property_type']} cap rate, − cost to stabilize) · owed / value {r['ltv']:.0%}")
    if not r["shown"]:
        st.warning(f"Filtered out by settings: {r['filter_reason']}")
    if ex:
        if conflict:
            pass   # conflict notice above carries the status; the raw formula summary is not shown
        else:
            st.info(display_summary(r["summary"]))

        dx = deal_explanations(con, run_id).get(pid)
        if dx:
            st.subheader("Why this one could be a deal - in plain English")
            # The stored read was written for a top-50 document and opens with its own
            # "## N. Name" heading; the page title already carries the name, so drop it.
            md = re.sub(r"^##\s*\d+\.\s*[^\n]*\n+", "", (dx[1] or "").strip())
            st.markdown(md)
            st.caption("A plain-English read of this property's stored scoring run - context and "
                       "diligence questions, not a valuation, an appraisal or an offer.")

        st.subheader("The equation for this property")
        rows = []
        for col, code, label, key in TERMS:
            t = ex[EX_KEY[code]]
            rows.append({"Term": f"{code} · {label}", "Strength": t["strength"], "Weight": w[key],
                         "Points before anti-fluke": round(w[key] * t["strength"], 1), "Why": t["why"]})
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True,
                     column_config={"Strength": st.column_config.ProgressColumn(min_value=0, max_value=1, format="%.2f")})
        cs_rows = ex.get("creative_signals", [])
        if cs_rows:
            st.markdown("**Inside C: each creative signal** — C = 1 − Π(1 − reliability × strength)")
            st.dataframe(pd.DataFrame([{"Signal": f"{x['code']} · {x['name']}", "Strength": x["strength"],
                                        "Reliability": x["reliability"], "Counts as": x["effective"],
                                        "Why": x["why"] if x["known"] else "unknown - no verified data (not evidence of absence)"} for x in cs_rows]),
                         hide_index=True, use_container_width=True,
                         column_config={"Strength": st.column_config.ProgressColumn(min_value=0, max_value=1, format="%.2f")})
        a, b = st.columns(2)
        with a:
            st.markdown(f"**Anti-fluke:** {ex['anti_fluke']}")
            for s in ex.get("counter_signals", []):
                st.markdown(f"↩ **Counter-signal** (×{cfg['counter_signal']['factor']}): {esc(s)}")
            st.markdown(f"**Early points:** {ex['early_points']}")
        with b:
            st.markdown(f"**Ready-to-act confirmation (court/lien filings, max {cfg['late']['max_points']}):** {ex['late_points']}")
            for s in ex.get("late_why", []):
                st.markdown(f"- {esc(s)}")
            # Payment-status ladder (SEC-filed delinquency) plus quote-verified research events.
            # Part of the stored score since the research config shipped; shown so the sum reconciles.
            rcfg = cfg.get("research") or {}
            rmax = ((rcfg.get("payment") or {}).get("cap") or 0) + (rcfg.get("sponsor_cap") or 0) + (rcfg.get("physical_cap") or 0)
            rp = ex.get("research_points") or 0.0
            st.markdown(f"**Payment status + verified research (latest SEC filing / reviewed research, max {rmax}):** {rp}")
            for s in ex.get("research_why") or []:
                st.markdown(f"- {esc(s)}")
            st.markdown(f"**Score = min(100, {ex['early_points']} + {ex['late_points']} + {rp}) = {ex['score']}**")

        ctx = json.loads(r["context"])
        if ctx:
            st.subheader("Context (filed facts kept for reference)")
            st.caption("Filed facts repeated for reference. Where the score above already reads a filed fact "
                       "(such as SEC-filed payment status), it is reflected in that score - never added again "
                       "here. Auctions and CMBS data are public or everyone has them.")
            for s in ctx:
                st.markdown(f"- {esc(s)}")

        a, b = st.columns(2)
        with a:
            st.subheader("Why this deal quality")
            st.dataframe(pd.DataFrame(json.loads(r["deal_parts"])), hide_index=True, use_container_width=True)
        with b:
            st.subheader("Deal angle for Bloomfield")
            for s in json.loads(r["angles"]) or ["No clear angle yet: keep watching."]:
                st.markdown(f"- {s}")

    else:
        st.caption("This property is not part of the weekly flagged run, so there is no formula audit view - "
                   "the reviewed assessment below is the complete evaluation.")

    st.subheader("AI research")
    aruns = con.execute(
        "SELECT status, started_at, final_answer FROM agent_runs "
        "WHERE goal LIKE '%[property_id=' || ? || ']%' "
        "ORDER BY started_at DESC LIMIT 3", [pid]).fetchall()
    if not aruns:
        st.caption("The research agent has not covered this property yet.")
    for a_status, a_started, a_final in aruns:
        when = pd.Timestamp(a_started).strftime("%b %d %H:%M") + " UTC"
        if a_status == "running":
            st.markdown(f"**Researcher is working on this property now** (started {when}).")
        elif a_final:
            st.markdown(f"**Researcher ({when}, {esc(a_status)}):** {esc(a_final)}")
        else:
            st.markdown(f"**Researcher ({when}):** run ended with status `{esc(a_status)}`, no summary written.")
    ev_rows = con.execute(
        "SELECT e.claim, e.value, e.source_kind, e.source_url, e.quote, e.observed_at "
        "FROM evidence e JOIN agent_runs r ON r.run_id = e.run_id "
        "WHERE r.goal LIKE '%[property_id=' || ? || ']%' "
        "ORDER BY e.observed_at DESC", [pid]).fetchall()
    if ev_rows:
        st.markdown(f"**Source-matched evidence ({len(ev_rows)}):** the quote appears verbatim on the "
                    "saved page at the address shown. That proves the words are on that page - it does "
                    "NOT by itself prove the claim is current or true. Allegations, unconfirmed leads "
                    "and dated reports are labeled next to each row.")
        for e_claim, e_value, e_kind, e_url, e_quote, e_obs in ev_rows:
            val = f" = **{esc(str(e_value))}**" if e_value not in (None, "") else ""
            st.markdown(f"- {esc(e_claim)}{val}{_evidence_caveat(e_claim)}")
            if e_kind == "web_page":
                if isinstance(e_url, str) and e_url.startswith(("https://", "http://")):
                    st.caption(f'web proof: "{esc(e_quote)}" - {esc(e_url)} · {pd.Timestamp(e_obs):%b %d %H:%M} UTC')
                else:
                    st.caption(f'web proof: "{esc(e_quote)}" (source address on file) · {pd.Timestamp(e_obs):%b %d %H:%M} UTC')
            else:
                st.caption(f"database proof: {esc(e_url)} · {pd.Timestamp(e_obs):%b %d %H:%M} UTC")
    elif aruns:
        latest = aruns[0][0]
        if latest == "done":
            st.caption("The researcher completed this property and saved no verified evidence: "
                       "it found nothing it could verify, or sources blocked it. An honest empty "
                       "result, not a skipped property.")
        elif latest == "running":
            st.caption("Research in progress; no verified evidence saved yet.")
        else:
            st.caption(f"The last research run ended with status '{esc(latest)}' and saved no "
                       "verified evidence. The property stays pending and will be retried.")

    # Research-lane provisional assessment (rubric v2): a structured review of the evidence,
    # kept strictly separate from the deterministic formula score above.
    try:
        ra_row = con.execute(
            "SELECT research_score, research_tier, confidence, coverage, score_breakdown, "
            "score_reasons, artifact, generated_at FROM research_assessments "
            "WHERE property_id = ? ORDER BY ingested_at DESC LIMIT 1", [pid]).fetchone()
    except Exception:
        ra_row = None
    if ra_row:
        r_score, r_tier, r_conf, r_cov, r_bd, r_reasons, r_artifact, r_gen = ra_row
        st.subheader("Research review (context)")
        st.caption("A structured independent research review kept as context next to the one displayed "
                   "score above. Unchecked lanes are unknowns, never counted as healthy.")
        if r_score is None:
            st.markdown("**Conflicting filings under review - unscored until the filings reconcile.**")
        if r_conf:
            st.markdown(f"**Confidence:** {esc(r_conf)}")
        if r_cov:
            st.markdown(f"**Coverage:** {esc(r_cov)}")
        try:
            reasons = json.loads(r_reasons or "[]")
            if isinstance(reasons, list) and reasons:
                with st.expander("Reasons, qualifications and counter-evidence", expanded=True):
                    for reason in reasons:
                        st.markdown(f"- {esc(str(reason))}")
        except Exception:
            pass
        if r_gen:
            st.caption(f"Research lane · assessment generated {esc(r_gen)}")

    st.subheader("Research log")
    with st.form(f"log-{pid}", clear_on_submit=True):
        x, y = st.columns([2, 1])
        status = x.selectbox("What happened", weekly.RESEARCH_STATUSES, format_func=lambda s: weekly.STATUS_LABELS[s])
        who = y.text_input("Researcher", value=st.session_state.get("researcher", ""))
        note = st.text_area("Note", placeholder="e.g. Called the principal; open to a bridge loan.")
        amount = st.number_input("Deal amount ($, only for Deal closed)", min_value=0, step=100_000)
        if st.form_submit_button("Save to research log"):
            st.session_state.researcher = who
            con.execute("INSERT INTO research_log VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [
                uuid.uuid4().hex, pid, r["property_name"], status, note,
                float(amount) if status == "deal_closed" and amount else None, who or None,
                datetime.now(timezone.utc).replace(tzinfo=None)])
            st.success("Saved.")
    hist = con.execute("SELECT * FROM research_log WHERE property_id = ? ORDER BY logged_at DESC", [pid]).df()
    if hist.empty:
        st.caption("Nothing logged for this flag yet.")
    for h in hist.to_dict("records"):
        amt = f" · {money(h['deal_amount'])}" if pd.notna(h["deal_amount"]) else ""
        st.markdown(f"- **{weekly.STATUS_LABELS.get(h['status'], h['status'])}**{amt} — {esc(h['note'])} "
                    f":gray[({pd.Timestamp(h['logged_at']):%b %d, %Y}{', ' + esc(h['researcher']) if h['researcher'] else ''})]")


def _list(rows, prefix, extra=None):
    if not rows:
        st.caption("None.")
    for r in rows:
        a, b = st.columns([5, 1])
        line = f"**{esc(r['property_name'])}** — score {r['score']:.0f}"
        if extra:
            line += f" · {extra(r)}"
        a.markdown(line)
        a.caption(esc(display_summary(r["summary"])))
        b.button("Open", key=f"{prefix}-{r['property_id']}", on_click=open_flag, args=(r["property_id"],))


def page_changes(con, run_id):
    st.title("What changed this week")
    ch = weekly.changes(con, run_id, run_cfg(con, run_id))
    if not ch["previous"]:
        st.info("This is the first weekly run, so there's nothing to compare yet.")
        return
    st.caption(f"{ch['current'].replace('week-', 'Week of ')} compared with {ch['previous'].replace('week-', 'week of ')}.")
    # A model change between the two runs makes the comparison not like-for-like: count and score
    # deltas can come from the model itself, not from new distress. Detect it from the stored
    # per-run settings and label the movement neutrally.
    cross_model = run_cfg(con, ch["current"]) != run_cfg(con, ch["previous"])
    if cross_model:
        st.warning("These two weeks were scored with different versions of the scoring model "
                   "(the model changed between runs). Count and score changes may reflect the model "
                   "change itself, not new distress - this is not a like-for-like comparison.")
    worse_label = "Score up" if cross_model else "Got worse"
    better_label = "Score down" if cross_model else "Got better"
    c = st.columns(4)
    for col, (label, key) in zip(c, [("New", "new"), (worse_label, "worse"),
                                     ("New filings", "new_late"), ("Dropped off", "dropped")]):
        col.metric(label, len(ch[key]))
    st.subheader("🆕 New flags")
    _list(ch["new"], "new")
    st.subheader(f"🔺 {worse_label}")
    _list(ch["worse"], "worse", lambda r: f"up {r['delta']} from {r['previous_score']:.0f}")
    st.subheader("✅ Became ready to act (new court filing or lien)")
    _list(ch["new_late"], "late", lambda r: "now READY TO ACT")
    st.subheader(f"🔻 {better_label}")
    _list(ch["better"], "better", lambda r: f"down {-r['delta']} from {r['previous_score']:.0f}")
    st.subheader("✔️ Dropped off")
    if not ch["dropped"]:
        st.caption("None.")
    for r in ch["dropped"]:
        st.markdown(f"- **{esc(r['property_name'])}** — no longer flagged (e.g. it opened or refinanced).")


def page_log(con, run_id):
    st.title("Research log")
    cfg = run_cfg(con, run_id)
    goal, last = cfg["goal"]["annual_deal_volume"], cfg["goal"]["last_year_volume"]
    log = con.execute("SELECT * FROM research_log ORDER BY logged_at DESC").df()
    closed = float(log.loc[log["status"] == "deal_closed", "deal_amount"].fillna(0).sum()) if len(log) else 0.0
    st.subheader("Progress toward the $300M goal")
    c = st.columns(3)
    c[0].metric("Closed from flags", money(closed))
    c[1].metric("Growth needed this year", money(goal - last))
    c[2].metric("Share of growth from flags", f"{closed / (goal - last):.0%}")
    st.progress(min(1.0, closed / (goal - last)))
    status = latest_status(con)
    counts = pd.Series(list(status.values())).value_counts() if status else pd.Series(dtype=int)
    st.subheader("Funnel")
    st.bar_chart(pd.DataFrame({"Stage": [weekly.STATUS_LABELS[s] for s in weekly.RESEARCH_STATUSES],
                               "Flags": [int(counts.get(s, 0)) for s in weekly.RESEARCH_STATUSES]}),
                 x="Stage", y="Flags", horizontal=True, sort=False)
    st.caption("Each flag counts once, at its latest stage.")
    st.subheader("Every entry")
    if log.empty:
        st.caption("Nothing logged yet.")
        return
    view = pd.DataFrame({
        "When": pd.to_datetime(log["logged_at"]).dt.strftime("%b %d, %Y"), "Flag": log["property_name"],
        "Stage": log["status"].map(weekly.STATUS_LABELS),
        "Amount": log["deal_amount"].map(lambda x: money(x) if pd.notna(x) else ""),
        "Note": log["note"], "Researcher": log["researcher"]})
    st.dataframe(view, hide_index=True, use_container_width=True)
    st.download_button("Download as CSV", view.to_csv(index=False), "research_log.csv", "text/csv")


def page_how(con, run_id):
    cfg = run_cfg(con, run_id)
    w, so, cs, late = cfg["weights"], cfg["standout"], cfg["counter_signal"], cfg["late"]
    st.title("How the score works")
    with st.expander("What we heard from Bloomfield, and what we changed", expanded=True):
        st.markdown("""
**What we heard**
- Construction loan still on the clock is the **strongest** signal. A project that opened with its construction loan still outstanding, or a loan from five years ago never paid off, is distress.
- Opening date watch is good but **weaker**. A 3–6 month delay is normal.
- Creative signals (rent vs. the city, a site gone quiet) are very promising once we can detect them reliably.
- Court and lien filings come **late**: the situation is already actionable, not an early warning.
- Forced and public sales are not really a signal, and CMBS data gives little edge because everyone has it.

**What we changed**
- The construction-loan clock (50) and opening date watch (20) outweigh every other signal; a building that opened with its loan still outstanding gets an extra boost.
- Court and lien filings add at most a few points and tag the flag **✅ READY TO ACT**.
- Auctions and CMBS are shown as context only, never scored.
- County records are found through **NETR Online** (`python -m collectors.discover`), recorders first because they feed the loan clock.
""")
    st.latex(r"C = 1-\prod_{j\in\{R,I,Q,V,X,K,L_q,W\}}\left(1-q_j\,s_j\right)")
    st.latex(r"\text{Early}=\sum_{L,O,C} w_i\, s_i \times \begin{cases}\max(\text{boost}(s_i),\ \text{rescale}) & s_i\ge %s\\ \text{rescale} & \text{otherwise}\end{cases}"
             % so.get("boost_starts_at", 0.6))
    st.latex(r"\text{boost}(s)=1+%s\times\frac{s-%s}{%s-%s}" % (so.get("max_boost", 1.5) - 1, so.get("boost_starts_at", 0.6),
                                                             so.get("boost_full_at", 1.0), so.get("boost_starts_at", 0.6)))
    st.latex(r"\text{rescale}=\min\!\left(%s,\ \frac{100}{\text{weights of terms that can apply}}\right)" % cfg["applicability"]["max_rescale"])
    st.latex(r"\text{Score}=\min\!\left(100,\ \text{Early}\times %s^{\,\#\text{counter-signals}}+\text{Late}\right)" % cs["factor"])
    lc, od, rd, ig = cfg["loan_clock"], cfg["opening_delay"], cfg["rent_decline"], cfg["inspection_gap"]
    c = cfg["creative"]
    qo, iv, xs, kc, lq = (c["occupancy_slide"], c["income_erosion"], c["cost_squeeze"], c["concession_creep"],
                          c["licensing_quality"])
    st.dataframe(pd.DataFrame([
        {"Term": "L · Loan clock (construction, or any short-term loan that needed an extension)", "Weight": w["loan_clock"], "0 → 1": f"{lc['start_months']} → {lc['full_months']} months unpaid, +{lc['per_extension']} per extension, +{lc.get('opened_still_unpaid_bonus', 0)} if the building opened with the loan still outstanding", "From": "County recorder (recorded_loans / Reader) + public SEC loan filings: past maturity = 1.0, 'modified' flag = an extension"},
        {"Term": "O · Opening delay", "Weight": w["opening_delay"], "0 → 1": f"{od['start_months']} → {od['full_months']} months late (under {od['start_months']} is normal)", "From": f"Project websites (Part B projects); if none, a public new-construction permit with no certificate of occupancy (months since issue − {od.get('expected_build_months', 24)})"},
        {"Term": "C · Creative signals (combined)", "Weight": w["creative"], "0 → 1": "1 − Π(1 − q·s) over the signals below", "From": "see below"},
    ]), hide_index=True, use_container_width=True)
    st.markdown(f"**Creative signals** (weight {w['creative']} combined; each counts as reliability q × strength s; together at most {c['max_confirmations']} anti-fluke confirmations)")
    st.dataframe(pd.DataFrame([
        {"Signal": "R · Rent vs. city", "s = 0 → 1": f"(city rent change − property rent change) ÷ {rd['full_gap']:.0%}", "q": rd["reliability"], "From": "B rent_observations + A market_rents"},
        {"Signal": "I · Site inactivity", "s = 0 → 1": f"{ig['start_days']} → {ig['full_days']} days since a city inspection", "q": ig["reliability"], "From": "A permit_inspections"},
        {"Signal": "Q · Occupancy slide", "s = 0 → 1": f"(peak occ. last {qo['lookback_months']} mo − occ. now − market drop) ÷ {qo['full_gap']:.0%}", "q": qo["reliability"], "From": "operating_metrics + market_occupancy"},
        {"Signal": "V · Income erosion", "s = 0 → 1": f"NOI drop from its best year (last {iv['lookback_months']} mo) ÷ {iv['full_noi_drop']:.0%}", "q": iv["reliability"], "From": "operating_metrics (public disclosures; no DSCR)"},
        {"Signal": "X · Cost squeeze", "s = 0 → 1": f"(expense growth − revenue growth since best year) ÷ {xs['full_gap']:.0%}", "q": xs["reliability"], "From": "operating_metrics"},
        {"Signal": "K · Concession creep", "s = 0 → 1": f"(free months per lease-year now − 6+ months ago) ÷ {kc['full_per_year']}", "q": kc["reliability"], "From": "B rent_observations (free rent + lease term)"},
        {"Signal": "Lq · Licensing / care", "s = 0 → 1": "CMS 1-star 0.8 · 2-star 0.5 · abuse +0.2 · state distress 0.8", "q": lq["reliability"], "From": "A licensing_records"},
        {"Signal": "W · Value cut", "s = 0 → 1": f"(value at securitization − most recent value) ÷ value at securitization ÷ {c.get('value_cut', {}).get('full_drop', 0.5):.0%}", "q": c.get("value_cut", {}).get("reliability", 1.0), "From": "Public SEC CMBS loan filings (Schedule AL valuation fields)"},
    ]), hide_index=True, use_container_width=True)
    cor = {int(k): v for k, v in cfg["corroboration"].items()}   # JSON turns the keys into text
    st.markdown(f"""
- **No lone-signal discount:** a single signal counts in full (1 signal → {cor[1]:.0%}), because much of the data isn't public.
- **Strength boost:** a signal at ≥ {so.get('boost_starts_at', 0.6):.0%} strength counts more, rising to ×{so.get('max_boost', 1.5)} at {so.get('boost_full_at', 1.0):.0%} (for C, the strongest single creative signal decides). The boost and the rescale don't stack: the larger is used.
- **↩ COUNTER-SIGNAL:** strong evidence the other way (rent beating its city by {cs['rent_beats_city_by']:.0%}+, or a city inspection in the last {cs['inspected_within_days']} days) multiplies early points by {cs['factor']} each.
- **Ready to act (max {late['max_points']}):** court/legal filing {late['court_points']}, contractor liens {late['lien_points']} each up to {late['max_lien_points']}. Older than {late['recent_days']} days count half.
- **Not scored:** Ten-X auctions and CMBS data are shown as context only.
- **Internal tiers (formula audit only):** hot ≥ {cfg['tiers']['hot']}, watch ≥ {cfg['tiers']['watch']} - used inside the model; the app shows plain score bands instead of these labels.

Edit every number in `pipeline/config/equation.yaml`, then rerun `python -m scorer.weekly`.
""")


def page_all(con, run_id):
    st.title("All properties")
    df = flags(con, run_id)
    if df.empty:
        st.info("No properties in this run.")
        return
    st.caption(
        "Every property scored in this run by the chosen scoring model, including the ones with no "
        "distress signals yet. A low or zero model score reflects available public data only - missing "
        "data is not proof of absence. Open a property from the Flag details page for the full breakdown.")
    # Displayed score is the chosen scoring model's current run (weekly_flags rows) - one score only.
    _t = run_cfg(con, run_id)["tiers"]
    _hot, _watch = float(_t["hot"]), float(_t["watch"])
    _bands = score_bands(_hot, _watch)
    out = pd.DataFrame({
        "Rank": df["rank"],
        "Property": df["property_name"],
        "Type": df["property_type"],
        "City": df["city"],
        "State": df["state"],
        "Loan": df["loan_amount"],
        "Score": df["score"],
        "Score band": df["score"].map(lambda v: score_band(v, _hot, _watch)),
        "Listed": df["shown"].map({True: "yes", False: "no"}),
        "Why hidden": df["filter_reason"].fillna(""),
    }).sort_values(["Score", "Rank"], ascending=[False, True], na_position="last")   # model scores first; columns stay user-sortable
    c1, c2 = st.columns(2)
    band_pick = c1.multiselect("Score bands", _bands, default=_bands, key="all_bands")
    listed_pick = c2.multiselect("Listed status", ["yes", "no"], default=["yes", "no"], key="all_listed")
    out = out[out["Score band"].isin(band_pick) & out["Listed"].isin(listed_pick)]
    st.caption(f"{len(out)} of {len(df)} properties shown")
    st.dataframe(out, hide_index=True, use_container_width=True)


def case_studies(con):
    """Memo-based example deals written to live storage by the ops console (never the repo).
    [(case_id, title, subtitle, body_markdown)] in position order; empty when the table is absent."""
    try:
        return con.execute("SELECT case_id, title, subtitle, body_md FROM case_studies "
                           "ORDER BY position, case_id").fetchall()
    except Exception:
        return []


def page_cases(con, run_id):
    st.title("Case studies")
    st.caption("Two real Bloomfield deals, run through the tool from the deal memo. These are code tests with "
               "manually staged inputs, not properties the tool found by itself and not new scores for the "
               "weekly list. Each page separates what the memo says, what the code produced, what was "
               "assumed, and what public sources show.")
    cases = case_studies(con)
    if not cases:
        st.info("Case studies are not available in collected data yet.")
        return
    labels = {cid: title for cid, title, _, _ in cases}
    pick = st.radio("Case", list(labels), format_func=labels.get, key="case_pick", horizontal=True)
    for cid, title, subtitle, body in cases:
        if cid == pick:
            st.header(title)
            if subtitle:
                st.caption(subtitle)
            st.markdown(body)


PAGES = {"Ranked flags": page_ranked, "All properties": page_all, "Flag details": page_detail,
         "What changed this week": page_changes, "Research log": page_log, "How the score works": page_how,
         "Case studies": page_cases}


def main():
    maybe_ingest_console()
    con = get_con()
    all_runs = runs(con)
    # Approved layout: white top bar + horizontal tab navigation, no sidebar.
    st.session_state.setdefault("page", "Ranked flags")
    top_l, top_r = st.columns([5, 2])
    with top_l:
        st.markdown(_style.TOPBAR, unsafe_allow_html=True)
    with top_r:
        run_id = st.selectbox("Week", all_runs,
                              format_func=lambda x: x.replace("week-", "Week of ")) if all_runs else None
    st.radio("Page", list(PAGES), key="page", horizontal=True, label_visibility="collapsed")
    db_name = Path(db_path() or "data/bloomfield.duckdb").name
    if "demo" in Path(db_path() or "").name:
        st.caption(f"Database: {db_name} · Sample data only. The tool reads and flags; Bloomfield's team does all outreach.")
    else:
        st.caption(f"Database: {db_name} · Live data from public SEC filings (CMBS loans). County sources are not turned on. "
                   "The tool reads and flags; Bloomfield's team does all outreach.")
    if not run_id:
        st.title("No weekly run yet")
        st.write("Run `python -m scorer.weekly` (real data) or `python -m scorer.demo_week` (made-up demo).")
        st.caption(research_status(con))
        with st.expander("Research diagnostics"):
            st.text(research_diagnostics(con))
        return
    PAGES[st.session_state.page](con, run_id)


main()
