# income-live-position-report (F4.3 — read-only live position reconciliation)

> 🔍 **READ-ONLY сверка.** Эта команда только **сверяет** завершённую F4.1/F4.2
> live-заявку с реальной позицией в портфеле. Она **ничего** не исполняет, не
> отменяет, не продаёт и не повторяет. Это наблюдение после F4.2, не торговля.

После того как первая tiny live-заявка исполнилась (F4.2 подтвердил
`EXECUTION_REPORT_STATUS_FILL`, `lots_executed=1`), F4.3 проверяет, что реальная
позиция действительно появилась и согласована с заявкой.

## Что F4.3 делает и чего НЕ делает

- ✅ читает (опционально) отчёты F4.1 `income_live_execution_report.json` и F4.2
  `income_live_order_status_report.json`;
- ✅ читает текущий live-портфель/позиции через **read-only** эндпоинты T-Invest;
- ✅ сверяет: order_id совпадает, статус `FILL`, `lots_executed=1`, есть позиция по
  ticker/figi/uid, количество согласовано с 1 лотом (где известен размер лота);
- ✅ считает рыночную стоимость и нереализованный P/L позиции из read-only цен;
- ✅ соотносит с целевой месячной корзиной `150000 RUB` — **только** при надёжных
  данных о доходе; иначе ставит `null` и предупреждает (не угадывает);
- ❌ **не** вызывает PostOrder, **не** отменяет, **не** ставит вторую заявку,
  **не** продаёт, **не** ретраит, **не** использует MARKET;
- ❌ **не** мутирует портфель/config, **не** шлёт Telegram;
- ❌ **не** требует и **не** использует `TINKOFF_LIVE_TRADING_TOKEN`;
- ❌ **не** использует `TINKOFF_SANDBOX_TOKEN`.

## Политика токена

- Используется **только** аналитический read-only `TINKOFF_TOKEN`
  (portfolio/positions/market-data). Значение токена не печатается.
- `TINKOFF_LIVE_TRADING_TOKEN` **не требуется** и **не используется**
  (`live_token_used=false`).
- `TINKOFF_SANDBOX_TOKEN` **не используется** (`sandbox_token_used=false`).
- Если read-only токена нет: команда падает **чисто** (exit `1`) **без сетевых
  вызовов** (клиент не создаётся, портфель не читается).
- account id в отчёте **маскируется**.

## CLI

```powershell
$env:TINKOFF_TOKEN = "<read-only analytics token>"   # или TINKOFF_READ_TOKEN
python main.py income-live-position-report `
  --ticker T --order-id 80578688754 --live-account-id <ACCOUNT_ID>
```

| Опция | По умолчанию | Назначение |
| --- | --- | --- |
| `--ticker` | `T` | тикер для сверки |
| `--order-id` | — (обязательно) | id завершённой live-заявки |
| `--live-account-id` | — (обязательно) | live account id |
| `--f41-report` | `data/reports/income_live_execution_report.json` | F4.1 (чтение) |
| `--f42-report` | `data/reports/income_live_order_status_report.json` | F4.2 (чтение) |
| `--output-json` | `data/reports/income_live_position_report.json` | JSON-отчёт |
| `--output-md` | `data/reports/income_live_position_report.md` | Markdown-отчёт |

## Сверка (reconciliation)

`reconciliation_passed=true` только если выполнены все жёсткие условия:

- `order_status == EXECUTION_REPORT_STATUS_FILL`;
- `lots_executed == 1`;
- order_id из F4.1/F4.2 совпадает с `--order-id`;
- найдена live-позиция по figi/uid;
- количество в позиции ≥ 1 лот (где известен размер лота).

Мягкие наблюдения (warning, не блокируют): позиция больше 1 лота (вероятны прежние
позиции), неизвестен размер лота, отсутствие отчётов F4.1/F4.2. Отсутствие позиции
или несоответствие статуса/лотов — провал сверки.

## Income goal (без угадывания)

- `base_monthly_living_basket_rub = 150000`;
- `estimated_income_contribution_rub_monthly` / `_yearly` и
  `income_target_coverage_pct` заполняются **только** при наличии надёжных данных о
  дивидендах/доходе. Если таких данных нет — поля `null` и добавляется явное
  предупреждение. Команда **никогда** не выдумывает доход.

## Отчёт

`data/reports/income_live_position_report.json` и `.md` (gitignored). Ключевые
поля: `stage` (`F4_3_LIVE_POSITION_RECONCILIATION_READ_ONLY`), `mode`
(`POSITION_READ_ONLY`), `ticker`, `order_id`, `live_account_id_masked`,
`order_status`, `lots_requested`, `lots_executed`, `instrument_uid`, `figi`,
`class_code`, `lot_size`, `position_found`, `position_quantity_lots`,
`position_quantity_units`, `average_position_price`, `current_price`,
`current_position_value`, `unrealized_pnl`, `currency`, `reconciliation_passed`,
`reconciliation_warnings`, income-goal поля, `checked_at`, `guards`,
`token_policy`.

`guards` фиксируют read-only контракт: `live_order_sent=false`,
`post_order_called=false`, `cancel_order_called=false`, `sell_order_sent=false`,
`market_order_used=false`, `retry_execution=false`, `portfolio_mutated=false`,
`config_mutated=false`, `telegram_sent=false`, `live_token_used=false`,
`sandbox_token_used=false`, `token_printed=false`.
