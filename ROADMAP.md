# Roadmap — tinkoff-kval-bot

_Last updated: 2026-06-22_

## Purpose

`tinkoff-kval-bot` is a read-only analytics toolkit for building and monitoring an income-oriented investment portfolio through T-Invest data.

Primary goal: help plan a portfolio intended to generate investment income from dividends, coupons, money-market funds, and other income sources.

Secondary/side goal: track qualified-investor turnover/status where useful. Turnover inflation is not the main objective.

## Non-negotiable safety contract

The project is read-only by default. Autonomous trading is permanently forbidden.
Order execution stays disabled until a separate execution PR.

Controlled manual-confirmed execution may be added **only** after all of the
following exist, in order:

1. a separate owner-approved roadmap plan (this document, see
   "Owner-approved deadline plan" below);
2. a separate design PR;
3. a separate implementation PR.

A full-access token is allowed **only** for the execution stage, and only after
explicit owner discussion, with strict isolation, a dedicated execution env var,
and a hard rule that the token is never printed/logged.

Any order is placed only after the owner's explicit manual confirmation. There is
no scheduled execution and no autonomous execution, ever.

Forbidden by default:

- autonomous trading
- order execution without manual confirmation
- scheduled live execution
- market orders without explicit approval
- full-access token printing
- portfolio mutation without explicit owner confirmation
- scraping-based financial decisions
- unreviewed investment recommendation wording

Allowed:

- read-only T-Invest data
- calculations
- reports
- Telegram notifications/summaries
- manual planning outputs
- no-order target/planned allocation analysis

Allowed only after a dedicated PR (design + implementation), with explicit owner
approval and the safety gates below:

- owner-only proposed actions
- order preview / no-send
- sandbox order flow
- manual-confirmed tiny live order
- full-access token only for the execution module after explicit owner approval
- explicit confirmation per order

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

### Milestone A3 — Disabled-candidate audit (report only)

Status: implemented.

Goal: безопасно разобрать, почему конкретные кандидаты остаются disabled, не меняя
никакой логики включения.

Work:

- `modules/income_universe_audit.py` — read-only классификатор disabled-кандидатов.
- `income-universe-audit` CLI: читает только `income_universe_builder_report.json`,
  пишет `income_universe_disabled_audit.json` / `.md`.
- Группы A/B/C/D/E: manual audit, policy review, coupon validation,
  resolver/mapping, keep disabled.
- `docs/income_universe_audit.md`.

Гарантии: не вызывает API, не читает `data/config`, не меняет income policy /
target portfolio / builder enable logic / resolver; `auto_enable_allowed=false`
для всех кандидатов. Следующие кандидаты на реализацию (отдельными PR):
coupon-validation, resolver/mapping, manual-income policy.

### Milestone C1 — Coupon validation report for group C

Status: implemented.

Goal: безопасно разобрать купонных/облигационных кандидатов из audit group C
(coupon-validation), не включая их и не меняя enable logic.

Work:

- `modules/income_coupon_validation.py` — read-only классификатор купонов
  (floating/fixed/unknown) с annualization guard.
- `income-coupon-validation` CLI: читает `income_universe_builder_report.json` и
  `income_universe_disabled_audit.json`, пишет `income_coupon_validation.json` /
  `.md`. `--offline` работает только по отчётам; API-режим использует read-only
  методы (резолв инструмента, купонный календарь, последняя цена).
- `docs/income_coupon_validation.md`.
- tests `tests/test_income_coupon_validation.py`.

Гарантии: не отправляет/не исполняет заявки, нет live/full-access; не меняет
income policy / target portfolio / builder enable logic / resolver; не пишет в
`data/config`; floating и неполные данные не annualize-ятся;
`auto_enable_allowed=false` для всех кандидатов.

Bugfix (audit routing): не-купонные инструменты (money-market LQDT/SBMM,
dividend/equity VTBR/T) больше не попадают в coupon-validation group C — она
теперь содержит только bond-like (coupon-capable) кандидатов. Не-купонные
кандидаты направляются на аудит источника дохода (group A); auto-enable
по-прежнему запрещён. Логика builder enable / resolver / target portfolio не
менялась.

Следующие кандидаты на реализацию (отдельными PR):

- resolver/mapping PR для group D;
- manual-income policy PR для group A/B;
- отдельный floating-coupon / future policy review для инструментов,
  провалидированных этим отчётом.

