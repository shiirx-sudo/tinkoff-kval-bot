# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Hard invariant: read-only

This bot **never** sends, cancels, or modifies orders. Only the read-only Tinkoff token is used.

- `postOrder` / `cancelOrder` and any live-execution adapter are **intentionally not implemented**. Do not add them. `execution-preflight` actively greps the codebase to confirm none exist (see `modules/execution_preflight.py`).
- `config/settings.py` raises at import time if `LIVE_ENABLED=true`. Don't relax this without an explicit user instruction.
- `execution-plan` builds a *dry-run* plan only — `dry_run=true` is forced; treat the word "execution" in this codebase as planning, not trading.
- All "income", "strategy", and "signals" outputs are notifications/analytics, never recommendations or orders.
- Forbidden by name (do not introduce, even as stubs): `order_client.py`, `OrdersService`, `postOrder`/`cancelOrder`, `place_order`/`submit_order`/`place_limit_order`, any full-access token. No web scraping; no investment recommendations; missing data resolves to `unknown` / `manual_required`, never a guess.
- Do not mutate the portfolio, and never inflate turnover just to reach kval status.
- If trade execution is ever added, it must be a **human-confirmed** flow (the decision and the button stay with the user) — never an autonomous engine.

When adding features, preserve this contract: any new TInvest endpoint must be a read method, and Telegram/output must say so.

## Commands

```bash
# Environment / smoke tests
python main.py doctor              # validates .env, settings, report contract
python main.py accounts            # lists accounts (masked IDs)
python -m pytest -q                # full test suite (no network — HTTP mocked)
python -m pytest tests/test_kval_tracker.py::test_name   # single test
ruff check .                       # lint (matches CI)

# Core kval workflow (writes to data/reports/)
python main.py kval-status [--as-of YYYY-MM-DD]
python main.py kval-plan [--horizon-quarters N] [--target-mode effective|bare]

# Execution planning (still dry-run; no orders ever sent)
python main.py instrument-scan --symbols LQDT --commission-bps 5
python main.py turnover-plan --instrument LQDT --mode roundtrip
python main.py execution-plan --instrument LQDT --mode roundtrip --size-mode balance
python main.py execution-preflight --instrument LQDT --max-side-notional-rub 130000

# Read-only analytics
python main.py passive-income-summary
python main.py income-summary --account-id <id> --target-monthly-rub 100000
python main.py income-calendar --months 12
python main.py income-watchlist

# Signals (read-only notifications)
python main.py strategy-scan --strategy trend_signal_v1 [--notify]
python main.py strategy-status

# Telegram (opt-in)
python main.py telegram-test --dry-run true
python main.py telegram-summary                  # print report digest (no send by default)
python main.py telegram-notify --dry-run false   # used by runner

python main.py -v <cmd>            # DEBUG logging
```

Both `ruff check .` and `pytest` must be green before any commit. Local dev is Windows/PowerShell + Python 3.14 + `.venv`; set the token env before running tests:

```powershell
$env:TINKOFF_READ_TOKEN="test"; $env:LIVE_ENABLED="false"; python -m pytest
```

CI runs `ruff check .` then `pytest -q` against Python 3.10/3.11/3.12 with `TINKOFF_READ_TOKEN=test-token-readonly`.

## Architecture

### Data flow (one direction)

```
.env → config/settings.py
        │
        ▼
brokers/tinkoff/rest_client.py   ← thin Bearer-token REST client
        │                          (Quotation/{units,nano} as strings, camelCase JSON)
        ▼
api/client.py                    ← ReadOnlyClient facade: only methods listed here
        │                          are exposed to the rest of the app
        ▼
modules/*                        ← pure-ish business logic (period, filter, turnover,
        │                          planner, scanner, signals, income, balance)
        ▼
reports/*                        ← write reports (data/reports/) + console renderers
        │                          (output_contract.py freezes column order & schema)
        ▼
notifications/*                  ← Telegram alerts (read reports, build text, send)
        │
        ▼
main.py                          ← CLI: argparse subcommands → cmd_* handlers
```

`main.py` only wires args → modules → reports. Business logic does not live in `main.py`.

### Why REST, not the SDK

Read `ANALYSIS.md` first if touching the broker layer. The project deliberately calls Tinkoff Invest API directly via `requests` (gRPC-over-REST endpoints under `/rest/...`). Consequence: **operations are JSON dicts in camelCase**, `Quotation` is `{units: str, nano: int}`, `operationType` is a string enum (e.g. `OPERATION_TYPE_BUY`). All filtering and aggregation code in `modules/` is built against this contract — do not introduce SDK objects.

### Money & precision

All monetary math uses `decimal.Decimal`. `common/helpers.quotation_to_decimal` is the canonical Quotation → Decimal conversion. Never use floats for turnover or notional. `common/helpers` also has `mask_identifier` (used everywhere account IDs surface) and `stable_hash`.

### Period rule

`kval-status` measures turnover over **the 4 most recently completed quarters** — the current (in-progress) quarter is excluded. `modules/period_calculator.py` owns this. There's an open question about the rule documented in `ANALYSIS.md`.

### Reports contract

`reports/output_contract.py` defines the schema and column order for every CSV/JSON. `reports/runtime_doctor.py` validates that what's on disk still matches the contract. When adding a column or report, update the contract first — tests and `doctor` will catch drift.

