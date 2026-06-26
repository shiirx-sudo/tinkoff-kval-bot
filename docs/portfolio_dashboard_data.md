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
3. Сколько пассивного дохода в месяц? → `income_summary.passive_income_rub_monthly_gross`
4. Насколько близко к цели 150 000 ₽/мес.? → `income_summary.income_target_coverage_pct` / `income_gap_rub_monthly`
5. Какой оборот к цели 60 000 000 ₽/год? → `turnover_summary.turnover_ytd_rub` / `turnover_ytd_progress_pct`
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

- Цель пассивного дохода: `base_monthly_living_basket_rub = 150000` (база `2026-06`).
- Цель оборота: год `60 000 000 ₽`, месяц `5 000 000 ₽`, квартал `15 000 000 ₽`.
- **Оборот = `sum(abs(gross BUY) + abs(gross SELL))`** (до комиссии). Дивиденды и
  купоны — это **доход/cashflow**, а **НЕ оборот**. Комиссии учитываются **отдельно**
  (`commissions_*`), не как оборот.

## Правила расчёта (без угадывания)

- Месячный пассивный доход (брутто) = годовой ожидаемый доход / 12.
- Покрытие цели = месячный доход / 150 000 × 100.
- Income gap = 150 000 − месячный доход.
- **Net-доход** при неизвестном налоговом режиме **не считается** (null + warning).
- Одно будущее событие не аннуализируется, если источник этого не поддерживает.
- BUY-оборот = gross покупки до комиссии; SELL-оборот = gross продажи до комиссии.
- Требуемый капитал = (150 000 × 12) / `required_capital_assumption_yield_pct` —
  это **явное допущение** доходности (поле в отчёте), не реальная доходность.
- Любая невычислимая метрика = `null` + warning. Позиции/кэш/операции/дивиденды/
  взносы **не выдумываются**.

## Взносы (contributions)

Депозиты read-only API надёжно не отдаёт, поэтому факт пополнений ведётся вручную
(manual model). Нужен локальный план-конфиг **`data/config/contribution_plan.json`**
(не коммитится). Если его нет — `contributions_tracking_enabled=false` и warning
`contribution_plan_not_configured`. Пример полей — в
**`config/contribution_plan.example.json`** (закоммичен): `enabled`,
`plan_weekly_rub`, `plan_monthly_rub`, `plan_start_date`,
`next_planned_contribution_date`, `source`, `facts: [{date, amount_rub}]`.

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
