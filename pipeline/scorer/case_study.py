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


def valuation(con, case: dict, as_of: date, cfg: dict | None = None) -> list[dict]:
    """General valuation from the property's own operating data, plus any sale comps listed in the deal file."""
    from . import creative as cr
    from .group import group_properties
    cfg = cfg or eq.load_equation_config()
    v = case.get("valuation")
    if not v:
        return []
    vcfg = cfg["valuation"]
    _, _, _, extra = eq.load_inputs(con)
    out = []
    for p in group_properties(con):
        rows = cr._ops(p, extra["ops"], as_of)
        if not rows:
            continue
        ptype = weekly.property_type(p)
        for c in val.value_cases(rows, ptype, vcfg):
            out.append(val.scenario(f"{c['case']} [{ptype}]", c["value"], v["upb"], v["price"], vcfg,
                                    v.get("reserves", 0.0), v.get("units"), c["noi"], c["cap_rate"]))
    for comp in v.get("comps", []):
        value = sum(x["units"] * x["per_unit"] for x in comp["parts"])
        out.append(val.scenario(comp["name"], value, v["upb"], v["price"], vcfg, v.get("reserves", 0.0), v.get("units")))
    return out


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
    last = max(date.fromisoformat(str(d)) if not isinstance(d, date) else d for d in case["as_of"])
    rows = valuation(con, case, last)
    if rows:
        print("\n== Valuation scenarios ==")
        for r in rows:
            basis = (f"NOI ${r['noi'] / 1e6:5.2f}M  cap {r['cap_rate']:.1%}" if r["noi"] is not None else f"{'sale comps':>23}")
            print(f"{r['scenario']:<48} {basis}  value ${r['value'] / 1e6:5.1f}M"
                  f"  recovery ${r['recovery'] / 1e6:5.1f}M  {r['multiple']:.2f}x  IRR {r['irr']:6.1%}"
                  f"  bid for target ${r['bid_for_target'] / 1e6:5.1f}M ({r['bid_pct_upb']:.0%} of UPB)")


if __name__ == "__main__":
    main()
