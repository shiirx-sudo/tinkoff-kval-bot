# Owner income decision report (ROADMAP F1)

`income-owner-decision-report` — это read-only, **owner-only** отчёт поддержки
решений. Он объединяет существующие диагностические отчёты в единую таблицу
кандидатов и помечает каждый кандидат owner-only значением `proposed_action`.

Это **этап F1** контролируемого staged-плана исполнения (F1 → F2 → F3 → F4). F1
**не отправляет заявки**, **не делает order preview**, **не использует full-access
токен** и **не мутирует** ни портфель, ни config. Это аналитика для ручного
review владельца, а не публичная инвестиционная рекомендация и не приказ на
сделку.

## Purpose

- собрать в одном месте, какие income-инструменты владелец может **рассмотреть**
  для следующего шага, а какие пока заблокированы и почему;
- дать прозрачный, детерминированный score и `proposed_action` по каждому
  кандидату;
- подготовить материал для F2 (order preview / no-send), не приближаясь к
  исполнению.

## Inputs

Все входы — локальные read-only отчёты (сети/API нет):

| опция CLI | по умолчанию | источник |
|---|---|---|
| `--universe-report` | `data/reports/income_universe_builder_report.json` | `build-income-universe` |
| `--audit-json` | `data/reports/income_universe_disabled_audit.json` | `income-universe-audit` |
| `--coupon-json` | `data/reports/income_coupon_validation.json` | `income-coupon-validation` |
| `--floating-policy-json` | `data/reports/income_floating_coupon_policy.json` | `income-floating-coupon-policy` |
| `--resolver-json` | `data/reports/income_resolver_mapping_diagnostics.json` | `income-resolver-mapping-diagnostics` |
| `--target-json` | `data/reports/target_portfolio.json` | `target-portfolio` (опционально) |

Прочие опции: `--output-json`, `--output-md`, `--max-candidates` (по умолчанию
30), `--min-score` (опциональный фильтр), `--offline` (по умолчанию и так без
сети).

Если какого-то входного отчёта нет, команда **не падает**: путь добавляется в
`missing_inputs`, отчёт деградирует безопасно (там, где данных не хватает —
`NEEDS_DATA`). Если не найден **ни один** источник кандидатов — выдаётся понятная
ошибка с командами smoke chain, которые нужно выполнить сначала.

## Outputs

- `data/reports/income_owner_decision_report.json`
- `data/reports/income_owner_decision_report.md`

JSON-схема верхнего уровня: `generated_at`, `mode`, `inputs`, `missing_inputs`,
`summary`, `candidates`, `guards`.

Каждый кандидат содержит идентификацию (ticker/figi/uid/isin/class_code/name),
`asset_type`/`source_role`, `policy_bucket`, `audit_group`/`audit_reason`,
`coupon_status`, `floating_policy_status`, `resolver_mapping_status`,
`income_readiness`, поля доходности (`estimated_yield`/`conservative_yield`/
`net_yield_pct`), `risk_flags[]`, `missing_data[]`, `score`, `score_components{}`,
`proposed_action`, `proposed_action_reason`, `next_required_step` и жёсткие
guard-флаги (см. ниже).

Диагностика policy-классификации (добавлено F1 policy refinement):

- `hard_blockers[]` — реальные hard blocker'ы (excluded policy, cap/override,
  keep-disabled group E, hard `state_control_risk`); непустой список → `BLOCKED`;
- `soft_warnings[]` — non-hard risk warnings (например, soft `state_control_risk`):
  не блокируют и не делают NEEDS_POLICY, но не дают форсировать `BUY_CANDIDATE`
  (кандидат остаётся `WAIT`);
- `owner_review_eligible` — `true`, если кандидат resolved и income-ready без
  mapping/hard/floating/policy/data блокеров (становится `BUY_CANDIDATE`/`WAIT`);
- `policy_unblock_reason` — почему resolved income-ready кандидат НЕ `NEEDS_POLICY`,
  хотя builder/audit пометили его disabled (manual_audit / `current_enabled=false`);
- `why_not_buy_candidate` / `why_not_wait` — короткая диагностика, почему статус
  не `BUY_CANDIDATE` / не `WAIT`.

## proposed_action enum

Owner-only значения (это статусы для review, не приказы):

- `BUY_CANDIDATE` — инструмент resolved, нет hard blocker / mapping / floating
  policy / critical missing data, проходит минимальный score, нет hard risk flag.
  Это **candidate for owner review**, а не покупка.
- `WAIT` — потенциально интересен, но не хватает score/доходности/уверенности,
  либо есть risk flag; нужен дополнительный review.
- `BLOCKED` — явный hard blocker: excluded policy (`income_excluded`),
  `override_disable` / `trailing_yield_above_cap`, hard `state_control_risk`,
  keep-disabled (audit group E), unknown-income bucket.
- `NEEDS_MAPPING` — audit group D или resolver `mapping_status`
  unresolved/no_matches/ambiguous; нет проверенного secid/ISIN/ticker/class_code.
- `NEEDS_POLICY` — floating coupon без утверждённой формулы, coupon unknown,
  manual/estimated income bucket (`income_estimated`/`income_manual`, group B),
  variable income money-market (`variable_income_policy_required`), coupon
  future-policy review.
- `NEEDS_DATA` — не хватает данных для решения (нет купонного календаря/частоты/
  цены, нет income/yield метрик, отсутствуют входные отчёты).

