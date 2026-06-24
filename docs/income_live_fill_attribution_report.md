# income-live-fill-attribution (F4.4 — read-only fill attribution)

> 🔍 **READ-ONLY атрибуция.** Эта команда только **разбирает** завершённую сделку:
> отделяет НОВЫЙ исполненный лот от уже имевшейся позиции, подтягивает комиссию из
> операций (если доступна) и считает вклад новой сделки в текущий PnL и income-цель.
> Она **ничего** не исполняет, не отменяет, не продаёт и не повторяет.

F4.3 показал, что текущая позиция больше 1 лота (например 27 units), значит до
новой сделки уже были прежние позиции. F4.4 отделяет новую сделку от прежней.

## Что F4.4 делает и чего НЕ делает

- ✅ читает (опционально) отчёты F4.1/F4.2/F4.3;
- ✅ читает **read-only** операции/сделки (и при необходимости портфель/рыночные
  данные) через T-Invest;
- ✅ сопоставляет новую сделку с операцией и подтягивает комиссию (если есть);
- ✅ считает стоимость и нереализованный PnL **именно новой сделки**, отдельно от
  суммарного PnL всей позиции;
- ✅ оценивает прежнюю позицию (units/среднее), **явно** помечая это как оценку;
- ✅ соотносит вклад новой сделки с месячной корзиной `150000 RUB` — только при
  надёжных данных о доходе;
- ❌ **не** вызывает PostOrder, **не** отменяет, **не** продаёт, **не** ретраит,
  **не** использует MARKET; **не** мутирует портфель/config; **не** шлёт Telegram;
- ❌ **не** требует/не использует `TINKOFF_LIVE_TRADING_TOKEN`;
- ❌ **не** использует `TINKOFF_SANDBOX_TOKEN`.

## Уровни уверенности (confidence) и fallback

| Уровень | Когда | Метод |
| --- | --- | --- |
| `high` | операция найдена по `order_id` (+ инструмент/количество) | `operations_order_id_match` |
| `medium` | нет связи по order_id, но совпали инструмент + BUY + количество (+цена/дата) | `operations_instrument_qty_price_date_match` / `operations_instrument_qty_match` |
| `low` | операций нет/нет совпадения — берём только F4.1/F4.2/F4.3 | `reports_only_derived` |

T-Invest `GetOperations` обычно **не** содержит брокерский `order_id`, поэтому в
реальности уверенность чаще `medium` (совпадение по инструменту/количеству/цене/
дате). `high` достигается только если операция несёт поле order_id.

## Без угадывания

- **Комиссия и денежный отток (BUY)**: брокер для покупки часто отдаёт комиссию
  со знаком **минус** (это отток денег, нормальная конвенция, не ошибка —
  предупреждения при отрицательной комиссии **нет**). Поэтому отчёт хранит:
  `fill_commission_raw` (сырое значение со знаком, напр. `-0.14`),
  `fill_commission_abs` (модуль, напр. `0.14`) и
  `fill_cash_outflow = fill_gross_amount + fill_commission_abs`
  (`fill_cash_outflow_formula`). Для реального кейса:
  `276.08 + 0.14 = 276.22`. Поле `fill_commission` сохранено для обратной
  совместимости и равно **сырой** комиссии со знаком; `fill_net_amount` сохранено
  и для BUY **равно** `fill_cash_outflow` (а не `gross + signed`). Если комиссия
  недоступна — `fill_commission_raw/abs = null`, `fill_cash_outflow = null`
  (частичный: известен только gross) и предупреждение. Не угадываем.
- **Доход/дивиденды**: `estimated_income_contribution_*` и
  `income_target_coverage_pct` заполняются только при надёжных данных; иначе `null`
  + предупреждение.
- **Прежняя позиция**: реконструируется из текущего среднего и цены новой сделки
  (`prev_units = total − fill`; `prev_avg = (avg·total − fill_price·fill)/prev_units`)
  и **помечается как оценка**: комиссия не учтена (если неизвестна), брокер может
  считать среднее своим методом, значение **информационное, не авторитетное**.
  Подтверждается только полной историей операций.

## Разделение PnL

`current_total_unrealized_pnl` (из F4.3) относится ко **всей** позиции (все units).
`estimated_new_fill_unrealized_pnl = (current_price − fill_price) · fill_units`
считается **только** для нового лота и держится отдельно. Для текущего случая:
total ≈ −965.52 RUB на 27 units; вклад нового 1 лота ≈ −7.88 RUB.

## Token policy

- Только аналитический read-only `TINKOFF_TOKEN`
  (operations/portfolio/market-data). Значение не печатается.
- `TINKOFF_LIVE_TRADING_TOKEN` не требуется/не используется
  (`live_token_used=false`); `TINKOFF_SANDBOX_TOKEN` не используется.
- Без read-only токена команда падает **чисто** (exit `1`) **без сетевых вызовов**.
- account id в отчёте маскируется.

## CLI

```powershell
$env:TINKOFF_TOKEN = "<read-only analytics token>"
python main.py income-live-fill-attribution `
  --ticker T --order-id 80578688754 --live-account-id <ACCOUNT_ID>
```

| Опция | По умолчанию | Назначение |
| --- | --- | --- |
| `--ticker` | `T` | тикер |
| `--order-id` | — (обязательно) | id завершённой live-заявки |
| `--live-account-id` | — (обязательно) | live account id |
| `--f41-report` | `data/reports/income_live_execution_report.json` | F4.1 (чтение) |
| `--f42-report` | `data/reports/income_live_order_status_report.json` | F4.2 (чтение) |
| `--f43-report` | `data/reports/income_live_position_report.json` | F4.3 (чтение) |
| `--output-json` | `data/reports/income_live_fill_attribution_report.json` | JSON |
| `--output-md` | `data/reports/income_live_fill_attribution_report.md` | Markdown |

## Отчёт

`data/reports/income_live_fill_attribution_report.json` и `.md` (gitignored).
Ключевые поля: `stage` (`F4_4_LIVE_FILL_ATTRIBUTION_READ_ONLY`), `mode`
(`FILL_ATTRIBUTION_READ_ONLY`), идентификаторы заявки/инструмента,
`fill_*` (новая сделка), `current_total_*` (вся позиция, отдельно),
`estimated_new_fill_*` (вклад новой сделки), `estimated_previous_*` (оценка прежней
позиции) + `old_position_estimation_warning`, `fill_attribution_confidence`,
`attribution_method`, income-goal поля + `income_estimation_warning`, `checked_at`,
`guards`, `token_policy`, `warnings`, `errors`.

`guards` фиксируют read-only контракт: `live_order_sent=false`,
`post_order_called=false`, `cancel_order_called=false`, `sell_order_sent=false`,
`market_order_used=false`, `retry_execution=false`, `portfolio_mutated=false`,
`config_mutated=false`, `telegram_sent=false`, `live_token_used=false`,
`sandbox_token_used=false`, `token_printed=false`.
