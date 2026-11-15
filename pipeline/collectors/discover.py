"""
discover.py - finds county record websites online and adds them to our list of CANDIDATE sources.

In plain English:
  1. it reads a public directory of county websites (settings: config/discovery.yaml; for example NETR Online),
     but only after a person has confirmed that directory's terms of use (terms_checked: true),
  2. for each county it collects the links to that county's recorder, court, public-notice and assessor sites,
  3. it SCREENS every link with the same polite fetcher the other collectors use: robots.txt must allow the
     page, there must be no login or CAPTCHA wall, and the site's terms page must not mention automated
     access (if it does, or if we cannot find the terms, a person decides),
  4. it saves the results in the website_candidates table and can write a PROPOSED county settings file.

It never solves a CAPTCHA, never logs in, and never switches a county to automatic by itself: a screened
site is only a candidate until a person reads the terms and adds the selectors (see config/counties/_template.yaml).

Run from the pipeline/ folder:
    python -m collectors.discover --state MI --county "Washtenaw County"
    python -m collectors.discover --state MI --limit 10 --screen --proposals
    python -m collectors.discover --check                  # self-check: can it reach and read the directory?
    python -m collectors.discover --state MI --county "Washtenaw County" --from-html saved_page.html   # offline
    python -m collectors.discover --report                 # what has been found so far
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

import yaml
from bs4 import BeautifulSoup

from collectors.common import config, db, fetch
from collectors.common.log import get_logger, setup_logging
from collectors.common.parse import load_yaml

log = get_logger("discover")

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "discovery.yaml"
PROPOSAL_DIR = Path(__file__).resolve().parents[1] / "config" / "counties" / "proposed"

TABLE_SQL = """
CREATE TABLE IF NOT EXISTS website_candidates (
    url            TEXT PRIMARY KEY,
    state          TEXT,
    county         TEXT,
    kind           TEXT,        -- recorder / building / court / public_notice / assessor / other
    link_text      TEXT,
    found_on       TEXT,        -- the directory page the link came from
    directory      TEXT,
    screen_status  TEXT,        -- not_screened / allowed_candidate / needs_person / manual_inbox
    screen_reason  TEXT,
    found_at       TIMESTAMP,
    screened_at    TIMESTAMP
);
"""


class DirectoryNotApproved(Exception):
    """A person has not yet confirmed the directory's terms of use."""


def load_config(path: str | Path | None = None) -> dict:
    return load_yaml(path or CONFIG_PATH) or {}


def ensure_table(con) -> None:
    con.execute(TABLE_SQL)


def county_slug(name: str) -> str:
    return re.sub(r"\s+", "_", re.sub(r"\s+county$", "", (name or "").strip().lower(), flags=re.I))


# ---------------------------------------------------------------------------
# Reading a directory page
# ---------------------------------------------------------------------------
def classify(text: str, url: str, rules: dict) -> str:
    hay = f"{text} {url}".lower()
    for kind, words in rules.items():
        if any(w in hay for w in words):
            return kind
    return "other"


def parse_links(html: str, page_url: str, skip_domains: list[str]) -> list[tuple[str, str]]:
    """Links on a directory page that lead to some other website (the county's own sites)."""
    seen, out = set(), []
    for a in BeautifulSoup(html, "html.parser").select("a[href]"):
        href = urljoin(page_url, a["href"].strip())
        p = urlparse(href)
        host = p.netloc.lower()
        if p.scheme not in ("http", "https") or not host:
            continue
        if any(host == d or host.endswith("." + d) for d in skip_domains):
            continue
        key = href.rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        out.append((" ".join(a.get_text(" ").split()), href))
    return out


def _directory(cfg: dict, name: str) -> dict:
    d = (cfg.get("directories") or {}).get(name)
    if not d or not d.get("enabled", True):
        raise KeyError(f"directory {name!r} is not enabled in discovery.yaml")
    if not d.get("terms_checked"):
        raise DirectoryNotApproved(
            f"{name}: a person must read the directory's terms of use and set terms_checked: true in "
            f"config/discovery.yaml first. {d.get('terms_note', '')}")
    return d


STATE_NAMES = {"AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
               "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
               "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
               "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
               "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
               "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
               "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
               "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
               "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
               "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming"}


