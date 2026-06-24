# income-live-order-status (F4.2 — read-only live order status monitor)

> 🔍 **READ-ONLY мониторинг.** Эта команда **только читает** статус уже созданной
> live-заявки через `GetOrderState`. Она **никогда** не создаёт, не отменяет, не
> повторяет и не продаёт заявки. Это этап наблюдения после F4.1, а не исполнение.

После того как F4.1 создала live-заявку (например `order_id=80578688754`,
`execution_report_status=EXECUTION_REPORT_STATUS_NEW`, `lots_executed=0`), нужно
безопасно узнать, стала ли она `FILL` / `PARTIALLYFILL` / `CANCELLED` / `REJECTED`.
F4.2 делает это read-only.

## Что F4.2 делает и чего НЕ делает

- ✅ читает текущее состояние заявки через read-only `GetOrderState`;
- ✅ классифицирует статус (terminal / filled / partially filled / rejected /
  cancelled) и `lots_requested` / `lots_executed`;
- ✅ опциональный watch-режим: периодически читает статус до терминального или
  timeout;
- ✅ пишет JSON/MD отчёт;
- ❌ **не** вызывает PostOrder (не создаёт заявок);
- ❌ **не** отменяет заявку;
- ❌ **не** ставит вторую заявку, **не** продаёт, **не** ретраит исполнение,
  **не** использует MARKET;
- ❌ **не** мутирует портфель/config, **не** шлёт Telegram-команд исполнения;
- ❌ **не** выполняет никаких действий на основе статуса (только наблюдение).

## Политика токена

- `TINKOFF_LIVE_TRADING_TOKEN` используется **только** для read-only чтения статуса
  (`GetOrderState`).
- Значение токена **никогда** не печатается и не пишется в отчёт/лог.
- Если токен отсутствует: команда блокируется (exit code `1`), **не** делает
  сетевых вызовов и сообщает `live_trading_token_present=false`.
- account id в отчёте **маскируется**.

## CLI

| Опция | По умолчанию | Назначение |
| --- | --- | --- |
| `--order-id` | — (обязательно) | id live-заявки для чтения статуса |
| `--live-account-id` | — (обязательно) | live account id заявки |
| `--watch` | выкл. | периодически читать статус (read-only) |
| `--interval-sec` | `10` | интервал опроса (только для `--watch`) |
| `--timeout-sec` | `300` | максимум опроса (только для `--watch`) |
| `--output-json` | `data/reports/income_live_order_status_report.json` | JSON-отчёт |
| `--output-md` | `data/reports/income_live_order_status_report.md` | Markdown-отчёт |

### Одноразовое чтение статуса

```powershell
$env:TINKOFF_LIVE_TRADING_TOKEN = "<ваш live trading token>"
python main.py income-live-order-status --order-id 80578688754 --live-account-id <ACCOUNT_ID>
```

### Watch до терминального статуса

```powershell
python main.py income-live-order-status `
  --order-id 80578688754 --live-account-id <ACCOUNT_ID> `
  --watch --interval-sec 10 --timeout-sec 300
```

Watch останавливается на терминальных статусах
(`EXECUTION_REPORT_STATUS_FILL` / `_CANCELLED` / `_REJECTED`) или по timeout. По
timeout статус остаётся нетерминальным, и **никаких действий не выполняется**.
Частичное исполнение (`lots_executed > 0`, но не полностью) отражается флагом
`is_partially_filled`.

## Отчёт

`data/reports/income_live_order_status_report.json` и `.md` (gitignored).

Ключевые поля JSON: `stage` (`F4_2_LIVE_ORDER_STATUS_READ_ONLY`), `mode`
(`STATUS_READ_ONLY` / `WATCH_READ_ONLY`), `order_id`, `live_account_id_masked`,
`execution_report_status`, `lots_requested`, `lots_executed`, `is_terminal`,
`is_filled`, `is_partially_filled`, `is_rejected`, `is_cancelled`,
`watch_timed_out`, `checked_at`, `checks_count`, `guards`, `token_policy`,
`warnings`, `errors`.

`guards` фиксируют read-only контракт: `live_order_sent=false`,
`post_order_called=false`, `cancel_order_called=false`, `sell_order_sent=false`,
`market_order_used=false`, `retry_execution=false`, `portfolio_mutated=false`,
`config_mutated=false`, `telegram_sent=false`, `token_printed=false`.

## Контракт чтения

Состояние читается через тот же проверенный live REST-адаптер
(`modules/tinvest_live_transport.py`, метод `get_live_state` →
`GetOrderState`), что и в F4.1, но здесь вызывается **только** read-only чтение
состояния. PostOrder/отмена из этого пути недостижимы.
