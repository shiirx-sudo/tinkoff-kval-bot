# portfolio-dashboard-data (F4.8 — read-only portfolio dashboard data model)

> 🔍 **READ-ONLY модель данных.** F4.8 — это **данные**, а не UI и не торговля. Она
> агрегирует портфельные метрики в структурированный JSON/MD, который будет
> рендерить будущий дашборд **F4.9**. Команда **ничего не исполняет**, не отменяет,
> не продаёт, не повторяет и **ничего не угадывает**.

Это **портфельный** уровень, а не «экран одной сделки». Первая сделка по T — лишь
**одно событие** в истории (`last_trade_audit_summary`), а не центр отчёта. Старые
позиции (например 27 шт. T) — реальные инвестиции и учитываются в портфельных
метриках.

## Какие вопросы закрывает модель (для F4.9)

1. Сколько стоит портфель сейчас? → `portfolio_summary.total_portfolio_value_rub`
2. Сколько свободного кэша? → `cash_summary.cash_rub` / `cash_pct`
3. Сколько дохода к цели в месяц? → `income_summary.total_income_monthly_conservative_rub`
   (scheduled дивиденды/купоны + только реализованный net стратегии)
4. Насколько близко к цели 150 000 ₽/мес.? → `income_summary.target_coverage_conservative_pct` / `income_gap_conservative_rub_monthly`
5. Какой оборот к цели 6 000 000 ₽ за trailing 4 квартала? →
   `turnover_summary.kval_turnover_trailing_4q_rub` / `kval_turnover_progress_pct` /
   `kval_turnover_gap_rub` (YTD-оборот `turnover_ytd_rub`/`turnover_ytd_progress_pct`
   остаётся вторичной метрикой)
6. Взносы по плану или пропущены? → `contributions_summary`
7. Есть ли критичные риски? → `risk_summary`

## Источники и partial-режим

- Всегда: локальные отчёты **F4.1–F4.6** (`data/reports/*.json`).
- Опционально: read-only `TINKOFF_TOKEN` для портфеля/операций/рыночных
  данных/дивидендов (обогащает портфель и реальный оборот).
- **`TINKOFF_LIVE_TRADING_TOKEN` и `TINKOFF_SANDBOX_TOKEN` не используются.**
- Если полный портфель/история операций через API недоступны — отчёт **partial**:
  позиции из F4.3, оборот из одной известной сделки F4.4 (явно `turnover_partial`),
  кэш = null. `data_freshness.overall = "partial"`. Команда всё равно завершается
  успешно (exit `0`).

## Определения и цели

- Цель дохода (income target): `monthly_income_target_rub = 150000` (база `2026-06`).
  F4.11: доход к цели = **scheduled** (дивиденды/купоны) + **strategy** (бот/стратегия,
  пока плейсхолдер). «Пассивный доход» — теперь подкатегория (scheduled), не вся
  модель. См. `docs/portfolio_dashboard.md` → «Доход к цели (F4.11)».
- Цель оборота (путь к квалинвестору): `6 000 000 ₽ за trailing 4 квартала`, с
  пропорциональными ориентирами `500 000 ₽/мес.` и `1 500 000 ₽/квартал`.
- **Оборот = `sum(abs(gross BUY) + abs(gross SELL))`** (до комиссии). Дивиденды и
  купоны — это **доход/cashflow**, а **НЕ оборот**. Комиссии учитываются **отдельно**
  (`commissions_*`), не как оборот.

## Правила расчёта (без угадывания)

- Месячный scheduled-доход (брутто) = годовой ожидаемый доход / 12.
- Консервативный доход к цели = scheduled (брутто) + только реализованный net
  стратегии (по умолчанию 0). Paper/model в покрытие **не** входят.
- Покрытие цели (консервативно) = доход к цели / 150 000 × 100.
- Income gap (консервативно) = 150 000 − доход к цели.
- Legacy-алиасы (`passive_income_rub_monthly_gross`, `income_target_coverage_pct`,
  `income_gap_rub_monthly`, `target_monthly_income_rub`) сохранены для
  совместимости и равны новым полям scheduled/conservative.
- **Net-доход** при неизвестном налоговом режиме **не считается** (null + warning).
- Одно будущее событие не аннуализируется, если источник этого не поддерживает.
- BUY-оборот = gross покупки до комиссии; SELL-оборот = gross продажи до комиссии.
- Требуемый капитал = (150 000 × 12) / `required_capital_assumption_yield_pct` —
  это **явное допущение** доходности (поле в отчёте), не реальная доходность.
- Любая невычислимая метрика = `null` + warning. Позиции/кэш/операции/дивиденды/
  взносы **не выдумываются**.

## Взносы (contributions)