### Milestone C2 — Floating coupon policy diagnostics for OFZ-ПК

Status: implemented.

Goal: безопасно зафиксировать, что делать с floating-coupon кандидатами (ОФЗ-ПК /
`SU29…`) из coupon-validation, не включая их, не прогнозируя доходность как факт
и не меняя enable logic.

Work:

- `modules/floating_coupon_policy.py` — read-only селектор floating-coupon
  кандидатов из `income_coupon_validation.json` с policy-диагностикой.
- `income-floating-coupon-policy` CLI: читает `income_coupon_validation.json`,
  пишет `income_floating_coupon_policy.json` / `.md`.
- `docs/income_floating_coupon_policy.md`.
- tests `tests/test_floating_coupon_policy.py`.

Гарантии: нет сети/API/order/execution/live/full-access; не меняет income policy /
target portfolio / builder enable logic / resolver / Telegram; не пишет в
`data/config`. Для каждого кандидата `annualization_allowed=false`,
`forecast_allowed=false`, `auto_enable_allowed=false`,
`forecast_method=not_supported_yet`, `policy_status=needs_floating_coupon_policy`,
`readiness=policy_required`.

Следующие кандидаты на реализацию (отдельными PR):

- official reference-rate policy design (формула купона / RUONIA / ключевая ставка);
- resolver/mapping PR для group D;
- manual-income policy PR для group A/B.

### Milestone D1 — Resolver/mapping diagnostics for unresolved income candidates

Status: implemented after this PR.

Goal: безопасно разобрать, почему income-кандидаты из audit group D остаются
unresolved (нет проверенного `secid/ISIN/ticker/class_code`), и подготовить
материал для ручного mapping review — не маппя и не включая ни одного кандидата.

Work:

- `modules/resolver_mapping_diagnostics.py` — read-only селектор group D
  unresolved кандидатов из `income_universe_disabled_audit.json` с
  mapping-диагностикой и опциональным read-only FindInstrument enrichment.
- `income-resolver-mapping-diagnostics` CLI: читает
  `income_universe_disabled_audit.json`, пишет
  `income_resolver_mapping_diagnostics.json` / `.md`. `--offline` работает только
  по audit-отчёту; API-режим использует read-only `find_instruments`.
- `docs/income_resolver_mapping_diagnostics.md`.
- tests `tests/test_resolver_mapping_diagnostics.py`.

Гарантии: нет order/execution/live/full-access; нет скрейпинга; не маппит и не
включает кандидатов автоматически (`auto_mapping_allowed=false`,
`auto_enable_allowed=false` для всех); найденные совпадения — только
`candidates_for_manual_review`; не меняет source candidate, income policy, target
portfolio, income universe, builder enable logic, resolver behavior, Telegram; не
пишет в `data/config`. Каждый кандидат —
`recommendation_guard="candidate_for_mapping_review_only"`. Это аналитика, не
инвестиционная рекомендация.

Следующие кандидаты на реализацию (отдельными PR):

- manual mapping config review (ручное применение проверенного mapping);
- manual-income policy A/B для group A/B;
- official floating-rate formula policy для floating-coupon инструментов.

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

### Milestone F — Confirmation-based execution — owner-approved staged plan