Приоритет оценки (от наиболее блокирующего): `NEEDS_MAPPING` → `BLOCKED` →
`NEEDS_POLICY` → `NEEDS_DATA` → `BUY_CANDIDATE`/`WAIT` (по score и soft warnings).

### F1 policy refinement: разблокировка resolved reliable кандидатов

**Инвариант:** `current_enabled=false` (builder запущен в `--enable-mode disabled`)
и связанная с этим audit-группа A (`manual_audit`) — это **состояние universe**, а
**не policy-блокер**. Сам по себе disabled-статус больше не делает resolved
`income_reliable` кандидата `NEEDS_POLICY` (раньше триггер `audit_group in (A, B)`
загонял T/VTBR в NEEDS_POLICY без реальной причины).

Resolved `income_reliable` кандидат становится **owner-review eligible**
(`BUY_CANDIDATE` при score ≥ порога, иначе/при soft risk → `WAIT`), если:

- есть resolved identity (class_code и не group D / resolver unresolved);
- `policy_bucket == income_reliable`, audit_group не D;
- нет resolver mapping blocker;
- нет floating coupon policy blocker;
- нет hard excluded / keep-disabled blocker;
- нет critical missing income data;
- нет **hard** risk flag.

`income_variable` money-market (например, `LQDT`) сознательно **не** форсируется в
`BUY_CANDIDATE`: пока нет утверждённой variable-income policy, он остаётся
`NEEDS_POLICY` с честной причиной `variable_income_policy_required`.

`state_control_risk` различает два режима: как hard excluded reason (+ group E) →
`BLOCKED`; как soft warning во входном `risk_flags[]` → `WAIT` (но никогда
`NEEDS_POLICY`, если это resolved income_reliable).

Markdown-отчёт содержит секцию **«Resolved reliable candidates unblocked for owner
review»** — кандидаты, которые раньше зависали бы в `NEEDS_POLICY` только из-за
disabled-статуса, а теперь являются `BUY_CANDIDATE`/`WAIT`. Это **не** исполнение:
`order_send_allowed=false`, `auto_execution_allowed=false`,
`execution_requires_manual_confirmation=true`; перед сделкой обязательны F2 order
preview / no-send и ручное подтверждение.

## Scoring model

Прозрачный детерминированный score 0..100, без ML. Каждая компонента — отдельный
вклад в `score_components`:

| компонента | вклад |
|---|---|
| `income_data_present` | +20 |
| `conservative_income_bucket` (`income_reliable` +15 / `income_variable` +8) | +15 / +8 |
| `resolved_identity` | +15 |
| `fixed_or_known_income` | +10 |
| `target_underweight_context` | +10 |
| `missing_data_penalty` | −15 |
| `floating_policy_penalty` | −20 |
| `unresolved_mapping_penalty` | −25 |
| `excluded_unknown_policy_penalty` | −20 |
| `risk_penalty` (за каждый risk flag) | −10 |

Сумма клампится в `[0, 100]`. Порог `BUY_CANDIDATE` — `score >= 50`; иначе
resolved income-ready кандидат становится `WAIT`. Модель сознательно простая:
важна прозрачность `score_components` и стабильность тестов, а не идеальная
калибровка.

## Why no order is sent in F1

F1 — это только **decision support**. Для каждого кандидата жёстко зафиксировано:

- `execution_requires_manual_confirmation = true`
- `order_preview_required = true`
- `order_send_allowed = false`
- `auto_execution_allowed = false`

В `summary`: `order_send_allowed_count = 0`, `auto_execution_allowed_count = 0`,
`execution_requires_manual_confirmation_count = total_candidates`. В блоке
`guards`: `full_access_token_used=false`, `portfolio_mutated=false`,
`config_mutated=false`, `next_stage="F2 order preview / no-send"`.

Автономное исполнение запрещено всегда; любая будущая сделка требует явного
ручного подтверждения владельца.

## How this leads to F2 (order preview)

`BUY_CANDIDATE` строки — это вход для **F2 order preview / no-send**: на F2
считаются лоты, цена, ориентировочная сумма, комиссии/НКД (если доступно),
влияние на кэш и risk-флаги — **без отправки заявок** и **без full-access
токена**. Только после F2 (и далее F3 sandbox, F4 tiny live) и явного ручного
подтверждения возможно какое-либо исполнение.

## Validation checklist

```powershell
$env:LIVE_ENABLED="false"
python main.py doctor
python -m pytest -q
ruff check .

# smoke chain (создаёт входные отчёты, затем decision report)
python main.py build-income-universe --force
python main.py income-universe-audit
python main.py income-coupon-validation
python main.py income-floating-coupon-policy
python main.py income-resolver-mapping-diagnostics
python main.py income-owner-decision-report
python main.py telegram-summary --send false --dry-run true
```

Проверки безопасности:

- safety/order grep по `modules/*.py`, `main.py`, `reports/*.py`,
  `notifications/*.py` (`postOrder|cancelOrder|OrdersService|place_order|...`) —
  только guard/negative phrases и tests;
- full-access grep — только guard/negative phrases/docs/tests;
- wording grep по `income_owner_decision_report.md`
  (`купить сейчас|продать сейчас|отправить заявку|гарантированная доходность|
  guaranteed income|safe profit`) — нет совпадений;
- strict assertion: `total_candidates == len(candidates)`,
  `order_send_allowed_count == 0`, `auto_execution_allowed_count == 0`, все строки
  с `execution_requires_manual_confirmation=true` / `order_send_allowed=false` /
  `auto_execution_allowed=false`.