ПЛАН (цели/старт/график) — локальный, в **`data/config/contribution_plan.json`**
(не коммитится). ФАКТ пополнений (F4.10.1) по умолчанию **API-based** — извлекается
из read-only операций брокера тем же путём, что и оборот: депозиты
(`OPERATION_TYPE_INPUT`) = взносы, выводы (`OPERATION_TYPE_OUTPUT`) трекаются
**отдельно** (net cash flow). Ручные `facts[]` — только fallback/корректировки.
Конфиг-дефолты: `fact_source=api_operations`, `manual_facts_enabled=false`. Если
плана нет — `contributions_tracking_enabled=false` и warning
`contribution_plan_not_configured`. Если API-операции недоступны — manual fallback с
warning `contribution_api_operations_unavailable_manual_fallback`. Пример полей — в
**`config/contribution_plan.example.json`** (закоммичен): `enabled`,
`plan_weekly_rub`, `plan_monthly_rub`, `plan_start_date`,
`next_planned_contribution_date`, `source`, `fact_source`, `manual_facts_enabled`,
`facts: [{date, amount_rub}]`. Подробности — `docs/contribution_plan.md`.

## Путь к цели (target_path_summary, F4.12)

Блок `target_path_summary` отвечает: сколько **капитала** нужно, чтобы целевой
месячный доход обеспечивался при разных допущениях годовой доходности, и как этого
достичь текущими плановыми взносами. Это **простая статическая модель** планирования.

Поля верхнего уровня: `monthly_income_target_rub` (из
`income_summary.monthly_income_target_rub`, по умолчанию 150000),
`annual_income_target_rub` (= месячная цель × 12), `current_capital_rub` (=
`portfolio_summary.total_portfolio_value_rub`), `current_planned_monthly_contribution_rub`
(= `contributions_summary.contribution_plan_monthly_rub`, если учёт взносов включён),
`model = "simple_no_growth_no_return"`, `yield_scenarios[]`, `warnings`.

Сценарии доходности: **8%, 10%, 12%, 15%, 18%**. Для каждого:

- `required_capital_rub = annual_income_target_rub / (yield_pct / 100)`
  (например, 1 800 000 / 0.10 = 18 000 000);
- `capital_gap_rub = max(required_capital_rub − current_capital_rub, 0)`;
- `months_to_target_at_current_contribution = capital_gap_rub /
  current_planned_monthly_contribution_rub`; `years = months / 12`;
- `required_monthly_contribution_{3,5,10,15}y_rub = capital_gap_rub / {36,60,120,180}`.

**Ограничения модели (важно):**

- без роста рынка, без реинвестирования, без налогов, без инфляции;
- **прогнозные дивидендные доходности — это исследовательские допущения, а НЕ
  подтверждённый доход**; они НЕ входят в консервативное покрытие цели;
- зависит от плана взносов: если он отсутствует/выключен или взнос = 0, поля
  `months_to_target_*`/`years_to_target_*` = `null` + warning
  `target_path_contribution_plan_not_configured`;
- если текущий капитал уже ≥ требуемого — gap/месяцы/годы/требуемый взнос = 0.

Всегда добавляются предупреждения `target_path_simple_model_no_growth_no_return` и
`target_path_not_investment_advice`. Это **не** прогноз доходности и **не**
инвестиционная рекомендация.

## CLI

```powershell
# Partial-режим (достаточно локальных отчётов; токен не обязателен):
python main.py portfolio-dashboard-data --live-account-id <ACCOUNT_ID>

# С read-only обогащением — задайте ТОЛЬКО аналитический токен:
$env:TINKOFF_TOKEN = "<read-only analytics token>"
python main.py portfolio-dashboard-data --live-account-id <ACCOUNT_ID>
```

| Опция | По умолчанию | Назначение |
| --- | --- | --- |
| `--live-account-id` | — (обязательно) | live account id (маскируется в отчёте) |
| `--reports-dir` | `data/reports` | каталог F4.1–F4.6 (только чтение) |
| `--contribution-plan` | `data/config/contribution_plan.json` | план взносов (локальный) |
| `--output-json` | `data/reports/portfolio_dashboard_data.json` | JSON |
| `--output-md` | `data/reports/portfolio_dashboard_data.md` | Markdown |

## Отчёт

`data/reports/portfolio_dashboard_data.json` и `.md` (gitignored). Верхнеуровневые
блоки: `portfolio_summary`, `positions[]`, `cash_summary`, `income_summary`,
`turnover_summary`, `contributions_summary`, `risk_summary`,
`last_trade_audit_summary`, `dashboard_kpi` (модель шапки будущего дашборда),
`data_freshness`, `data_sources_used/missing`, `warnings`, `errors`, `token_policy`,
`guards`.

`guards`/`token_policy` фиксируют read-only контракт: `live_order_sent=false`,
`post_order_called=false`, `cancel_order_called=false`, `sell_order_sent=false`,
`market_order_used=false`, `retry_execution=false`, `portfolio_mutated=false`,
`config_mutated=false`, `telegram_sent=false`, `live_token_used=false`,
`sandbox_token_used=false`, `token_printed=false`. Значения токенов не печатаются и
не пишутся; account id маскируется.

## Дальше

F4.8 — только модель данных. **F4.9** отрендерит её как портфельный кокпит
(read-only), но **по-прежнему без торговли**.
