# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Hard invariant: read-only

This bot **never** sends, cancels, or modifies orders. Only the read-only Tinkoff token is used.

- `postOrder` / `cancelOrder` and any live-execution adapter are **intentionally not implemented**. Do not add them. `execution-preflight` actively greps the codebase to confirm none exist (see `modules/execution_preflight.py`).
- `config/settings.py` raises at import time if `LIVE_ENABLED=true`. Don't relax this without an explicit user instruction.
- `execution-plan` builds a *dry-run* plan only — `dry_run=true` is forced; treat the word "execution" in this codebase as planning, not trading.
- All "income", "strategy", and "signals" outputs are notifications/analytics, never recommendations or orders.

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

# Signals (read-only notifications)
python main.py strategy-scan --strategy trend_signal_v1 [--notify]
python main.py strategy-status

# Telegram (opt-in)
python main.py telegram-test --dry-run true
python main.py telegram-notify --dry-run false   # used by runner

python main.py -v <cmd>            # DEBUG logging
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
