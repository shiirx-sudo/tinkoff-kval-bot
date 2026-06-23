# income-live-readiness (F4.0 — pre-live readiness)

Read-only команда этапа **F4.0**. Она **не** исполняет сделок: проверяет, что
предыдущий sandbox-этап (F3) реально пройден, готовит **tiny live plan** и
фиксирует будущую точную фразу подтверждения. Live-заявка остаётся заблокированной
до отдельного PR этапа **F4.1**.

## Что F4.0 делает и чего НЕ делает

F4.0 — это **readiness/reporting**, а не исполнение:

- ✅ читает последний F3 sandbox execution report;
- ✅ проверяет sandbox FILL-gate;
- ✅ строит фиксированный tiny live plan (T, BUY, LIMIT, 1 лот, cap 300 ₽);
- ✅ формирует будущую точную фразу подтверждения;
- ✅ сообщает, присутствует ли будущий live-токен (только наличие, без печати);
- ❌ **не** отправляет live-заявку;
- ❌ **не** отправляет sandbox-заявку;
- ❌ **не** вызывает live `Orders`-сервис / order-endpoint;
- ❌ **не** использует execution-токен;
- ❌ **не** мутирует портфель/config, **не** шлёт Telegram.

## Проверка sandbox-gate (вход F3)

Команда читает `data/reports/income_sandbox_execution_report.json` и считает gate
пройденным только при всех условиях:

- `stage == F3_SANDBOX_MANUAL_CONFIRMED_EXECUTION`;
- `mode == SANDBOX_SEND` (реальная sandbox-отправка, не dry-run);
- `sandbox_order_result.sandbox_order_sent == true`;
- `sandbox_order_result.execution_report_status == EXECUTION_REPORT_STATUS_FILL`;
- `guards.live_order_sent == false`;
- `guards.live_orders_service_used == false`;
- `guards.full_access_live_token_used == false`;
- `guards.token_printed == false`.

Любое нарушение → `ready_for_f4_live_manual_order=false` с причиной в
`blocking_reasons[]` и код возврата `1`.

- Если sandbox-отчёт **отсутствует** → `ready=false`, blocking reason «missing
  sandbox execution report», exit code `1`.
- Если заявка **не FILL** → `ready=false` с причиной.
- Если gate пройден → `ready=true`, exit code `0`.

## CLI

```bash
# по умолчанию: T, 1 лот, cap 300 ₽, вход из стандартного F3-отчёта
python main.py income-live-readiness
```

Опции:

| Опция | По умолчанию | Назначение |
| --- | --- | --- |
| `--ticker` | `T` | тикер tiny live plan |
| `--lots` | `1` | число лотов tiny live plan |
| `--max-order-rub` | `300` | жёсткий cap размера будущей live-заявки |
| `--sandbox-report` | `data/reports/income_sandbox_execution_report.json` | вход F3 (только чтение) |
| `--output-json` | `data/reports/income_live_readiness_report.json` | JSON-отчёт |
| `--output-md` | `data/reports/income_live_readiness_report.md` | Markdown-отчёт |

## Tiny live plan (подготовлен, НЕ исполняется)

| Поле | Значение |
| --- | --- |
| ticker | `T` |
| side | `BUY` |
| order_type | `LIMIT` (никаких market-заявок) |
| lots | `1` |
| max_order_rub | `300` |
| instrument_id_source | `uid-first` |

## Будущая фраза подтверждения

```
CONFIRM LIVE BUY T 1 LOT MAX 300 RUB
```

Она будет нужна на этапе **F4.1**, но **не** на F4.0: readiness ничего не
исполняет.

## Политика токена (будущий live-токен)

- Будущая реальная live-отправка (F4.1) обязана использовать **отдельный** env var
  `TINKOFF_LIVE_TRADING_TOKEN`.
- `TINKOFF_TOKEN` остаётся **read-only / analytics**-токеном и **не** используется
  для исполнения.
- Sandbox-токен (`TINKOFF_SANDBOX_TOKEN`) **не** используется для live.
- F4.0 readiness только сообщает, **присутствует** ли `TINKOFF_LIVE_TRADING_TOKEN`
  (поле `token_policy.live_trading_token_present`), но **никогда** не печатает и не
  пишет его значение.

## Отчёт

`data/reports/income_live_readiness_report.json` и `.md` (gitignored).

Ключевые поля JSON: `stage` (`F4_0_PRE_LIVE_READINESS`), `mode` (`READINESS_ONLY`),
`ticker`, `sandbox_gate_passed`, `sandbox_report_path`, `sandbox_order_id`,
`sandbox_execution_report_status`, `ready_for_f4_live_manual_order`,
`blocking_reasons[]`, `warnings[]`, `live_plan` (`ticker`, `side`, `order_type`,
`lots`, `max_order_rub`, `instrument_id_source`,
`required_future_confirmation_phrase`), `required_future_confirmation_phrase`,
`token_policy` (`live_trading_token_env`, `live_trading_token_present`,
`tinkoff_token_used_for_execution=false`, `sandbox_token_used_for_live=false`,
`token_printed=false`), `guards`, `next_stage`.

`guards` фиксируют контракт: `live_order_sent=false`, `sandbox_order_sent=false`,
`live_orders_service_used=false`, `full_access_live_token_used=false`,
`live_token_used=false`, `sandbox_token_used=false`, `token_printed=false`,
`portfolio_mutated=false`, `config_mutated=false`, `telegram_sent=false`,
`no_live_execution=true`, `no_order_execution=true`.

## Путь к F4.1 (отдельный PR)

Live-исполнение остаётся **заблокированным** до отдельного PR этапа **F4.1**, и
допускается только когда:

1. F4.0 readiness-отчёт говорит `ready=true`;
2. пользователь явно одобряет отдельный F4.1 PR;
3. вручную предоставлены live trading token (`TINKOFF_LIVE_TRADING_TOKEN`) и live
   account id;
4. использована точная фраза `CONFIRM LIVE BUY T 1 LOT MAX 300 RUB`;
5. никаких market-заявок, ровно 1 лот, cap 300 ₽, отдельная ручная команда.
