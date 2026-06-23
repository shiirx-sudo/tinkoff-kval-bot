# income-live-execute (F4.1 — tiny live manual-confirmed order)

> ⚠️ **РЕАЛЬНЫЕ ДЕНЬГИ.** Это первая и единственная команда проекта, которая может
> отправить **настоящую** биржевую заявку на реальные деньги. Она делает это
> **только** при явном `--send-live`, всех пройденных gate'ах, наличии live account
> id, отдельного live-токена и **точной** ручной фразы подтверждения. Без
> `--send-live` это безопасный dry-run, который ничего не отправляет.

F4.1 — это **real live execution capability**. Разрешена **ровно одна** крошечная
заявка:

- ticker `T`, side `BUY`, type `LIMIT`, **1 лот**, cap **300 RUB**, instrumentId
  **uid-first**.

## Что F4.1 делает и чего НЕ делает

- ✅ читает F4.0 readiness report и F2 preview report (только чтение);
- ✅ проверяет все gate'ы (readiness + preview + цена/cap + фраза + account + токен);
- ✅ при `--send-live` и пройденных gate'ах отправляет **одну** live BUY LIMIT
  заявку через проверенный live REST-адаптер;
- ✅ пишет post-trade отчёт json+md (с маскированным account id, без токена);
- ❌ **никаких** market-заявок (только LIMIT, без market fallback);
- ❌ **никаких** sell-заявок, ретраев, усреднения;
- ❌ **никакой** автоматизации, цикла, планировщика, Telegram-исполнения;
- ❌ **не** мутирует config, **не** отправляет sandbox-заявок, **не** ищет account;
- ❌ один запуск = **максимум одна** заявка, ровно **одна** сетевая попытка PostOrder.

## Обязательная фраза подтверждения

```
CONFIRM LIVE BUY T 1 LOT MAX 300 RUB
```

Если фраза `--confirm` отсутствует или не совпадает: live-заявка **не** отправляется,
`confirmation_matched=false`, exit code `1`, требуемая фраза печатается в отчёте
(`required_confirmation_phrase`).

## Политика токена (обязательно)

- Live-исполнение использует **ТОЛЬКО** отдельный env var
  `TINKOFF_LIVE_TRADING_TOKEN`.
- Аналитический read-only токен (`TINKOFF_TOKEN`) для исполнения **запрещён**.
- Sandbox-токен (`TINKOFF_SANDBOX_TOKEN`) для live **запрещён**.
- Значение токена **никогда** не печатается и не пишется в отчёт/ошибку/лог (только
  кладётся в `Authorization` header при отправке).
- Если `TINKOFF_LIVE_TRADING_TOKEN` отсутствует, а передан `--send-live`: блок, без
  заявки, exit code `1`, отчёт говорит `token_policy.live_trading_token_present=false`.

Live account id **не** определяется автоматически — его нужно передать вручную через
`--live-account-id` (обязателен для `--send-live`). В отчёте он **маскируется**.

## Gate'ы перед отправкой

**Readiness gate** (`income_live_readiness_report.json`, F4.0): `stage ==
F4_0_PRE_LIVE_READINESS`, `mode == READINESS_ONLY`, `sandbox_gate_passed == true`,
`ready_for_f4_live_manual_order == true`, `live_plan` (`ticker=T`, `side=BUY`,
`order_type=LIMIT`, `lots=1`, `max_order_rub=300`),
`required_future_confirmation_phrase` совпадает с фразой CLI, guards
`live_order_sent=false`, `live_orders_service_used=false`, `no_live_execution=true`,
`no_order_execution=true`.

**Preview gate** (`income_order_preview.json`, F2): F2 preview — источник
**eligibility**: тикер `T` выбран, `preview_status == PREVIEW_READY`,
`source_proposed_action == BUY_CANDIDATE`, безопасные F2-флаги
(`order_send_allowed=false`, `auto_execution_allowed=false`,
`full_access_token_required=false`, `orders_service_allowed=false`,
`manual_confirmation_required=true`), `reference_price_status == OK` и
`reference_price` > 0, корректный `lot_size`, и идентификаторы инструмента
(uid/figi). Размер заявки F4.1 здесь **не** проверяется по preview-итогу.

> F4.1 **не** требует `estimated_total_rub ≤ max_order_rub`. Preview
> `estimated_total_rub` отражает `preview_lots` (сайзинг под preview-cap старого
> запуска) и может отличаться от текущих `--lots`, поэтому он **только для
> прозрачности отчёта** и не является решающим cap-блокером. Решающий cap-чек —
> в Price/cap gate ниже, по `current_order_estimated_total_rub`.

**Price/cap gate (current-order notional):** лимитная цена = последняя preview
reference price; решающий cap-чек считает стоимость **именно текущей заявки**:
`current_order_estimated_total_rub = reference_price × lot_size × cli_lots` ≤
`max_order_rub` (300). Преимущество: F2 preview `estimated_total_rub` отражает
`preview_lots` (сайзинг под preview-cap старого запуска) и может ложно блокировать
меньший `--lots` — поэтому он **только для прозрачности отчёта** и не является
решающим блокером. Если `preview_lots ≠ cli_lots`, добавляется **предупреждение**
(не блок); заявка всё равно блокируется, если `current_order_estimated_total_rub >
max_order_rub`. Если цена отсутствует/не OK → блок, без market fallback.

