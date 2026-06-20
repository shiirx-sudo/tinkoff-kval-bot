# Roadmap — tinkoff-kval-bot

_Last updated: 2026-06-19_

## Purpose

`tinkoff-kval-bot` is a read-only analytics toolkit for building and monitoring an income-oriented investment portfolio through T-Invest data.

Primary goal: help plan a portfolio intended to generate investment income from dividends, coupons, money-market funds, and other income sources.

Secondary/side goal: track qualified-investor turnover/status where useful. Turnover inflation is not the main objective.

## Non-negotiable safety contract

Until explicitly changed by the owner, the project remains strictly read-only.

Forbidden by default:

- OrdersService
- postOrder
- cancelOrder
- place_order
- submit_order
- place_limit_order
- order_client
- full-access token
- LIVE_EXECUTION
- autonomous trading
- portfolio mutation
- scraping for financial data
- investment recommendations wording

Allowed:

- read-only T-Invest data
- calculations
- reports
- Telegram notifications/summaries
- manual planning outputs
- no-order target/planned allocation analysis

## Workflow rules

### Direct-to-main allowed for small safe fixes

Small direct commits to `main` are acceptable when all are true:

- focused diff
- strictly read-only
- no order endpoints / no full token / no live execution
- no `.env` or secret changes
- no portfolio mutation
- no scraping
- no recommendation wording
- pytest green
- ruff clean
- safety scan clean
- clean `git status -sb` after push

### Branch + PR required for large or risky work

Use branch + PR for:

- new modules
- architecture changes
- income engine / income policy changes
- API client changes
- required capital / yield / target gap math changes
- report-output semantics changes
- anything near execution/trading/orders/tokens/live
- ambiguous diffs

## Implemented foundation

### 1. Read-only qualification/turnover tracking

Status: implemented.

Purpose:

- monitor qualification windows and turnover requirements
- plan manual turnover without placing orders
- keep qualification as a side benefit, not the core investment goal

### 2. Balance-adaptive execution planning, still read-only

Status: implemented.

Purpose:

- compute theoretical turnover/execution plan
- respect available cash and reserve
- block impossible/silly plans
- no real orders

### 3. Technical signals: trend_signal_v1

Status: implemented.

Purpose:

- read-only technical signal layer
- Telegram signal notifications
- portfolio-aware SELL/EXIT WATCH only when the instrument is actually held
- AVOID instead of SELL when there is no position

Current role: secondary signal/risk layer, not a trade executor.

### 4. Fundamental filter: fundamental_filter_v1

Status: implemented.

Purpose:

- quality overlay for companies
- manual YAML fallback/override
- flags such as state-control risk, cash-return quality, management alignment, market growth

Current role: quality/risk context, not a source of truth and not a recommendation engine.

### 5. Income engine: income_engine_v1

Status: implemented.

Purpose:

- estimate portfolio income from dividends, coupons, money-market funds
- calculate raw expected income
- calculate gap to target monthly income
- produce reports and Telegram summaries

### 6. Automatic read-only income sources

Status: implemented.

Purpose:

- use official read-only T-Invest API data where available
- dividends: future-known and trailing history
- bonds: known coupon schedule
- money-market funds: trailing yield from candles
- manual YAML remains override/fallback, not the primary source

Source priority:

1. manual override
2. API known future
3. API trailing 12m
4. trailing 30d
5. assumed/manual fallback
6. unknown

### 7. Income source audit

Status: implemented.

Purpose:

- show the raw API events behind income calculations
- classify dividend/coupon events into usable and non-usable buckets
- expose suspicious one-off or trailing-only income
- prevent blind trust in raw yield numbers

Reports:

- income_source_audit.json
- income_source_audit.csv
- income_source_audit.md

### 8. Income quality policy: income_quality_policy_v1

Status: implemented.

Purpose:

- split income into raw/base/estimate/excluded/unknown layers
- make conservative planning possible
- exclude unreliable trailing-only income from base by default
- apply haircut to variable money-market income

Policy buckets:

- income_reliable
- income_variable
- income_estimated
- income_manual
- income_excluded
- income_unknown

### 9. Target portfolio planner: target_portfolio_v1

Status: implemented.

Purpose:

- calculate target allocation for a desired monthly net income
- use conservative/base income policy
- show current vs target
- create read-only new-capital and monthly contribution plans
- use neutral wording such as planned_add_rub and underweight_by_rub, never buy/sell/order wording

Known behavior:

- if eligible universe is too small, target plan leaves some weight idle and raises diversification warnings
- required capital depends heavily on eligible universe and conservative yield
- allocation uses transparent cap-based equal weights; yield affects required capital and expected income, not the weights

Low-yield diagnostics (read-only):

- each allocation now reports `capital_share_pct`, `income_share_pct`,
  `income_efficiency_ratio`, `yield_vs_blended_ratio` and a `low_yield_slot` flag
- a slot is flagged `low_yield_slot` when it holds a large share of capital
  (`capital_share_pct >= 10`) yet its conservative net yield is far below the
  blended portfolio yield (`yield_vs_blended_ratio < 0.30`)
- flagged slots add a Russian analytical warning to the report
- diagnostics are warnings only: they do not change weights, do not auto-exclude
  instruments, and are not investment advice or a recommendation

## Current strategic issue

The target planner works, but the eligible income universe is too narrow.

Recent observed target output used only:

- VTBR
- T
- LQDT

This caused:

- only 75% allocation used due 25% position caps
- diversification warning
- required capital around 21.6M RUB for 100k RUB/month target

