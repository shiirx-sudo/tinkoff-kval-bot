# AI Review Playbook

Internal checklist and prompt library for reviewing future PRs in this repository
with AI assistance (Claude Code / Codex). Specific to `tinkoff-kval-bot`.

## Purpose

- **AI review is an assistant step, not a replacement for human review.** A human
  approves and merges; the AI accelerates finding issues.
- **AI output is not trusted until verified** against the GitHub diff, the test
  suite, the safety contract, a smoke run, and docs consistency. A model report
  ("looks good", "CI is green") is a claim to check, not a fact.
- **This project is safety-sensitive.** It touches broker APIs and portfolio/
  trading infrastructure. The hard invariant is **read-only**: the bot never sends,
  cancels, or modifies orders, and only the read-only token is used (see
  `CLAUDE.md` and `modules/execution_preflight.py`). A wrong "this is safe" here is
  worse than a missed bug.

## Golden rules

- **Never trust a report without checking the diff.** Read `git diff` / the PR
  patch yourself before agreeing with any AI summary.
- **Never say CI is green** unless GitHub statuses/workflows confirm it, or you ran
  the local validation and saw it pass. Empty statuses ≠ green.
- **Never merge if safety scope is unclear.** Ambiguity about whether something
  touches execution/orders/tokens is a BLOCK.
- **Never allow** trading / `postOrder` / `cancelOrder` / sell / retry / market /
  scheduler / Telegram-execution / POST or action endpoints in read-only PRs.
- **Never print tokens** and never let a token value reach logs, reports, or HTML.
- **Never commit** `.env`, `data/config/*`, or `data/reports/*` (all gitignored).
- **Always preserve backward-compatible JSON aliases** unless the PR explicitly and
  intentionally removes them (and says so).
- **Always check docs for stale numbers/claims** (turnover targets, income model,
  contribution sourcing).

## Standard PR review checklist

### 1. Scope
- Does the PR do only what it claims?
- Are unrelated files changed?
- Is there scope creep (drive-by refactors, unrelated renames)?

### 2. Safety
- No trading writes.
- No `PostOrder` / `CancelOrder`.
- No sell / retry / market behavior.
- No scheduler.
- No Telegram execution.
- No POST/action endpoints in the dashboard.
- No live/sandbox token reads in read-only flows.

### 3. Backward compatibility
- Existing CLI commands still work.
- Existing JSON fields still exist or have documented aliases.
- Existing reports still load.
- Existing configs without new fields still load.

### 4. Data model correctness
- No guessing — missing data resolves to `null` / `unknown` / `manual_required`.
- Null/partial states are explicit (not silently zero).
- Warnings are propagated to the caller / report.
- Financial terms are not mixed:
  - dividends/coupons = **scheduled income**
  - strategy PnL = **realized net only**
  - paper/model = **separate**, not in conservative coverage
  - turnover = **buy+sell gross only** (dividends/coupons are not turnover)
  - contributions = **deposits**; withdrawals tracked separately

### 5. Edge cases
- Empty operations list.
- API unavailable (None) → explicit fallback + warning.
- Partial reports / partial freshness.
- Old config without new keys.
- Future dates (e.g. plan not started yet).
- Zero / negative values.
- Duplicate operations (dedup by id).
- Unknown operation types (warn, do not guess).

### 6. Tests
- Happy path.
- Fallback path.
- Partial / error path.
- Legacy aliases.
- Safety scan (no forbidden literals; `no_order_endpoints` / `no_live_adapter`).
- Dashboard HTML labels.
- Real-account read-only smoke where applicable.

### 7. Docs consistency
- No stale 60M turnover target.
- Current turnover target is **6M trailing 4 quarters** (500k/mo, 1.5M/quarter).
- Contributions are **API-based by default** after F4.10.1.
- "Passive income" is **no longer top-level** after F4.11 (it is scheduled income).
- Strategy income is a **placeholder** unless reports exist.

### 8. Smoke
- Run the command relevant to the feature.
- Inspect the JSON report, not only console output.
- Render the dashboard HTML/server for UI changes.
- Confirm no tracked `data/` files appear in `git status`.

## Standard AI prompts

Ready-to-copy prompts. Paste the diff/validation as context; do not let the model
assume the PR description is true.

### Prompt 1 — PR diff review

```text
Review this PR diff as a strict code reviewer.

Focus on:
- scope creep
- regressions
- backward compatibility
- edge cases
- missing tests
- docs inconsistencies
- unsafe behavior

Return:
1. PASS/BLOCK
2. blockers
3. non-blocking concerns
4. exact files/lines to inspect
5. tests that should exist
6. smoke commands to run

Do not assume the PR description is true. Base your review only on the diff and available validation.
```

### Prompt 2 — safety contract review

