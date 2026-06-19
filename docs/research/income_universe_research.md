# Income Universe Research Notes

> **Research notes only — read carefully before using anything here.**
>
> - These are research notes only.
> - Not investment recommendations.
> - Do not add instruments directly to the production universe from this file.
> - Every instrument or heuristic must be validated through official sources / the
>   T-Invest read-only API / current market data before use.
> - Keep the project strictly read-only.

## 1. Purpose

This file collects ideas for expanding the income universe, bond filters, cashflow
accounting, risk policy, and future reports. It is an inbox for research, not a
specification and not a recommendation. Concrete tickers appear only as
`candidates_for_audit` and must be validated before any use.

## 2. Bond universe filters

Ideas for future `bond_candidates` / `extended_income` profiles:

- rating filters:
  - conservative: AA and above;
  - moderate: A- and above only after explicit risk review;
- coupon type:
  - fixed coupon for scenarios where rate decline is expected;
  - floater / variable coupon for an uncertain rate environment;
- maturity buckets:
  - up to 2 years for a liquidity-focused bucket;
  - 3–5 years for a longer income-planning bucket;
  - long OFZ-PK / floaters only as a separate scenario, not default;
- avoid or flag:
  - offers / put dates;
  - amortization, unless cashflow logic supports it explicitly;
  - poor liquidity;
  - suspiciously high yield without an issuer-risk explanation;
- track:
  - price as % of nominal;
  - accrued interest / NKD;
  - nominal;
  - maturity date;
  - next offer date;
  - coupon frequency;
  - coupon formula;
  - yield to maturity / expected yield at audit date.

## 3. OFZ-PK / floater income cadence idea

Research idea:

- Long OFZ-PK floaters can be grouped by coupon months to create smoother monthly
  cashflow.
- This should become a scenario comparison only, not a recommendation.
- Candidate examples from the source material are stored only as
  `candidates_for_audit`, never as an approved universe:
  - OFZ 29024 / SU29024RMFS5
  - OFZ 29025 / SU29025RMFS2
  - OFZ 29026 / SU29026RMFS0
  - OFZ 29027 / SU29027RMFS8
  - OFZ 29023 / SU29023RMFS7
  - OFZ 29022 / SU29022RMFS9
  - OFZ 29017 / SU29017RMFS9
  - OFZ 29018 / SU29018RMFS7
  - OFZ 29009 / SU29009RMFS6
  - OFZ 29010 / SU29010RMFS4
- Warning: the coupon formula and coupon calendar must be validated against official
  bond data before any use.

## 4. Bond risk / exclusion signals

Ideas for a future `bond_risk_policy_v1`:

- credit rating downgrade by 2 or more notches;
- negative watch / under review only if the underlying reason is concerning;
- losses for the last 4 quarters;
- negative equity;
- questionable reporting quality;
- questionable financial management;
- high dependency on bond-market refinancing;
- large arbitration / legal disputes;
- recurring or material tax debts / account blocks;
- default outside exchange-traded bonds, including CFA, crowdfunding, OTC debt;
- issuer debt structure dominated by market debt;
- yield materially above fair yield for the rating without a clear explanation.

## 5. Bond fair-yield / relative-value idea

Future research / backlog idea:

- Compare bond yield against a fair-yield baseline by credit rating.
- Possible future model:
  - money-market yield baseline;
  - rating-based default probability;
  - recovery assumption;
  - liquidity adjustment;
  - duration / maturity adjustment.
- Output should be `relative_value_score`, `yield_premium_to_fair`, and
  `risk_adjusted_yield_flag`.
- Do not implement now.

## 6. Bond accounting / cashflow ledger idea

Future report / data-model idea. For bond analytics, do not rely only on position
value. Future reports should separate:

A. Transaction register:

- ticker / figi / isin;
- issuer;
- date;
- quantity;
- price as % of nominal;
- accrued interest / NKD;
- commission;
- nominal;
- maturity date;
- offer date;
- expected yield at audit date.

B. Cashflow register:

- coupons;
- amortization;
- redemption;
- taxes if available;
- fees if available.

Idea: compare planned yield at entry/audit date vs actual realized cashflow over
time.

Do not implement now.

## 7. Money-market / deposit benchmark scenario

Scenario idea:

- Keep money-market funds and deposits as benchmark / opportunity-cost scenarios.
- Target monthly income reports should show:
  - required capital at current portfolio base yield;
  - required capital at a conservative money-market benchmark;
  - required capital at a deposit-like benchmark;
  - after-tax and inflation-adjusted caveats.
- Do not hardcode bank offers or old rates.
- Any deposit rates from articles are examples only and must not be treated as
  current data.

## 8. Currency / quasi-currency bond bucket

Research idea:

- Add a future `currency_bond_candidates` / `quasi_currency_income` profile.
- Purpose:
  - diversify ruble income risk;
  - compare hard-currency / CNY / substitute-bond income streams.
- Require extra warnings:
  - FX risk;
  - liquidity risk;
  - settlement risk;
  - issuer risk;
  - tax treatment;
  - availability for qualified / non-qualified investor.

Do not add instruments directly to config now.

## 9. Equity income quality overlay

Ideas for future expansion of `fundamental_filter_v1` / a `dividend_reliability_score`:

- management alignment with minority shareholders;
- whether free cash flow is returned to shareholders;
- debt burden and whether cash is absorbed by debt service;
- state influence:
  - state as support / protection;
  - state as controller / source of forced capex or regulation;
- whether the company's market is growing or shrinking;
- dividend consistency;
- shareholder return policy;
- dilution / additional share issue risk.

## 10. Behavioral / portfolio risk notes

Non-technical policy notes:

- avoid panic decisions after a drawdown;
- avoid trying to recover losses through higher-risk assets;
- avoid concentration in one issuer / sector / asset type;
- avoid excessive news-driven churn;
- keep scenario comparison and risk warnings visible in reports.

## 11. Claude Code workflow notes

(Recorded here because there is no `docs/development/` directory yet; move to
`docs/development/claude_code_workflow.md` if such a directory is created later.)

- Keep root `CLAUDE.md` minimal.
- Put detailed module-specific context near the relevant modules/docs.
- Use atomic tasks with explicit file scope.
- Tell Claude which files may be read/changed.
- Avoid broad prompts like "improve everything".
- For docs-only work, say docs-only explicitly.
- For safety:
  - local reversible actions may be allowed;
  - destructive actions, external publishing, force-push, secrets changes, and
    anything visible to third parties require explicit user confirmation.
- Do not remove unfamiliar files as "unused".
- Do not over-engineer.
- Do not touch neighboring code when fixing docs.
- For long work:
  - keep progress notes;
  - use git commits as checkpoints;
  - run tests before finishing if code was changed.
- For this project specifically:
  - preserve read-only T-Invest API safety;
  - no portfolio mutation;
  - no execution modules;
  - no secret/token changes;
  - no scraping.
