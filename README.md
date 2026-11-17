# Bloomfield Sourcing Tool

Flags commercial properties showing signs of financial distress, for Bloomfield Capital.
Built by the University of Michigan Business+Tech AI Sprint team. Output is intended for
researcher review; the tool never contacts anyone.

- **Live tool:** https://bloomfield-flags.up.railway.app/
- **Detail:** [pipeline/README.md](pipeline/README.md) (per-part setup),
  [pipeline/UMICH-WORKFLOW.md](pipeline/UMICH-WORKFLOW.md) (U-M GPT Toolkit key),
  [docs/README-ARCHIVE.md](docs/README-ARCHIVE.md) (the original long-form README, archived
  snapshot).

## How it works

Each scheduled run is the same chain:

1. **Collect** public records. SEC CMBS filings are live today; the county recorder, court
   and notice, bank call report and permit collectors are built and turn on only as each
   site's terms are cleared (`pipeline/collectors/`, `pipeline/bot/`). The shared fetch
   helper checks each site's robots.txt, and each new county source waits in a manual inbox
   until a person approves it.
2. **Read** - an AI reader turns each document into fixed fields with a supporting quote.
   The validation step (`pipeline/reader/check.py`) rejects a record when its quote is not
   found in the saved source text, and fetch failures are logged so gaps stay visible.
3. **Link** - records about the same property, loan, lender and company are joined; unsure
   matches go to a person (`pipeline/matcher/`).
4. **Score** - the dashboard shows one displayed **Opportunity Score** per property: the
   scoring model's latest stored run (current equation eq-2.2, term by term in
   `pipeline/config/equation.yaml`): early-warning signals, a ready-to-act lane for court
   filings and liens (capped at 10 points), and payment status & verified research (capped
   at 60), with the total capped at 100. Reviewed research stays alongside the score as
   context and never overrides it. A property without a stored score is **Unscored**, not
   cleared.
5. **Review** - a Researcher decision panel on each property page (Add to weekly list,
   Needs more digging, Dismiss; notes are not shared with the team), and a Research log page
   tracking progress toward the $300M goal and the review funnel.

## How to keep it running

- The dashboard is a Streamlit app on **Railway**, auto-deploying from the **main branch of
  MaahikT/Bloomfield-Project-2**: merging there redeploys the site. Service root `pipeline/`,
  start command `BLOOMFIELD_DB=data/live.duckdb python -m dashboard.serve`, one replica; the
  DuckDB file lives on the service volume mounted at `/app/data`.
- `dashboard.serve` starts the scheduler and ingest threads together with Streamlit in one
  process. DuckDB allows one read-write process - never point a second service at the same
  database file.
- 495 of 495 automated checks pass (post-PR21 local suite, Nov 17).
- Spend limits are configuration, not approval to enable spend: reader spend thresholds in
  `pipeline/config/reader.yaml` ($9 per week, $5 per run - the code logs a warning past them
  and keeps accounting; reader calls only); the web bot's
  optional search provider and its own caps are in `pipeline/config/bot.yaml` and run only
  when a key is set.
- If the site is down, diagnose first (Railway logs, service status, volume attached), then
  restart or redeploy.
- Researcher decisions, personal lists and notes in the dashboard are stored in the
  browser's local storage (persistence verified in the current deploy, PR22), not in a
  shared team database; clearing browser storage removes them. Match-review answers recorded in the database are kept and obeyed by
  the matcher; the dashboard does not edit them.

## Public repository

This repo is public. Keep out: confidential or unapproved Bloomfield material, private
research datasets, personal data and secrets. `pipeline/scripts/check_no_data_staged.py`,
when installed as a pre-commit hook, blocks common private file types (databases,
spreadsheets, `.env`, data and sample folders) from commits.