Status: owner-approved staged plan (see "Owner-approved deadline plan —
controlled execution by 2026-06-30" below).

This milestone is no longer "blocked forever". It is now delivered through the
controlled, manual-confirmed stages **F0 → F1 → F2 → F3 → F4**. Each stage is a
separate PR. Autonomous execution remains permanently forbidden; every order
requires explicit owner confirmation.

Any execution work must still be:

- separate branch + PR (design PR + implementation PR)
- explicit owner approval
- full safety design
- full-token discussion (execution-only env var, never printed)
- no autonomous orders
- confirmation per action
- strong preflight and kill switch

### Milestone R1 — Automated investment research intake

Status: planned.

Goal:
автоматически находить, собирать и анализировать инвестиционные материалы как
research input для income universe и риск-контроля, без торговых действий и без
авто-включения инструментов.

Useful ideas from article archive:

- Bond / OFZ research:
  - coupon schedule validation;
  - floating coupon handling for OFZ-PK;
  - maturity / liquidity / tax assumptions;
  - inflation and deposit-rate benchmark;
  - high-yield bond risk filters.
- Dividend / equity income research:
  - dividend stability checklist;
  - FCF / debt / payout sustainability;
  - distinguish announced future dividends from trailing/manual estimates;
  - keep manual/estimated income as diagnostics, not auto-enabled.
- Risk and behavior:
  - Pump & Dump / hype detection as risk flag;
  - avoid social-signal based auto-selection;
  - tilt / drawdown behavior notes;
  - falling-market mistake checklist;
  - broker / operational risk checklist.
- Automation / tooling:
  - local article/archive ingestion;
  - source reliability scoring;
  - ticker/entity extraction;
  - research digest JSON/MD;
  - Telegram research digest, no send by default;
  - candidates go to disabled/research audit only, never directly to enabled universe.
- Non-goals:
  - no trading signals;
  - no investment recommendations;
  - no auto-enable;
  - no scraping-based financial decisions;
  - no portfolio mutation;
  - no config mutation.

First implementation candidate:
`research-ingest` + `research-analyze` commands that process local files/RSS/API
sources into a read-only research digest. Output should be candidate ideas and
risk tags only. Any instrument must still pass current official/read-only
validation before appearing in income universe analysis.

Future backlog:

1. research-ingest: local ZIP/HTML/MD input, metadata extraction.
2. research-analyze: topic classification, ticker/entity extraction, risk tags.
3. research-digest: JSON/MD report with source reliability and candidate implications.
4. research-to-universe bridge: only disabled/research candidates, no auto-enable.
5. telegram research summary: optional digest, no send by default.

## Owner-approved deadline plan — controlled execution by 2026-06-30

Owner goal: by 2026-06-30 complete several small real trades to validate the full
workflow for personal income-investing use:
research/analytics → owner decision → order preview → manual confirmation →
execution → portfolio/report verification.

- This is for owner's personal use.
- This is not autonomous trading.
- This is not public investment advice.
- The bot may produce owner-only proposed actions, but execution requires manual
  confirmation.
- Initial live trades must be tiny, capped, and fully preflighted.

### Milestone F0 — Roadmap execution transition

Status: this PR.

Goal: formally replace "execution blocked forever" with "execution allowed only
through controlled manual-confirmed stages."

### Milestone F1 — Owner decision report

Status: implemented + policy refinement after this PR.

Policy refinement note: resolved `income_reliable` candidates (например, `T`,
`VTBR`) больше не зависают в `NEEDS_POLICY` только из-за disabled-статуса (builder
`--enable-mode disabled` → audit group A / `current_enabled=false`). Если у них нет
реальных hard/mapping/floating/income-policy/data блокеров, они становятся
`BUY_CANDIDATE`/`WAIT` для owner review. Это **не** исполнение и по-прежнему
требует F2 order preview / no-send и ручного подтверждения перед любой сделкой.
`income_variable` money-market (`LQDT`) сознательно остаётся `NEEDS_POLICY`
(`variable_income_policy_required`), пока нет отдельной variable-income policy.
Добавлены диагностические поля `policy_unblock_reason`, `hard_blockers[]`,
`soft_warnings[]`, `owner_review_eligible`, `why_not_buy_candidate`,
`why_not_wait` и markdown-секция «Resolved reliable candidates unblocked for owner
review».

Command: `income-owner-decision-report`.

Outputs: `data/reports/income_owner_decision_report.json` /
`income_owner_decision_report.md`.

Work:

- `modules/income_owner_decision_report.py` — read-only объединение income
  universe builder, disabled audit, coupon validation, floating-coupon policy,
  resolver/mapping diagnostics и опционального target portfolio context;
- прозрачный deterministic scoring (без ML) + `score_components`;
- `docs/income_owner_decision_report.md`;
- tests `tests/test_income_owner_decision_report.py`.

Goal:

- combine income universe, coupon validation, floating-coupon policy, resolver
  diagnostics, target portfolio context;
- produce owner-only candidate scoring;
- output `proposed_action` values:
  - `BUY_CANDIDATE`
  - `WAIT`
  - `BLOCKED`
  - `NEEDS_MAPPING`
  - `NEEDS_POLICY`
  - `NEEDS_DATA`
- no orders;
- no order preview (это уже F2);
- no full-access token;
- no portfolio mutation, no config mutation;
- `execution_requires_manual_confirmation=true`, `order_preview_required=true`,
  `order_send_allowed=false`, `auto_execution_allowed=false` для каждого
  кандидата.

Guarantees: missing входные отчёты не роняют команду (`missing_inputs` +
безопасная деградация в `NEEDS_DATA`); отсутствие любого источника кандидатов —
понятная ошибка с подсказкой про smoke chain. `proposed_action` — owner-only
proposed action (candidate for owner review), не приказ на сделку и не публичная
инвестиционная рекомендация.

Next: F2 order preview / no-send.

### Milestone F2 — Order preview, no send

Status: implemented after this PR.

Command: `income-order-preview`.

Outputs: `data/reports/income_order_preview.json` /
`data/reports/income_order_preview.md`.

Work:

- `modules/income_order_preview.py` — read-only построение order preview по
  `BUY_CANDIDATE` из F1 `income_owner_decision_report.json`; расчёт лотов,
  количества, reference price, estimated notional/commission/НКД (если данные
  безопасно доступны), cash impact, risk flags;
- `income-order-preview` CLI с `--decision-json`, `--output-json`/`--output-md`,
  `--candidate-action`, повторяемым `--ticker`, `--max-candidates`,
  `--max-order-rub` (preview cap, не лимит заявки), `--min-lots`/`--max-lots`,
  `--price-mode auto|offline|readonly-api` / `--offline`;
- `docs/income_order_preview.md`;
- tests `tests/test_income_order_preview.py`.

Guarantees:

- no orders (заявки не отправляются);
- no orders-service calls; `postOrder`/`cancelOrder` не вызываются;
- no full-access token (только read-only методы);
- no portfolio/config mutation; запись только в `data/reports/`;
- no live/sandbox execution, no autonomous trading, no market order;
- для каждого preview и в guards: `order_send_allowed=false`,
  `auto_execution_allowed=false`, `full_access_token_used=false`,
  `orders_service_used=false`, `manual_confirmation_required=true`;
- цена никогда не выдумывается: нет read-only цены/данных →
  `NEEDS_PRICE`/`PRICE_UNAVAILABLE`, lot_size недоступен → `BLOCKED`
  (`LOT_SIZE_UNAVAILABLE`), min lot > cap → `BLOCKED` (`MIN_LOT_EXCEEDS_CAP`);
- небезопасный F1-источник (`order_send_allowed != false`) → hard fail;
- manual confirmation required before F3.

Next: F3 sandbox manual-confirmed execution.

Goal (original):

- calculate lots, price, estimated amount, commission/fees if available, NKD for
  bonds if available, cash impact, risk flags;
- do not send orders;
- no orders-service calls;
- no full-access token required;
- output preview only.

### Milestone F3 — Sandbox manual-confirmed execution

Status: implemented after this PR.

Command: `income-sandbox-execute-preview`.

Outputs: `data/reports/income_sandbox_execution_report.json` /
`data/reports/income_sandbox_execution_report.md`.

Work:

- `modules/income_sandbox_execution.py` — берёт ОДНОГО `PREVIEW_READY` кандидата
  (`BUY_CANDIDATE`) из F2 `income_order_preview.json`, валидирует безопасные F2-флаги,
  строит preflight, генерирует точную фразу ручного подтверждения и может выполнить
  заявку ТОЛЬКО в sandbox (BUY/LIMIT) при пройденных gate'ах;
- sandbox-транспорт через adapter-seam (`SandboxOrderAdapter`); по умолчанию
  `UnconfiguredSandboxAdapter` (транспорт не подключён → нужен отдельный проверенный
  sandbox-wrapper PR, этап F3.1); dry-run работает без адаптера и без токена;
- `income-sandbox-execute-preview` CLI с `--ticker` (один тикер), `--preview-json`,
  `--output-json`/`--output-md`, `--sandbox-account-id`, `--max-order-rub`,
  `--max-price-deviation-bps`, `--dry-run` (default), `--send-sandbox`, `--confirm`,
  `--price-mode auto|offline|readonly-api`, `--client-order-id-prefix`;
- `docs/income_sandbox_execution.md`;
- tests `tests/test_income_sandbox_execution.py` (без реального API; sandbox-адаптер
  и read-only клиент мокаются).

Guarantees:

- sandbox only; live-заявки запрещены;
- no live order-endpoint, no live `Orders`-сервис; они не реализованы и не вызываются;
- no full-access live token (sandbox-токен только из отдельного `TINKOFF_SANDBOX_TOKEN`,
  никогда не печатается); no live account (sandbox account id передаётся явно);
- exact manual confirmation required (`CONFIRM SANDBOX BUY <T> <N> LOTS MAX <RUB> RUB`);
- no autonomous execution, no market order (только LIMIT), no Telegram-исполнения;
- no portfolio/config mutation; запись только в `data/reports/`;
- dry-run по умолчанию; один запуск = один тикер = максимум одна sandbox-заявка;
- guards: `live_order_sent=false`, `auto_execution_allowed=false`,
  `live_orders_service_used=false`, `full_access_live_token_used=false`,
  `portfolio_mutated=false`, `config_mutated=false`, `manual_confirmation_required=true`.

Next: F3.1 verified sandbox transport wrapper (этот PR), затем F4 tiny live —
отдельный PR и отдельное одобрение.

### Milestone F3.1 — Verified sandbox transport wrapper

Status: implemented after this PR (sandbox contract verified).

Command: `income-sandbox-execute-preview --sandbox-transport verified-rest …`.

F3 (PR #23) дал scaffold/dry-run с adapter-seam `SandboxOrderAdapter` и
`UnconfiguredSandboxAdapter` (реальная отправка намеренно не работала — контракт не
угадывали). F3.1 подключает **проверенный** sandbox-транспорт только после
подтверждения официального контракта.

Подтверждённый источник контракта (не догадка):

- транспорт — тот же gRPC-over-REST pattern, что у read-only
  `brokers/tinkoff/rest_client.py` (source 3: существующий проверенный REST pattern);
- сервис/методы/поля — официальные proto RussianInvestments/investAPI (source 2):
  `sandbox.proto` (`SandboxService.PostSandboxOrder` /
  `GetSandboxOrderState`, пакет `tinkoff.public.invest.api.contract.v1`) и
  `orders.proto` (`PostOrderRequest`/`PostOrderResponse`/`OrderDirection`/
  `OrderType`/`GetOrderStateRequest`).

Work:

- `modules/tinvest_sandbox_transport.py` — `VerifiedSandboxRestAdapter`
  (BUY/LIMIT only; MARKET/не-BUY → hard fail; токен только в Authorization header,
  не печатается; payload по подтверждённым полям `quantity`=лоты/`price`/`direction`/
  `accountId`/`orderType`/`orderId`/`instrumentId`); read-only `GetSandboxOrderState`.
- `modules/income_sandbox_execution.py` — `--sandbox-transport` resolver
  (`unconfigured`/`verified-rest`/`verified-sdk`), блокировки
  `SANDBOX_TRANSPORT_UNCONFIGURED` / `SANDBOX_SDK_NOT_AVAILABLE`, отчётные поля
  `sandbox_transport`, `sandbox_order_request_sanitized`,
  `sandbox_order_response_sanitized`, `sandbox_order_state_sanitized`, guards
  `token_printed=false`/`live_token_used=false`.
- `income-sandbox-execute-preview` CLI: новый флаг `--sandbox-transport`.
- tests `tests/test_tinvest_sandbox_transport.py` + дополнения в
  `tests/test_income_sandbox_execution.py` (только моки; реальный sandbox API в
  тестах/smoke/CI не вызывается).
- docs `docs/income_sandbox_execution.md`.

Guarantees: sandbox only; live-заявки запрещены; no live order-endpoint, no live
`Orders`-сервис, no full-access live token; sandbox-токен только из
`TINKOFF_SANDBOX_TOKEN`, никогда не печатается; no live account; no autonomous
execution; no market order; no portfolio/config mutation; dry-run по умолчанию; один
запуск = максимум одна sandbox-заявка; реальная sandbox-отправка только вручную
(`--sandbox-transport verified-rest` + `--send-sandbox` + sandbox account id + точная
фраза `--confirm`), отдельной командой после PR.

Next: F4 tiny live manual-confirmed order — отдельный PR и отдельное одобрение.

### Milestone F3.2 — Sandbox account bootstrap

Status: implemented after this PR.

Command: `income-sandbox-account` (actions `status`/`list`/`open`/`pay-in`).

Outputs: `data/reports/income_sandbox_account_report.json` /
`data/reports/income_sandbox_account_report.md`.

Purpose:

- получить/создать sandbox account id;
- при необходимости пополнить sandbox-счёт sandbox-деньгами;
- никаких заявок; никакого live; никакой мутации портфеля/конфига.

Подтверждённый источник контракта (не догадка) — тот же, что у F3.1:

- транспорт — gRPC-over-REST pattern read-only `brokers/tinkoff/rest_client.py`;
- сервис/методы/поля — официальные proto RussianInvestments/investAPI:
  `sandbox.proto` (`SandboxService.GetSandboxAccounts` / `OpenSandboxAccount` /
  `SandboxPayIn`), `users.proto` (`Account`: id/type/name/status/openedDate/
  accessLevel), `common.proto` (`MoneyValue`: currency/units/nano); пакет
  `tinkoff.public.invest.api.contract.v1`.

Work:

- `modules/income_sandbox_account.py` — оркестрация status/list/open/pay-in,
  sanitize ответов, отчёт json+md, guards;
- `modules/tinvest_sandbox_transport.py` — методы sandbox-счёта
  `get_sandbox_accounts` / `open_sandbox_account` / `sandbox_pay_in`
  (sandbox-only; токен только в Authorization header, не печатается);
- `income-sandbox-account` CLI: `--action`, `--sandbox-transport`
  (`verified-rest`/`unconfigured`), `--sandbox-account-id`, `--pay-in-rub`,
  `--confirm`, `--dry-run`, `--output-json`/`--output-md`;
- `docs/income_sandbox_account.md`;
- tests `tests/test_income_sandbox_account.py` (только моки; реальный sandbox API
  в тестах/smoke/CI не вызывается).

Guarantees: sandbox only; заявки (LIVE и sandbox) не отправляются; no live
order-endpoint, no live `Orders`-сервис, no full-access live token; sandbox-токен
только из `TINKOFF_SANDBOX_TOKEN`, никогда не печатается/не пишется в отчёт; live-
токен не используется; no live account; no autonomous trading; no portfolio/config
mutation; no Telegram; `status` не вызывает API; `open`/`pay-in` мутируют sandbox
только при точной фразе подтверждения (`CONFIRM SANDBOX ACCOUNT OPEN` /
`CONFIRM SANDBOX PAYIN <RUB> RUB`); реальные open/pay-in — только вручную после PR.

Next:

- manual one-shot sandbox order через F3.1 transport.

F4 остаётся заблокированным, пока:

1. F3.2 sandbox account существует;
2. при необходимости сделан sandbox pay-in;
3. F3.1 one-shot sandbox order вручную отправлен и зафиксирован в отчёте;
4. одобрен отдельный F4 PR.

### Milestone F4 — Tiny live manual-confirmed order

Status: blocked — отдельный PR и отдельное одобрение. Допускается только после
того, как все условия выполнены по порядку:

1. F2 preview существует (implemented);
2. F3 dry-run проходит (implemented);
3. F3.1 verified sandbox transport существует (implemented этим PR);
4. хотя бы одна реальная sandbox-заявка прогнана **вручную** и зафиксирована в
   отчёте (manual one-shot, не CI);
5. одобрен отдельный F4 PR.


Goal:

- 1–3 very small real orders before 2026-06-30;
- limit orders only unless owner explicitly approves otherwise;
- explicit confirmation phrase;
- max order amount cap;
- allowlist of instruments;
- cash/lot/price/status preflight;
- no scheduled execution;
- post-trade report.

### Schedule

- 2026-06-22: roadmap transition PR
- 2026-06-23: owner decision report PR
- 2026-06-24: order preview / no-send PR
- 2026-06-25: sandbox manual-confirm flow PR
- 2026-06-26: pre-live checklist and tiny-live design
- 2026-06-27 to 2026-06-30: 1–3 tiny manual-confirmed live orders if all gates pass

### Safety gates for live execution

- LIVE execution disabled by default
- full-access token never printed
- separate env var for execution token
- explicit account id display before confirmation
- instrument allowlist
- max order amount cap
- limit orders for first live trades
- price deviation check
- lot size check
- available cash check
- trading status check
- duplicate order guard
- dry-run default
- confirmation phrase required
- Telegram send optional, never execution by default
- kill switch / emergency disable
- complete post-trade report

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