Любое нарушение любого gate → live-заявка **не** отправляется, причина в
`blocking_reasons[]`, exit code `1`.

## CLI

| Опция | По умолчанию | Назначение |
| --- | --- | --- |
| `--ticker` | `T` | тикер |
| `--live-account-id` | — | live account id (обязателен для `--send-live`) |
| `--max-order-rub` | `300` | жёсткий cap размера заявки |
| `--lots` | `1` | число лотов |
| `--instrument-id-source` | `auto` | `auto` (uid-first, figi-fallback) / `uid` / `figi` |
| `--send-live` | выкл. | явный флаг реальной отправки (одна заявка) |
| `--confirm` | — | точная фраза подтверждения (обязательна для `--send-live`) |
| `--dry-run` | вкл. | dry-run по умолчанию; отправка только при `--send-live` |
| `--readiness-report` | `data/reports/income_live_readiness_report.json` | вход F4.0 (чтение) |
| `--preview-report` | `data/reports/income_order_preview.json` | вход F2 (чтение) |
| `--output-json` | `data/reports/income_live_execution_report.json` | JSON-отчёт |
| `--output-md` | `data/reports/income_live_execution_report.md` | Markdown-отчёт |

### Пример: dry-run (ничего не отправляет, токен не нужен)

```bash
python main.py income-live-execute --ticker T --max-order-rub 300 --lots 1 --dry-run
```

### Пример: реальная отправка (ТОЛЬКО вручную после одобрения)

```powershell
# 1) выставить ОТДЕЛЬНЫЙ live-токен в окружении (значение не печатается)
$env:TINKOFF_LIVE_TRADING_TOKEN = "<ваш live trading token>"

# 2) одна live BUY LIMIT заявка с точной фразой и live account id
python main.py income-live-execute `
  --ticker T --lots 1 --max-order-rub 300 `
  --live-account-id <ВАШ_LIVE_ACCOUNT_ID> `
  --send-live `
  --confirm "CONFIRM LIVE BUY T 1 LOT MAX 300 RUB"
```

> ⚠️ Это реальные деньги. Команда отправит настоящую заявку, если все gate'ы
> пройдены. Запускайте её **только** вручную, осознанно, по одной.

## Отчёт

`data/reports/income_live_execution_report.json` и `.md` (gitignored).

Ключевые поля JSON: `stage` (`F4_1_TINY_LIVE_MANUAL_CONFIRMED_ORDER`), `mode`
(`DRY_RUN`/`LIVE_SEND`), `ticker`, `readiness_gate_passed`, `preview_gate_passed`,
`confirmation_matched`, `live_order_sent`, `live_order_result`,
`live_order_response_sanitized`, `live_order_state_sanitized` (опционально),
`live_http_status`, `live_http_error_body`, `live_http_error_json`,
`live_order_request_sanitized`, `live_order_request_wire_sanitized`,
`live_account_id_masked`, `required_confirmation_phrase`, `live_plan`,
`blocking_reasons[]`, `warnings[]`, `token_policy`, `guards`.

Current-order notional cap-gate (прозрачность): `reference_price`, `lot_size`,
`cli_lots`, `preview_lots`, `preview_estimated_total_rub`,
`current_order_estimated_total_rub`, `current_order_cap_passed`,
`preview_lots_matches_cli_lots`, `preview_lots_mismatch_warning` (+ собранный блок
`current_order_notional_gate`).

`guards` фиксируют контракт: `live_order_sent` (true только если live PostOrder
принят), `sandbox_order_sent=false`, `live_orders_service_used` (true только если
live PostOrder вызван), `full_access_live_token_used` / `live_token_used` (true
только если использован `TINKOFF_LIVE_TRADING_TOKEN`),
`tinkoff_token_used_for_execution=false`, `sandbox_token_used_for_live=false`,
`token_printed=false`, `portfolio_mutated=false`, `config_mutated=false`,
`telegram_sent=false`, `market_order_used=false`, `auto_execution_allowed=false`,
`manual_confirmation_required=true`, `no_retries=true`, `one_order_max=true`.

## Verified live contract (не догадка)

Live использует тот же `PostOrderRequest` из официального `orders.proto`, что
переиспользует sandbox (`PostSandboxOrder`): `quantity` (= лоты, int64 строкой),
`price` (Quotation), `direction` (`ORDER_DIRECTION_BUY`), `accountId`, `orderType`
(`ORDER_TYPE_LIMIT`), `orderId` (UUID v4), `instrumentId` (uid-first). Отличаются
только имя live-сервиса, live-токен и live account id. Реализация — в
`modules/tinvest_live_transport.py` (одна попытка, без ретраев).

## Безопасность тестов/CI

В тестах и валидации **никакая** реальная live-заявка не отправляется: live-адаптер
получает инъецируемый fake-транспорт, реальная сеть не вызывается. Реальная
отправка возможна **только** вручную после merge с явной командой и точной фразой.

## Будущее расширение

Любое расширение (больше size, другие тикеры, автоматизация, UI-кнопка, продажи,
ретраи) — только в **отдельном PR** с отдельным одобрением.