def _plain(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def find_link(html: str, page_url: str, wanted: list[str], same_site: str) -> str | None:
    """On a directory page, the link on the same site whose text (or address) matches one of the wanted names."""
    wants = [_plain(w) for w in wanted if w]
    best = None
    for a in BeautifulSoup(html, "html.parser").select("a[href]"):
        href = urljoin(page_url, a["href"].strip())
        if urlparse(href).netloc.lower() != same_site:
            continue
        text = _plain(a.get_text(" "))
        text_no_county = re.sub(r" county$", "", text)
        for w in wants:
            if text == w or text_no_county == w:
                return href                       # exact match wins
            if best is None and w and (f" {w} " in f" {text} " or w.replace(" ", "-") in href.lower() or w.replace(" ", "_") in href.lower()):
                best = href
    return best


def _get(url: str, session=None):
    return fetch.polite_get(url, config.domain_settings(urlparse(url).netloc), session=session)


def locate_county_page(d: dict, state: str, county: str, session=None) -> tuple[str | None, str]:
    """Find the directory page for one county. Try the configured address first, then follow the directory's links."""
    host = urlparse(d.get("home_url") or d["county_url"]).netloc.lower()
    names = [re.sub(r"\s+county$", "", county, flags=re.I), county]
    guess = d["county_url"].format(state=state, county_slug=county_slug(county))
    resp = _get(guess, session)
    if resp is not None:
        return guess, resp.text
    log.info("the guessed county address did not work; following the directory's own links instead")
    state_page = None
    if d.get("state_url"):
        url = d["state_url"].format(state=state)
        r = _get(url, session)
        if r is not None:
            state_page = (url, r.text)
    if state_page is None and d.get("home_url"):
        r = _get(d["home_url"], session)
        if r is not None:
            st_link = find_link(r.text, d["home_url"], [STATE_NAMES.get(state, state), state], host)
            if st_link:
                r2 = _get(st_link, session)
                if r2 is not None:
                    state_page = (st_link, r2.text)
    if state_page is None:
        return None, "could not open the directory's page for this state"
    link = find_link(state_page[1], state_page[0], names, host)
    if not link:
        return None, f"the state page has no link that matches {county}"
    r = _get(link, session)
    return (link, r.text) if r is not None else (None, f"could not open {link}")


def save_candidates(con, html: str, page_url: str, d: dict, cfg: dict, directory: str, state: str, county: str) -> list[dict]:
    rows = []
    for text, link in parse_links(html, page_url, d.get("skip_domains", [])):
        if con.execute("SELECT 1 FROM website_candidates WHERE url = ?", [link]).fetchone():
            continue
        kind = classify(text, link, cfg.get("classify", {}))
        con.execute("INSERT INTO website_candidates VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    [link, state, county, kind, text[:200], page_url, directory, "not_screened", None, db.utc_now(), None])
        rows.append({"url": link, "state": state, "county": county, "kind": kind, "link_text": text[:200],
                     "found_on": page_url, "directory": directory})
    return rows


def discover_county(con, cfg: dict, directory: str, state: str, county: str, session=None) -> list[dict]:
    """Find the county's website links in the directory and save them as candidates."""
    d = _directory(cfg, directory)
    url, html = locate_county_page(d, state, county, session)
    if url is None:
        log.warning("%s, %s: %s (blocked, disallowed or down?). Run --check for details, or use --from-html with a saved page.",
                    county, state, html)
        return []
    rows = save_candidates(con, html, url, d, cfg, directory, state, county)
    log.info("%s, %s: %d new candidate sites", county, state, len(rows))
    return rows


def discover_from_html(con, cfg: dict, directory: str, state: str, county: str, html_path: str, page_url: str | None = None) -> list[dict]:
    """Offline route: read a directory page saved from a browser (File > Save page as). No network needed."""
    d = _directory(cfg, directory)
    html = Path(html_path).read_text(encoding="utf-8", errors="replace")
    return save_candidates(con, html, page_url or d.get("home_url") or "https://directory.invalid/", d, cfg, directory, state, county)


def check_access(cfg: dict, directory: str, state: str = "MI", county: str = "Washtenaw County", session=None) -> list[tuple[bool, str]]:
    """A step-by-step self-check that says exactly what is working and what is not."""
    out = []
    email = config.get_user_agent()
    out.append((bool(email), "contact address is set (the team default unless .env overrides it)" if email else "no contact address: set contact_email_default in config/sources.yaml"))
    try:
        d = _directory(cfg, directory)
    except (KeyError, DirectoryNotApproved) as e:
        return out + [(False, str(e))]
    out.append((True, f"{directory} is approved in discovery.yaml"))
    home = d.get("home_url") or d["county_url"]
    domain = urlparse(home).netloc.lower()
    resp = _get(home, session)
    if resp is None:
        why = fetch.get_fallbacks().get(domain) or (
            "robots.txt disallows it" if fetch.get_robots_skips().get(domain) else
            "could not connect. If you are on a cloud session, its network policy probably blocks this host; "
            "on your own computer check your internet connection or firewall")
        return out + [(False, f"cannot open {home}: {why}")]
    out.append((True, f"opened the directory home page {home}"))
    url, html = locate_county_page(d, state, county, session)
    if url is None:
        return out + [(False, f"could not find the {county}, {state} page: {html}. Save that page from a browser and use --from-html")]
    out.append((True, f"found the {county}, {state} page: {url}"))
    links = parse_links(html, url, d.get("skip_domains", []))
    kinds: dict[str, int] = {}
    for text, link in links:
        k = classify(text, link, cfg.get("classify", {}))
        kinds[k] = kinds.get(k, 0) + 1
    out.append((bool(links), f"{len(links)} outside links on that page ({', '.join(f'{n} {k}' for k, n in sorted(kinds.items())) or 'none'})"))
    return out


# ---------------------------------------------------------------------------
# Screening a candidate site (never gets around anything)
# ---------------------------------------------------------------------------
def screen_site(url: str, cfg: dict, session=None) -> tuple[str, str]:
    """Return (status, reason). allowed_candidate only means 'passed the automatic screens'."""
    domain = urlparse(url).netloc.lower()
    settings = config.domain_settings(domain)
    resp = fetch.polite_get(url, settings, session=session)
    if resp is None:
        if fetch.needs_manual_inbox(domain):
            return "manual_inbox", fetch.get_fallbacks().get(domain, "access denied, login or CAPTCHA")
        if (urlparse(url).path or "/") in fetch.get_robots_skips().get(domain, set()):
            return "manual_inbox", "robots.txt disallows this page"
        return "needs_person", "could not be read (down, error or unreachable)"
    soup = BeautifulSoup(resp.text, "html.parser")
    terms = next((urljoin(url, a["href"]) for a in soup.select("a[href]")
                  if re.search(r"terms|conditions|use policy|disclaimer", a.get_text(" "), re.I)), None)
    if not terms:
        return "needs_person", "no terms of use page found; a person must check"
    t = fetch.polite_get(terms, settings, session=session)
    if t is None:
        return "needs_person", "terms page could not be read; a person must check"
    text = BeautifulSoup(t.text, "html.parser").get_text(" ").lower()
    hits = [w for w in cfg.get("terms_red_flags", []) if w in text]
    if hits:
        return "needs_person", "terms mention: " + ", ".join(hits) + " (a person must read them)"
    return "allowed_candidate", "passed robots.txt, no login/CAPTCHA, no red-flag words in terms; a person still confirms"


def screen_pending(con, cfg: dict, limit: int | None = None, session=None) -> dict:
    counts: dict[str, int] = {}
    # Screen the sites that feed the strongest signal first (recorders -> loan clock).
    rank = priority_sql(cfg)
    rows = con.execute("SELECT url FROM website_candidates WHERE screen_status = 'not_screened' "
                       f"AND kind != 'other' ORDER BY {rank}, state, county"
                       + (f" LIMIT {int(limit)}" if limit else "")).fetchall()
    for (url,) in rows:
        status, reason = screen_site(url, cfg, session)
        con.execute("UPDATE website_candidates SET screen_status=?, screen_reason=?, screened_at=? WHERE url=?",
                    [status, reason, db.utc_now(), url])
        counts[status] = counts.get(status, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Adding to the list: a PROPOSED county file for a person to review
# ---------------------------------------------------------------------------
SOURCE_FOR_KIND = {"recorder": "recorder", "building": "permits", "court": "court", "public_notice": "public_notice"}


def priority_sql(cfg: dict) -> str:
    """SQL ordering by signal_priority in discovery.yaml (unknown kinds last)."""
    pri = cfg.get("signal_priority", {})
    whens = " ".join(f"WHEN '{k}' THEN {int(v.get('rank', 9))}" for k, v in pri.items() if str(k).isidentifier())
    return f"CASE kind {whens} ELSE 9 END" if whens else "(CASE WHEN TRUE THEN 9 END)"


def write_proposals(con, out_dir: str | Path | None = None) -> list[Path]:
    out = Path(out_dir or PROPOSAL_DIR)
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for state, county in con.execute("SELECT DISTINCT state, county FROM website_candidates ORDER BY 1, 2").fetchall():
        rows = con.execute("SELECT url, kind, screen_status, screen_reason FROM website_candidates "
                           "WHERE state=? AND county=? AND kind != 'other'", [state, county]).fetchall()
        if not rows:
            continue
        sources = {}
        for kind, source in SOURCE_FOR_KIND.items():
            mine = [r for r in rows if r[1] == kind]
            if mine:
                sources[source] = {"access": "manual_inbox",
                                 "notes": "PROPOSED by discover.py. A person must confirm each site's terms before changing "
                                          "access to scrape/open_data_api. Candidates: "
                                          + "; ".join(f"{u} [{s}: {why or 'not screened yet'}]" for u, _, s, why in mine)}
        if not sources:
            continue
        doc = {"county": county, "state": state, "status": "manual_inbox", "priority": 5,
               "notes": "PROPOSED file written by discover.py. Review, then move it to config/counties/ (not done automatically).",
               "sources": sources}
        path = out / f"{state.lower()}_{county_slug(county)}.yaml"
        path.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
        written.append(path)
    return written


def report(con, cfg: dict | None = None) -> str:
    rows = con.execute("SELECT screen_status, count(*) FROM website_candidates GROUP BY 1 ORDER BY 1").fetchall()
    out = "Candidate websites: " + (", ".join(f"{n} {s}" for s, n in rows) if rows else "none yet")
    pri = (cfg or {}).get("signal_priority", {})
    if pri and rows:
        kinds = dict(con.execute("SELECT kind, count(*) FROM website_candidates GROUP BY 1").fetchall())
        out += "\nBy what they feed in the Opportunity Score (look at these first):"
        for kind, v in sorted(pri.items(), key=lambda kv: kv[1].get("rank", 9)):
            out += f"\n  {v.get('rank')}. {kind:<13} {kinds.get(kind, 0):>4} site(s) -> {v.get('feeds')}"
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Find county record websites online and add them as candidates.")
    ap.add_argument("--directory", default="netr_online")
    ap.add_argument("--state"), ap.add_argument("--county", help='e.g. "Washtenaw County"')
    ap.add_argument("--limit", type=int, help="max counties (or sites to screen)")
    ap.add_argument("--screen", action="store_true", help="screen the sites found (robots.txt, login/CAPTCHA, terms)")
    ap.add_argument("--proposals", action="store_true", help="write proposed county settings files")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--check", action="store_true", help="self-check: is the directory reachable and does it parse?")
    ap.add_argument("--from-html", help="read a directory page saved from a browser instead of fetching it (needs --state and --county)")
    args = ap.parse_args()
    setup_logging()
    con = db.connect()
    ensure_table(con)
    cfg = load_config()
    if args.check:
        steps = check_access(cfg, args.directory, args.state or "MI", args.county or "Washtenaw County")
        for ok, msg in steps:
            print(("PASS  " if ok else "FAIL  ") + msg)
        raise SystemExit(0 if all(ok for ok, _ in steps) else 1)
    if args.from_html:
        if not (args.state and args.county):
            raise SystemExit("--from-html needs --state and --county")
        rows = discover_from_html(con, cfg, args.directory, args.state, args.county, args.from_html)
        print(f"{len(rows)} new candidate sites from {args.from_html}")
    elif not args.report:
        counties = [(args.state, args.county)] if args.county else [
            (s, c) for s, c in con.execute(
                "SELECT state, county FROM county_registry WHERE (? IS NULL OR state = ?) ORDER BY priority, state, county",
                [args.state, args.state]).fetchall()][: args.limit]
        try:
            for st, co in counties:
                discover_county(con, cfg, args.directory, st, co)
        except DirectoryNotApproved as e:
            raise SystemExit(str(e))
        if args.screen:
            print("Screened:", screen_pending(con, cfg, args.limit))
    if args.proposals:
        for p in write_proposals(con):
            print("Proposed:", p)
    print(report(con, cfg))


if __name__ == "__main__":
    main()
