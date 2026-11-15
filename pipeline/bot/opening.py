"""
opening.py - the opening-date searcher (signal 2).

For each watched project it opens the project's own website, finds what it says about opening ("Opening Fall
2026", "Now leasing for Fall 2025", "coming soon"), turns that into a deadline with scout/seasons.py, and
records it in the projects table (plus a snapshot, so a date that keeps moving is noticed). A project whose
deadline has passed while the site still says "coming soon" becomes a stale_candidate; the scorer decides how
much that is worth (a 3-6 month slip is normal, so short delays score little).
"""
from __future__ import annotations

import hashlib
import re
from datetime import date

from bs4 import BeautifulSoup

from collectors.common import config, db, fetch
from collectors.common.log import get_logger
from scout import seasons
from scout_reader_schema import upsert

from .search import Budget, allowed_domain, search

log = get_logger("bot.opening")
COLLECTOR = "bot/opening.py"
OPEN_WORDS = re.compile(r"open|leas|deliver|move[-\s]?in|coming|available|complet", re.I)


def project_id_for(name: str, url: str) -> str:
    return "bot-" + hashlib.sha1(f"{name}|{url}".encode()).hexdigest()[:12]


def page_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ")).strip()


def find_phrase(text: str, student_housing: bool = False) -> tuple[str | None, seasons.Opening | None]:
    """The sentence on the page that best states an opening date (the first one that gives a deadline)."""
    sentences = re.split(r"(?<=[.!?])\s+|\s{2,}| \| | · ", text)
    fallback = None
    for s in sentences:
        s = s.strip()
        if not s or len(s) > 220 or not OPEN_WORDS.search(s):
            continue
        o = seasons.interpret(s, student_housing=student_housing)
        if o.deadline:
            return s, o
        if fallback is None and re.search(r"coming\s+soon|pre-?leasing|opening\s+soon", s, re.I):
            fallback = (s, o)
    return fallback if fallback else (None, None)


def still_unopened(text: str, opening_cfg: dict) -> bool:
    low = text.lower()
    if any(m in low for m in opening_cfg.get("opened_markers", [])):
        return False
    return any(m in low for m in opening_cfg.get("unopened_markers", []))


def targets(con, cfg: dict, budget: Budget, session=None) -> list[dict]:
    """Projects to check: the watchlist, plus every project in the table that has a website."""
    oc = cfg.get("opening_date") or {}
    found: dict[str, dict] = {}
    for w in oc.get("watchlist") or []:
        if w.get("url"):
            found[w["url"]] = dict(w)
    for r in con.execute("SELECT project_id, project_name, main_url, city, state, property_type FROM projects "
                         "WHERE main_url IS NOT NULL AND coalesce(check_status, 'ok') != 'blocked'").fetchall():
        found.setdefault(r[2], {"project_id": r[0], "project_name": r[1], "url": r[2], "city": r[3], "state": r[4],
                                "property_type": r[5]})
    # projects with no website: ask the search service (if one is on) for the project's own site
    for r in con.execute("SELECT project_id, project_name, city, state, property_type FROM projects WHERE main_url IS NULL").fetchall():
        for q in oc.get("queries") or []:
            hits = search(q.format(name=r[1], city=r[2] or "", state=r[3] or ""), cfg, budget, session)
            best = next((h for h in hits if (r[1] or "").lower() in (h["title"] + " " + h["snippet"]).lower()), None)
            if best:
                found.setdefault(best["url"], {"project_id": r[0], "project_name": r[1], "url": best["url"], "city": r[2],
                                               "state": r[3], "property_type": r[4]})
                break
    return list(found.values())


def record_opening(con, *, pid: str, name: str, url: str, state, city, ptype, address, phrase, unopened: bool,
                   file_id, content_hash, today: date, how: str) -> None:
    """Update the project from what its page says, and keep a snapshot so a moving date is noticed."""
    now = db.utc_now()
    student = (ptype or "") == "student_housing"
    opening = seasons.interpret(phrase, student_housing=student) if phrase else None
    deadline = opening.deadline if opening else None
    prev = con.execute("SELECT stated_opening_phrase, opening_deadline, first_stated_phrase, date_change_count, first_seen_at "
                       "FROM projects WHERE project_id = ?", [pid]).fetchone()
    months = None
    if deadline and unopened and deadline < today:
        months = round((today - deadline).days / 30.44, 1)
    changes = int(prev[3] or 0) if prev else 0
    if prev and phrase and prev[0] and prev[1] and deadline and deadline != prev[1]:
        changes += 1                                                   # the stated date moved since last time
    missed = bool(student and opening and opening.lease_up_date and opening.lease_up_date < today and unopened)
    upsert(con, "projects", {
        "project_id": pid, "project_name": name, "property_address": address, "city": city,
        "state": state, "property_type": ptype, "main_url": url,
        "first_seen_at": prev[4] if prev else now, "stated_opening_phrase": phrase or (prev[0] if prev else None),
        "opening_deadline": deadline or (prev[1] if prev else None), "page_still_unopened": unopened,
        "stale_candidate": bool(months), "months_overdue": months or 0.0, "missed_lease_up": missed,
        "date_change_count": changes, "first_stated_phrase": (prev[2] if prev and prev[2] else phrase),
        "last_checked_at": now, "check_status": "ok", "notes": f"Updated by the web bot ({how})"})
    upsert(con, "project_snapshots", {
        "snapshot_id": f"{pid}-{now:%Y%m%d%H%M%S%f}", "project_id": pid, "checked_at": now, "source": "live",
        "url": url, "file_id": file_id, "content_hash": content_hash,
        "stated_opening_phrase": phrase, "page_still_unopened": unopened})


