"""
case_study.py - run a real deal through the whole pipeline, as if our tool had been watching it.

    cd pipeline
    python -m scorer.case_study data/cases/<deal>.yaml

The deal file (kept OUT of the public repo: data/ is git-ignored) lists rows for the same tables Parts A
and B fill (recorded_loans, loans, filing_fields, projects, operating_metrics, market_occupancy,
licensing_records, ...). This script loads them into a throwaway database, lets Part C's grouping
put them together, scores the property with the Opportunity Score on each `as_of` date (a backtest:
only data public on that date is used), and runs the valuation scenarios.
"""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

import yaml

from scout_reader_schema import create_scout_reader_tables

from . import equation as eq
from . import valuation as val
from . import weekly
from .schema import open_db


def load_case(path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_db(case: dict):
    con = open_db(":memory:")
    create_scout_reader_tables(con)
    weekly.create_tables(con)
    for table, rows in (case.get("tables") or {}).items():
        for row in rows:
            row = {k: (json.dumps(v) if isinstance(v, (list, dict)) else v) for k, v in row.items()}
            con.execute(f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})",
                        list(row.values()))
    return con


def backtest(con, case: dict, cfg: dict | None = None) -> list[dict]:
    cfg = cfg or eq.load_equation_config()
    out = []
    for d in case["as_of"]:
        d = d if isinstance(d, date) else date.fromisoformat(str(d))
        results = eq.score_all(con, cfg, d)
        top = max(results, key=lambda r: r.score) if results else None
        if top:
            out.append({"as_of": d, "score": top.score, "tier": top.tier, "stage": top.stage,
                        "flags": eq.flag_text(top), "explain": eq.explain(top, cfg)})
    return out


def valuation(case: dict) -> list[dict]:
    v = case.get("valuation")
    if not v:
        return []
    common = {k: v[k] for k in ("closing_pct", "months", "legal_per_month", "reserves", "target_irr", "units") if k in v}
    return [val.scenario(s["name"], s.get("noi"), s.get("cap_rate"), v["upb"], v["price"], value=s.get("value"), **common)
            for s in v["scenarios"]]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run a real deal through the pipeline (backtest + valuation).")
    ap.add_argument("case", help="deal file, e.g. data/cases/my_deal.yaml")
    args = ap.parse_args(argv)
    case = load_case(Path(args.case))
    con = build_db(case)
    print(f"\n== {case.get('name', 'case')}: Opportunity Score over time ==")
    for row in backtest(con, case):
        ex = row["explain"]
        sigs = ", ".join(f"{s['code']}={s['effective']:.2f}" for s in ex["creative_signals"] if s["effective"] > 0)
        print(f"{row['as_of']}  score {row['score']:>5.1f}  {row['tier']:<5} {row['stage']:<12} {row['flags']:<16} "
              f"L={ex['L_loan_clock']['strength']:.2f} O={ex['O_opening']['strength']:.2f} "
              f"C={ex['C_creative']['strength']:.2f} [{sigs}] late={ex['late_points']}")
    rows = valuation(case)
    if rows:
        print("\n== Valuation scenarios ==")
        for r in rows:
            basis = (f"NOI ${r['noi'] / 1e6:5.2f}M  cap {r['cap_rate']:.1%}" if r["noi"] else f"{'comps':>23}")
            print(f"{r['scenario']:<34} {basis}  value ${r['value'] / 1e6:5.1f}M"
                  f"  recovery ${r['recovery'] / 1e6:5.1f}M  {r['multiple']:.2f}x  IRR {r['irr']:6.1%}"
                  f"  bid for target ${r['bid_for_target'] / 1e6:5.1f}M ({r['bid_pct_upb']:.0%} of UPB)")


if __name__ == "__main__":
    main()
