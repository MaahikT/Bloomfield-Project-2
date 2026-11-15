"""
run.py - the weekly bot. One searcher per signal; each can be run alone.

    python -m bot                       # every enabled signal
    python -m bot --signals opening_date,distress_news
    python -m bot --list                # what each signal does

Signals and what runs for each:
  opening_date    project websites: "coming soon" past the date, and dates that keep moving   (bot/opening.py)
  distress_news   news and notices: foreclosure, receivership, lis pendens, UCC sale, auction (bot/news.py)
  county_records  county recorder, court and public-notice sites that are set up              (collectors/county.py)
  lenders         bank call reports: lenders with rising nonaccruals                          (collectors/ffiec.py)
  cmbs            SEC CMBS loan filings                                                       (collectors/edgar.py)
Then run the matcher and scorer:  python -m matcher ; python -m scorer.weekly
"""
from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from collectors import run_all
from collectors.common import db
from collectors.common.log import get_logger, setup_logging
from collectors.common.parse import load_yaml
from scout_reader_schema import open_db

from . import news, opening
from .search import Budget

log = get_logger("bot")
CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "bot.yaml"

DESCRIPTIONS = {
    "opening_date": "Project websites: still 'coming soon' after the date, and dates that keep moving (second-strongest signal).",
    "distress_news": "News and notices: foreclosure, receivership, lis pendens, UCC sale, auction (late: confirms a deal is ready to act).",
    "county_records": "County recorder, court and public-notice sites that are set up (construction-loan clock, liens, foreclosures).",
    "lenders": "Bank call reports: lenders with rising commercial real estate nonaccruals.",
    "cmbs": "SEC CMBS loan filings (little edge: everyone has them).",
}
COLLECTOR_FOR = {"county_records": ["county"], "lenders": ["ffiec"], "cmbs": ["edgar"]}


def load_config(path=None) -> dict:
    return load_yaml(path or CONFIG_PATH) or {}


def run_signal(name: str, con, cfg: dict, budget: Budget, today: date, session=None, defer_to_ai: bool = False) -> dict:
    if name == "opening_date":
        return opening.run(con, cfg, budget, today, session, defer_to_ai)
    if name == "distress_news":
        return news.run(con, cfg, budget, today, session, defer_to_ai)
    if name in COLLECTOR_FOR:
        results = run_all.run_all(con, only=COLLECTOR_FOR[name])
        return {"collectors": COLLECTOR_FOR[name], "status": [r.get("status") for r in results],
                "new_files": sum(int(r.get("files_new") or 0) for r in results)}
    raise KeyError(f"unknown signal {name!r}; choose from {sorted(DESCRIPTIONS)}")


def run(con, cfg: dict | None = None, signals: list[str] | None = None, today: date | None = None, session=None,
        defer_to_ai: bool = False) -> dict:
    """Run the chosen signals (default: every enabled one). One failing signal never stops the others."""
    cfg = cfg or load_config()
    today = today or date.today()
    budget = Budget(cfg)
    out = {}
    for name in signals or cfg.get("enabled_signals") or list(DESCRIPTIONS):
        try:
            out[name] = run_signal(name, con, cfg, budget, today, session, defer_to_ai)
        except KeyError:
            raise
        except Exception as e:                                          # noqa: BLE001 - keep going
            log.exception("signal %s failed", name)
            out[name] = {"error": str(e)}
    out["_budget_left"] = {"pages": budget.pages_left, "queries": budget.queries_left}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="The web bot: one searcher per signal.")
    ap.add_argument("--signals", help="comma-separated; default is every enabled signal in config/bot.yaml")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--full", action="store_true", help="crawl, read with the AI, link and score (see bot/full.py)")
    ap.add_argument("--no-ai", action="store_true", help="with --full: skip the AI reader")
    args = ap.parse_args(argv)
    if args.full:
        from .full import main as full_main
        return full_main([a for a in (argv if argv is not None else __import__("sys").argv[1:]) if a != "--full"])
    if args.list:
        for k, v in DESCRIPTIONS.items():
            print(f"{k:15} {v}")
        return 0
    setup_logging()
    con = open_db()
    result = run(con, signals=[s.strip() for s in args.signals.split(",")] if args.signals else None)
    for k, v in result.items():
        print(f"{k}: {v}")
    print("Next: python -m matcher ; python -m scorer.weekly")
    return 0