def check_project(con, cfg: dict, t: dict, today: date, session=None, defer_to_ai: bool = False) -> str:
    """Open one project page and save it. Without AI the bot reads the page itself (words and dates); with AI it only
    saves the page and the AI reader reads it later (see apply_readings). Returns ok / unreachable / no_date / saved."""
    oc = cfg.get("opening_date") or {}
    name, url = t.get("project_name") or url_name(t["url"]), t["url"]
    pid = t.get("project_id") or project_id_for(name, url)
    res = fetch.fetch_and_store(con, url, config.domain_settings(fetch.urlparse(url).netloc), source="project_site",
                                doc_category="project_website", collector_name=COLLECTOR, session=session,
                                state=t.get("state"), city=t.get("city"), source_record_id=name,
                                notes="Fetched by the web bot")
    if res is None:
        status = "blocked" if fetch.needs_manual_inbox(url) else "unreachable"
        if con.execute("SELECT 1 FROM projects WHERE project_id = ?", [pid]).fetchone():
            con.execute("UPDATE projects SET check_status = ?, last_checked_at = ? WHERE project_id = ?", [status, db.utc_now(), pid])
        return "unreachable"
    if defer_to_ai:
        return "saved"
    html = config.from_stored_path(res.file_path).read_text(encoding="utf-8", errors="replace") if res.file_path else ""
    text = page_text(html)
    phrase, _ = find_phrase(text, (t.get("property_type") or "") == "student_housing")
    record_opening(con, pid=pid, name=name, url=url, state=t.get("state"), city=t.get("city"), ptype=t.get("property_type"),
                   address=t.get("address"), phrase=phrase, unopened=still_unopened(text, oc), file_id=res.file_id,
                   content_hash=getattr(res, "file_hash", None), today=today, how="keywords")
    return "ok" if phrase else "no_date"


def apply_readings(con, today: date | None = None) -> dict:
    """Turn the AI reader's readings of saved project pages into project updates (latest reading per website)."""
    today = today or date.today()
    rows = con.execute("""
        SELECT r.url, r.state, r.city, r.source_record_id, f.file_id, r.file_hash, f.project_name, f.stated_opening_phrase,
               f.page_still_unopened, f.property_type, f.property_address, f.status
        FROM raw_files r JOIN filing_fields f ON f.file_id = r.file_id
        WHERE r.source = 'project_site' AND f.status IN ('ok', 'needs_review') AND f.model_name != 'bot-keywords'
        QUALIFY row_number() OVER (PARTITION BY r.url ORDER BY r.collected_at DESC) = 1""").fetchall()
    out = {"updated": 0, "no_date": 0}
    for url, st, city, rec_name, fid, fhash, pname, phrase, unopened, ptype, addr, status in rows:
        name = pname or rec_name or url_name(url)
        pid = (con.execute("SELECT project_id FROM projects WHERE main_url = ?", [url]).fetchone() or [None])[0] \
            or project_id_for(rec_name or name, url)
        record_opening(con, pid=pid, name=name, url=url, state=st, city=city, ptype=ptype, address=addr, phrase=phrase,
                       unopened=bool(unopened), file_id=fid, content_hash=fhash, today=today, how="AI reader")
        out["updated" if phrase else "no_date"] += 1
    return out


def url_name(url: str) -> str:
    return fetch.urlparse(url).netloc.replace("www.", "")


def run(con, cfg: dict, budget: Budget, today: date | None = None, session=None, defer_to_ai: bool = False) -> dict:
    today = today or date.today()
    summary = {"checked": 0, "ok": 0, "no_date": 0, "unreachable": 0, "saved": 0, "stale": 0}
    for t in targets(con, cfg, budget, session):
        if not allowed_domain(t["url"], cfg):
            continue
        if not budget.take_page():
            log.info("page limit reached; stopping the opening-date searcher")
            break
        outcome = check_project(con, cfg, t, today, session, defer_to_ai)
        summary["checked"] += 1
        summary[outcome] += 1
    summary["stale"] = con.execute("SELECT count(*) FROM projects WHERE stale_candidate AND notes LIKE '%web bot%'").fetchone()[0]
    return summary
