# income_universe_builder_v1

Read-only generator for the local income universe (`data/config/income_universe.yaml`),
so you don't have to maintain it by hand.

> Read-only analytics. NOT investment recommendations. The builder sends no orders,
> does not mutate the portfolio, does not use a full-access token, does not scrape,
> and does not change secrets. `enabled: true` means **eligible for analysis**, not a
> recommendation.

## Why manual YAML is not enough

Hand-maintaining the universe is slow and error-prone: tickers/class codes drift,
policy buckets change, and audit notes get stale. The builder resolves seeds through
the read-only T-Invest API (via the existing `income-watchlist` pipeline), applies the
current income policy, and writes a compatible YAML — deterministically and repeatably.

It is a **rules-driven / seed-driven** builder: it does **not** scan the whole T-Invest
market. Candidates come only from the rules file (and `overrides.include`); the
read-only API is used to resolve and classify those seeds, not to discover new ones.

## Generated file vs rules file

- **Rules file** (`config/income_universe_rules.example.yaml` → copy to
  `data/config/income_universe_rules.yaml`): selection rules, candidate seeds, and
  manual overrides. You edit this.
- **Generated file** (`data/config/income_universe.generated.yaml` by default, or
  `--output data/config/income_universe.yaml`): produced by the builder. Do not edit
  the generated sections by hand — change the rules/overrides and re-run.

Both stay compatible with `modules/income_universe.py` (schema: `profiles → <name> →
{description, instruments: [{ticker, class_code, role, enabled, notes}]}`). Only
`enabled: true` instruments enter a watchlist.

## Safety contract

- Read-only T-Invest API only; no orders, no execution/live modules.
- No portfolio mutation, no secret/token changes, no full-access token.
- No scraping, no external paid APIs.
- Output is analytics/candidates only: `eligible_for_analysis` / `excluded_by_policy`.

## Command examples

```powershell
# Preview only (writes nothing):
.\.venv\Scripts\python.exe main.py build-income-universe `
  --rules-path data/config/income_universe_rules.yaml `
  --enable-mode policy --dry-run

# Write the generated universe (default output path):
.\.venv\Scripts\python.exe main.py build-income-universe --enable-mode policy

# Overwrite an existing file safely with a backup:
.\.venv\Scripts\python.exe main.py build-income-universe `
  --output data/config/income_universe.yaml --enable-mode policy --backup
```

## Enable modes

- `disabled` (default): every found candidate is written `enabled: false` — a pure
  audit queue.
- `policy`: `enabled: true` only for instruments the current income policy treats as
  base-eligible (`income_reliable` / `income_variable`); everything else stays
  disabled with a reason.
- `conservative`: `enabled: true` only for money-market instruments that are
  policy-eligible; all equities/bonds stay disabled.

Other flags: `--backup`, `--force` (overwrite without backup), `--dry-run`,
`--include-disabled/--no-include-disabled` (default include), `--max-bonds` (cap).
`--profile-set` is **reserved** — only `income` is implemented; any other value logs a
warning and falls back to `income` logic.

## What cannot be fully automated

The builder will not invent data it cannot read from the read-only API. These require
manual review and are flagged in `notes`, never used to enable a candidate:

- **Credit ratings** — if unavailable from the API, no rating is set; notes say
  "rating unavailable from read-only API".
- **Issuer qualitative risk** — governance, refinancing dependence, legal/tax issues.
- **One-off / non-recurring dividend detection** — trailing yield can mislead.
- **Tax treatment** — especially for FX / quasi-currency instruments.
- **Qualified-investor availability** — eligibility is not auto-determined.

Bonds (corporate, OFZ-PK, quasi-currency) are always written **disabled** by the
builder (bond/OFZ/quasi roles are never auto-enabled in any mode). An OFZ-PK can
classify as `income_reliable` through a known coupon schedule (`api_coupon_schedule`),
while others classify as `income_unknown`; either way the builder keeps them disabled,
because annualizing a floater's currently-known coupon can mislead until a dedicated
coupon-calendar validation is implemented.

## How to review the generated universe

1. Run with `--dry-run` and read the summary (scanned / included / disabled-by-reason
   / unresolved / policy-excluded / unknown-income).
2. Inspect `disabled_research_candidates` — that's the manual-audit queue.
3. For each `enabled: true`, confirm the `auto:` note (policy bucket + source) makes
   sense; validate yield source for money-market and trailing entries.
4. Re-run `income-watchlist --universe-profile <name>` and `target-portfolio
   --universe-profile <name>` to confirm behavior before relying on the universe.
