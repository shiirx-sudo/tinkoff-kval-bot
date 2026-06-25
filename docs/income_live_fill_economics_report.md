# income-live-fill-economics (F4.5 — read-only fill net PnL & position economics)

> 🔍 **READ-ONLY экономика/отчётность.** Эта команда только **считает экономику**
> уже завершённой сделки (1 лот) поверх атрибуции F4.4: gross vs net PnL,
> комиссионный drag, цену безубытка и расстояние до неё, долю новой сделки в
> позиции и (только при надёжных данных) вклад в месячную корзину `150000 RUB`.
> Она **ничего** не исполняет, не отменяет, не продаёт и не повторяет.

F4.4 отделил новую сделку от прежней позиции и подтянул комиссию. F4.5 берёт эти
read-only данные и считает **экономику именно новой сделки**, держа её **отдельно**
от PnL всей позиции.

## Что F4.5 делает и чего НЕ делает

- ✅ читает F4.4 отчёт (`income_live_fill_attribution_report.json`) как **основной**
  источник; опционально F4.3/F4.2/F4.1 как дополнительный контекст;
- ✅ считает **gross PnL** (до комиссии) и **net PnL** (после комиссии/денежного
  оттока) новой сделки;
- ✅ считает комиссионный **drag** (рубли и % от gross-суммы);
- ✅ считает **цену безубытка после комиссии** и расстояние до неё;
- ✅ считает **долю** новой сделки в суммарной позиции;
- ✅ соотносит вклад новой сделки с месячной корзиной `150000 RUB` — **только** при
  надёжных данных о доходе (наследуется из F4.4, без угадывания);
- ❌ **не** вызывает PostOrder, **не** отменяет, **не** продаёт, **не** ретраит,
  **не** использует MARKET; **не** мутирует портфель/config; **не** шлёт Telegram;
- ❌ **не** требует/не использует `TINKOFF_LIVE_TRADING_TOKEN`;
- ❌ **не** использует `TINKOFF_SANDBOX_TOKEN`.

## Gross vs net PnL

Ключевое различие, которое и считает F4.5:

- **gross PnL** = `current_price · units − fill_gross_amount` — нереализованный PnL
  новой сделки **без** учёта комиссии (по «грязной» сумме сделки).
- **net PnL** = `current_price · units − fill_cash_outflow` — нереализованный PnL
  **с** учётом комиссии (по фактическому денежному оттоку покупки
  `gross + |commission|`).

Для реального кейса (T, 1 лот):

| Величина | Значение |
| --- | --- |
| `fill_price` / `fill_gross_amount` | `276.08` |
| `fill_commission_abs` | `0.14` |
| `fill_cash_outflow` | `276.22` (= `276.08 + 0.14`) |
| `current_price` | `268.26` |
| `new_fill_current_value` | `268.26` (= `268.26 · 1`) |
| **gross** `new_fill_gross_unrealized_pnl` | `−7.82` (= `268.26 − 276.08`) |
| **net** `new_fill_net_unrealized_pnl_after_commission` | `−7.96` (= `268.26 − 276.22`) |
| `commission_drag_rub` | `0.14` |
| `break_even_price_after_commission` | `276.22` (= `276.22 / 1`) |
| `distance_to_break_even_rub` | `−7.96` (= `268.26 − 276.22`) |

Разница между gross и net (`−7.82` против `−7.96`) — это и есть комиссионный drag
(`0.14 RUB`): комиссия делает безубыток выше цены сделки.

## Разделение PnL всей позиции и новой сделки

`current_total_unrealized_pnl` (из F4.3/F4.4) относится ко **всей** позиции (все
units, например `−965.52 RUB` на 27 units) и в отчёте имеет
`total_position_pnl_kept_separate = true`. PnL **новой сделки** (`−7.82` gross /
`−7.96` net на 1 лот) — **отдельная** величина.

**Среднее всей позиции (`current_average_position_price`) НЕ используется** для
расчёта PnL новой сделки: экономика новой сделки считается строго от её собственной
цены/денежного оттока и текущей цены. Так данные сделки не «смешиваются» со средним
по всей позиции.

## Без угадывания

- **Комиссия недоступна** → `commission_drag_rub`, `break_even_*`,
  net-after-commission поля = `null` + предупреждение; **gross**-поля всё равно
  считаются, если есть цена.
- **Текущая цена недоступна** → все PnL-поля (`new_fill_current_value`, gross/net,
  `distance_to_break_even_*`) = `null` + предупреждение; `break_even_price_after_commission`
  (не зависит от текущей цены) всё ещё считается из денежного оттока.
- **Доход/дивиденды** — `estimated_income_contribution_*`, `income_target_coverage_pct`
  наследуются из F4.4 (которая их заполняет только при надёжных данных); иначе
  `null` + предупреждение. Не угадываем.

## Token policy

- Опционально аналитический read-only `TINKOFF_TOKEN` — **только** для refresh
  текущей цены, и **только** если её нет в F4.4/F4.3. Значение не печатается.
- `TINKOFF_LIVE_TRADING_TOKEN` не требуется/не используется
  (`live_token_used=false`); `TINKOFF_SANDBOX_TOKEN` не используется.
- Отсутствие `TINKOFF_TOKEN` **не блокирует**, если отчётов F4.1/F4.2/F4.3/F4.4
  достаточно (gross/net считаются без сети).
- Если F4.4 отчёт отсутствует и данных недостаточно — команда падает **чисто**
  (exit `1`) **без сетевых вызовов**.
- account id в отчёте маскируется.

## CLI

```powershell
# Достаточно существующих отчётов F4.1–F4.4 — токен не обязателен.
python main.py income-live-fill-economics `
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
| `--f44-report` | `data/reports/income_live_fill_attribution_report.json` | F4.4 (основной источник, чтение) |
| `--output-json` | `data/reports/income_live_fill_economics_report.json` | JSON |
| `--output-md` | `data/reports/income_live_fill_economics_report.md` | Markdown |

## Отчёт

`data/reports/income_live_fill_economics_report.json` и `.md` (gitignored).
Ключевые поля: `stage` (`F4_5_LIVE_FILL_ECONOMICS_READ_ONLY`), `mode`
(`FILL_ECONOMICS_READ_ONLY`), идентификаторы заявки/инструмента, `fill_*` (новая
сделка), `current_total_*` (вся позиция, **отдельно**,
`total_position_pnl_kept_separate=true`), `new_fill_gross_unrealized_pnl(_pct)` и
`new_fill_net_unrealized_pnl_after_commission(_pct)`, `commission_drag_rub` /
`commission_drag_pct_of_gross_amount`, `break_even_price_after_commission`,
`distance_to_break_even_rub(_pct)`, `new_fill_weight_in_total_position_pct`,
`previous_position_estimated_*` + `previous_position_estimation_warning`, income-goal
поля + `income_estimation_warning`, `checked_at`, `guards`, `token_policy`,
`warnings`, `errors`.

`guards` фиксируют read-only контракт: `live_order_sent=false`,
`post_order_called=false`, `cancel_order_called=false`, `sell_order_sent=false`,
`market_order_used=false`, `retry_execution=false`, `portfolio_mutated=false`,
`config_mutated=false`, `telegram_sent=false`, `live_token_used=false`,
`sandbox_token_used=false`, `token_printed=false`.