`data/reports/`, `data/manual/`, `data/state/`, and `data/alerts/` are all gitignored. The `data/config/*.yaml` files (e.g. `income_engine.yaml`, `fundamental_filter.yaml`) are user-supplied; commit only `config/*.example.yaml`.

### Tests

`tests/conftest.py` sets `TINKOFF_READ_TOKEN`/`LIVE_ENABLED` *before* importing settings and provides JSON-shaped fixtures (`quotation`, `make_operation`, `make_account`) matching the REST contract. The HTTP layer is mocked at `TinkoffReadOnlyClient._post` — tests never hit the network. Follow this pattern for new tests.

### Strategy / signal layer

`strategies/trend_signal_v1.py` is a self-contained read-only strategy. It produces BUY/SELL/HOLD/SKIP/AVOID verdicts with a score 0–100; SELL is suppressed to AVOID unless the instrument is actually held (portfolio is read via `ReadOnlyClient.get_portfolio`). `modules/strategy_signals.py` orchestrates scans and dedup state (`data/state/strategy_signals_state.json`). `notifications/signals.py` builds the Telegram text. Watchlist items support explicit class code (`TQBR:SBER` or `SBER@TQBR`); otherwise the resolver walks `SIGNALS_CLASS_CODE_PRIORITY`.

`modules/fundamental_filter.py` is an optional overlay (manual YAML, no scraping) that can downgrade BUY to HOLD.

### Telegram

`notifications/telegram.py` is fully self-contained: reading reports, computing the alert decision (status-change, deadline windows, daily summary cadence with antispam in `data/alerts/telegram_alert_state.json`), and sending. Token is never logged. `telegram-notify` is the cron-friendly entry point; `telegram-test` and `telegram-summary` are diagnostic.

## Conventions & workflow

- Code comments and all user-facing text (Telegram, console, commit messages) are written in **Russian**.
- CSV output is `utf-8-sig` with `;` as the delimiter (see `reports/output_contract.py`); reports also emit json and md.
- **Never push directly to `main`.** Work on a branch and open a PR so the diff is reviewable.

## Claude Code workflow protocol

This is the operating protocol for Claude Code in this repo. It exists so the
user does not have to re-decide every time whether to `/clear`, what to do next,
PR vs direct, whether to commit, which checks to run, or what to put in the
report. Follow it by default.

### 1. Session hygiene

- Start every new independent task with `/clear` unless the user explicitly says
  this is a continuation.
- After `/clear`, restate the task, branch/main policy, allowed files, forbidden
  files, validation commands, and expected report format.
- Do not rely on hidden chat history after `/clear`; use repository files and the
  latest user task.

### 2. Default task framing

For every task, Claude must identify:

- task type:
  - docs-only
  - local config/generated data
  - code change
  - report/smoke/validation
  - PR review/fixup
- allowed files
- forbidden files
- branch policy
- commit policy
- validation commands
- stop conditions

### 3. Branch / PR / direct-to-main rules

- Default: work on a branch and open a PR.
- Never push directly to `main` unless the user explicitly says direct-to-main is
  allowed for this exact task.
- New modules, API client changes, income engine/policy changes, report
  semantics, calculations, or anything near execution/trading/orders/tokens/live
  require branch + PR.
- Docs-only changes may still use branch + PR if `CLAUDE.md` is being changed or
  if the workflow rules are being changed.
- Local generated/user config under `data/config` and reports under
  `data/reports` are not committed.

### 4. Local config / generated files

- `data/config/*.yaml` are user/local files unless explicitly stated otherwise.
- Generated files like `data/config/income_universe.yaml` and
  `data/config/income_universe.generated.yaml` must not be committed.
- If updating a local config file, create a timestamped backup before overwrite.
- After local config generation, run smoke commands and report exact
  enabled/disabled profiles.
- If `git status` shows `data/config` or `data/reports` as tracked changes, stop
  and ask/report.

### 5. Reporting requirement

Every Claude response after doing work must include:

- HEAD / branch
- files changed
- whether anything was committed/pushed
- whether PR was opened or not
- validation results:
  - pytest
  - ruff
  - safety scan
  - smoke commands
- `git status -sb`
- exact next action for the user
- explicit "do not merge yet" or "ready to merge" if a PR exists

Claude must not leave the user asking "what now?".

### 6. PR workflow

When work is PR-based:

- Push branch.
- If `gh` is available and authenticated, open PR.
- If `gh` is not available, do not install it unless the user explicitly asks.
- Instead provide the GitHub PR creation URL and exact title/body.
- After PR is opened, report:
  - PR URL
  - base branch
  - head branch
  - commits
  - files changed
  - checks status if available
  - whether merge is allowed or must wait for CI

### 7. Stop conditions

Claude must stop and report, not guess, if:

- a command would require full-access token;
- any order/execution/live API appears;
- `.env`/secrets would be changed;
- a generated/user config appears tracked unexpectedly;
- exact account-id is unclear and the command requires one;
- a task requires new API endpoints not already confirmed read-only;
- tests fail;
- safety scan shows new violations;
- branch is not clean before a merge-sensitive action.

### 8. User-facing style

- Give concrete next command/task, not vague options.
- Do not ask the user what to do next if the next safe step is obvious.
- If user action is required, state exactly what to click or paste.
- If there are multiple safe options, recommend one default.

### 9. Required final report template

```
Done / Blocked:

Branch:
HEAD:
Files changed:
Committed:
Pushed:
PR:
Validation:
- pytest:
- ruff:
- safety scan:
- smoke:
Git status:
Important findings:
Next action:
Merge status:
```