Next work should focus on filling and auditing `data/config/income_universe.yaml`, expanding eligible instruments, and comparing target scenarios, not on changing execution logic.

## Planned next actions

### Milestone A — Income universe management

Status: implemented.

Implemented in `cd294b3`:

- `config/income_universe.example.yaml`
- `modules/income_universe.py`
- `--universe-profile` / `--universe-path` support for `target-portfolio`
- `--universe-profile` / `--universe-path` support for `income-watchlist`

Goal: avoid passing long `--watchlist` strings manually and build a maintainable income universe.

Planned work:

- add `config/income_universe.example.yaml`
- add real user file `data/config/income_universe.yaml` under gitignore
- add `modules/income_universe.py`
- support profiles such as:
  - base_income
  - extended_income
  - money_market
  - dividend_candidates
  - bond_candidates
- add `--universe-profile` / `--universe-path` to `target-portfolio`
- optionally add the same to `income-watchlist` and `income-source-audit`

Expected result:

- run target planner with `--universe-profile base_income`
- easier iteration on candidate instruments
- no hardcoded recommendations in code

### Milestone A2 — Income universe builder

Status: in progress (PR `feature/income-universe-builder-v1`, not yet merged).

Goal: stop hand-maintaining `data/config/income_universe.yaml`; generate it from
read-only T-Invest data + local rules/overrides.

Work in this PR:

- `modules/income_universe_builder.py` (read-only; reuses `income-watchlist` resolution + income policy).
- `build-income-universe` CLI with `--enable-mode disabled|policy|conservative`, `--dry-run`, `--backup`/`--force`, `--include-disabled`, `--max-bonds`.
- `config/income_universe_rules.example.yaml` (rules/overrides; tracked example).
- `docs/income_universe_builder.md`.
- Generated YAML stays compatible with `modules/income_universe.py`.

Not automated (flagged in notes, never auto-enabled): credit ratings, issuer
qualitative risk, one-off dividend detection, tax treatment, qualified-investor
availability. Bonds/OFZ-PK/quasi-currency stay disabled until coupon/income smoke
passes (separate task).

### Milestone B — Expand eligible instruments

Status: planned.

Goal: improve target allocation quality by adding more instruments that can pass conservative policy.

Candidate categories to research using read-only data:

- money-market funds
- federal/corporate bonds with known coupon schedule
- dividend stocks with announced future payments
- stable dividend candidates with clear policy treatment
- instruments to exclude due state-control risk, unknown income, or one-off payouts

Important: this stage should audit sources before using them in base planning.

### Milestone C — Target portfolio scenario analysis

Status: planned.

Goal: compare target plans under several assumptions.

Planned scenarios:

- conservative only
- conservative + estimated income
- money-market heavy
- dividend heavy
- bond/coupon heavy
- lower/higher monthly income targets
- different max-position and max-money-market caps

Expected reports:

- scenario comparison table
- required capital by scenario
- monthly base income by scenario
- risks/warnings by scenario

### Milestone D — Planned top-up workflow, still no orders

Status: planned.

Goal: turn target allocation into a clear manual funding plan.

Planned outputs:

- monthly contribution plan
- expected base income growth over time
- underweight/overweight tracking
- Telegram summary
- no order placement

### Milestone E — Portfolio review dashboard / report pack

Status: planned.

Goal: provide a single overview for decision-making.

Potential outputs:

- portfolio_income_dashboard.md
- summary of income, target, policy, audit, and target allocation
- high-risk/unknown instruments list
- upcoming income calendar
- contribution progress

### Milestone F — Confirmation-based execution, future only

Status: blocked / future discussion.

No execution is planned now.

If ever introduced, it must be:

- separate branch + PR
- explicit owner approval
- full safety design
- full-token discussion
- no autonomous orders
- confirmation per action
- strong preflight and kill switch

## Ideas inbox

Use this section to append new ideas before implementing them.

- Done: income_universe_v1 profiles and config (`cd294b3`).
- Add scenario comparison for target portfolio.
- Add bond-focused income planner.
- Add money-market alternatives comparison.
- Add dividend reliability scoring from historical/audited events.
- Add target progress tracker over months.
- Add Telegram `/income_summary`, `/target_portfolio`, `/income_audit` commands if interactive bot mode is added.
- Add dashboard/report pack for human review.
- Add data-quality warnings for suspicious dividends, one-off payments, and missing API data.

### Research-backed backlog ideas

Sourced from research notes in `docs/research/income_universe_research.md`. These are
research ideas / candidates for audit / future backlog — not investment
recommendations, and no concrete instruments enter the production universe from here.

- Bond universe filter policy: rating, coupon type, maturity, offer/amortization flags, liquidity, NKD.
- Bond risk policy: downgrade, losses, negative equity, reporting quality, refinancing dependence, legal/tax/default signals.
- Bond cashflow ledger: separate transaction register and cashflow register.
- Deposit / money-market benchmark scenarios for required capital.
- OFZ-PK / floater monthly cashflow scenario.
- Currency / quasi-currency bond profile.
- Dividend reliability / shareholder-return score.
- Claude Code workflow docs and minimal-context discipline.

## Decision log

- Main goal is income-oriented investing, not turnover inflation.
- Qualification is useful but secondary.
- Historical/trailing income is not treated as reliable base income by default.
- Manual YAML values are override/fallback and must remain clearly labeled.
- Target portfolio planning must remain read-only and neutral-worded.
- New ideas should be recorded in this roadmap before implementation.