```text
Review this PR for safety-contract violations.

Project safety contract:
- no trading writes unless explicitly approved
- no PostOrder / CancelOrder
- no sell/retry/market behavior
- no scheduler
- no Telegram execution
- no POST/action endpoints in dashboards
- no live/sandbox token reads in read-only flows
- no token printing
- no mutation of portfolio/broker state
- no `.env`, `data/config/*`, `data/reports/*` commits

Return:
1. PASS/BLOCK
2. every suspicious file/function
3. exact forbidden behavior if present
4. why it matters
5. recommended fix
```

### Prompt 3 — data schema compatibility review

```text
Review this PR for data schema compatibility.

Check:
- existing JSON fields remain present or have documented aliases
- old configs still load
- old reports still render
- null/partial states are explicit
- warnings/errors are propagated
- dashboard handles missing fields safely
- docs describe new and legacy fields accurately

Return:
1. PASS/BLOCK
2. breaking schema changes
3. missing aliases
4. missing tests
5. recommended migration notes
```

### Prompt 4 — dashboard UI review

```text
Review dashboard changes.

Check:
- labels match the current data model
- no stale terminology
- no misleading financial claims
- paper/model values are clearly separated from conservative values
- read-only dashboard has no action buttons/endpoints
- HTML escaping is safe
- empty/null values render clearly
- important warnings are visible

Return:
1. PASS/BLOCK
2. misleading UI labels
3. safety/UI risks
4. missing empty-state handling
5. exact tests to add
```

### Prompt 5 — financial logic review

```text
Review financial calculations.

Check:
- scheduled income is dividends/coupons only
- strategy income is realized net only
- paper/model estimates are excluded from conservative coverage
- turnover is buy+sell gross only
- commissions are separate
- contributions count deposits only
- withdrawals are tracked separately
- no forecast is treated as confirmed income
- taxes are not guessed

Return:
1. PASS/BLOCK
2. incorrect calculations
3. misleading terminology
4. missing warnings
5. tests to add
```

### Prompt 6 — docs consistency review

```text
Review docs against current implementation.

Check for stale references:
- old turnover target 60M/year
- old monthly turnover 5M
- old quarterly turnover 15M
- old manual-only contribution facts
- top-level passive income terminology
- claims that strategy income exists before reports exist
- any safety claim contradicted by code

Return:
1. PASS/BLOCK
2. stale docs found
3. exact replacement text
4. docs files to update
```

### Prompt 7 — test coverage review

```text
Review test coverage for this PR.

Check:
- each new branch of logic has a test
- fallback paths have tests
- partial/API-unavailable paths have tests
- legacy aliases have tests
- dashboard labels have tests
- safety scan covers new files
- docs-only changes do not require code tests, but affected render/tests still pass

Return:
1. PASS/BLOCK
2. missing tests
3. weak tests
4. redundant tests
5. exact test names to add
```

### Prompt 8 — final merge review

```text
Perform final pre-merge review.

Inputs:
- PR diff
- local validation output
- GitHub status/workflow status
- smoke output
- changed files list

Check:
- branch is based on current main
- PR is mergeable
- no unexpected files
- validation matches changed scope
- safety contract holds
- docs are not stale
- smoke confirms expected runtime behavior

Return:
1. MERGE / DO NOT MERGE
2. blockers
3. post-merge commands
4. post-merge smoke commands
5. branch cleanup commands
```

## Local validation commands

Windows PowerShell (the project's dev shell):

```powershell
git status -sb
git log --oneline -10

python main.py doctor
python -m pytest
python -m ruff check .
python main.py execution-preflight
```

For dashboard PRs:

```powershell
$LIVE_ACCOUNT_ID = "2057431918"
python main.py portfolio-dashboard-data --live-account-id $LIVE_ACCOUNT_ID
python main.py portfolio-dashboard --host 127.0.0.1 --port 8766
```

For JSON inspection (inspect the report, not just console output):

```powershell
$report = Get-Content data\reports\portfolio_dashboard_data.json -Raw | ConvertFrom-Json
$report.income_summary | Format-List
$report.turnover_summary | Format-List
$report.contributions_summary | Format-List
```

> Note: `python -m pytest` requires the token env to be set, e.g.
> `$env:TINKOFF_READ_TOKEN="test"; $env:LIVE_ENABLED="false"` (see `CLAUDE.md`).
> The test suite never hits the network; only the explicit smoke commands above do,
> and only via the read-only path.

## GitHub review commands / checks

Final review should verify, against GitHub itself:

- compare the branch against `main` (is it based on current main?);
- the changed-files list matches the claimed scope;
- PR metadata (title, base, head, description);
- commit status / workflow runs if CI is configured;
- exact file patches when a claim needs confirmation.

**Do not claim CI is green if statuses are empty.** No configured workflow ≠ passing
workflow — say "no CI configured" rather than "green".

## Current project-specific invariants

- Base monthly living basket = **150000 RUB/month**, base date **2026-06**.
- Turnover target = **6,000,000 RUB trailing 4 quarters**.
- Monthly turnover target = **500,000 RUB**.
- Quarterly turnover target = **1,500,000 RUB**.
- Contribution facts default to **API operations** (`fact_source=api_operations`).
- Manual contribution facts are **fallback/adjustments only**
  (`manual_facts_enabled=false` by default).
- Income model (F4.11):
  - scheduled income = **dividends/coupons**;
  - strategy income = **realized net only** (placeholder until reports exist);
  - paper/model **excluded** from conservative coverage.
- The dashboard is **read-only** — GET routes only, no actions, no token reads.

> This playbook is process/documentation. It is **not** investment advice and adds
> no trading, execution, broker writes, scheduler, Telegram, or action endpoints.
