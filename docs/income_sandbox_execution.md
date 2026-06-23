# income-sandbox-execute-preview (F3 — sandbox manual-confirmed execution)

Read-only-by-default команда этапа **F3** контролируемого, ручного-подтверждаемого
исполнения. Берёт **одного** кандидата со статусом `PREVIEW_READY` из отчёта F2
(`income-order-preview`) и может выполнить заявку **только в sandbox**, **только по
одному тикеру**, **только после точной фразы ручного подтверждения**.

## Назначение

F3 существует, чтобы проверить жизненный цикл заявки и отчётность в безопасной
среде до любых реальных сделок. Это **не** торговый сигнал и **не** инвестиционная
рекомендация — это контролируемая sandbox-проверка процесса
research → owner decision (F1) → order preview / no-send (F2) → **sandbox manual
confirm (F3)** → tiny live (F4, отдельный PR).

## Почему только sandbox

- LIVE-заявки на этапе F3 **запрещены**.
- Не используется live order-endpoint, live `Orders`-сервис, full-access live токен
  и live account.
- Нет autonomous execution, нет market-заявок (только LIMIT), нет Telegram-исполнения.
- Не мутируется портфель и не мутируется config; запись только в `data/reports/`.

Реальная live-сделка появляется только на этапе **F4** и только в отдельном PR с
отдельным одобрением владельца.

## Требуемые входы

- Файл F2 `data/reports/income_order_preview.json` (создаётся `income-order-preview`).
- Выбранная строка обязана пройти жёсткую валидацию:
  - `preview_status == PREVIEW_READY`;
  - `source_proposed_action == BUY_CANDIDATE`;
  - `manual_confirmation_required == true`;
  - `order_send_allowed == false`;
  - `auto_execution_allowed == false`;
  - `full_access_token_required == false`;
  - `orders_service_allowed == false`;
  - `preview_lots` — целое > 0;
  - `estimated_total_rub <= --max-order-rub`;
  - `reference_price_status == OK` для реальной sandbox-отправки.

Любое нарушение → команда останавливается с понятной ошибкой, заявка не отправляется.

## CLI

```bash
# dry-run (по умолчанию): отчёт + required confirmation phrase, заявка НЕ отправляется
python main.py income-sandbox-execute-preview --ticker T --max-order-rub 1000 --dry-run

# попытка реальной sandbox-отправки (только при всех пройденных gate'ах и точной фразе)
# обязательно: проверенный транспорт verified-rest + sandbox account id + точная фраза
python main.py income-sandbox-execute-preview \
  --ticker T \
  --sandbox-transport verified-rest \
  --sandbox-account-id <SANDBOX_ACCOUNT_ID> \
  --send-sandbox \
  --confirm "CONFIRM SANDBOX BUY T 3 LOTS MAX 1000 RUB"
```

Реальная sandbox-отправка дополнительно требует переменную окружения
`TINKOFF_SANDBOX_TOKEN` (sandbox-токен, не live). Без `--sandbox-transport
verified-rest` отправка блокируется (`SANDBOX_TRANSPORT_UNCONFIGURED`).

Опции:

| Опция | По умолчанию | Назначение |
| --- | --- | --- |
| `--ticker` | — (обязателен) | ровно один тикер из F2 preview |
| `--preview-json` | `data/reports/income_order_preview.json` | вход F2 (только чтение) |
| `--output-json` | `data/reports/income_sandbox_execution_report.json` | JSON-отчёт |
| `--output-md` | `data/reports/income_sandbox_execution_report.md` | Markdown-отчёт |
| `--sandbox-account-id` | none | sandbox account id (обязателен для `--send-sandbox`) |
| `--sandbox-transport` | `unconfigured` | `unconfigured` / `verified-rest` / `verified-sdk` |
| `--instrument-id-source` | `auto` | `auto` (uid-first, figi-fallback) / `uid` / `figi` |
| `--max-order-rub` | `1000` | жёсткий cap размера заявки |
| `--max-price-deviation-bps` | `100` | макс. отклонение свежей цены от preview |
| `--dry-run` | true | dry-run по умолчанию |
| `--send-sandbox` | выкл. | явный флаг попытки sandbox-отправки |
| `--confirm` | none | точная фраза подтверждения |
| `--price-mode` | `auto` | `auto` / `offline` / `readonly-api` |
| `--client-order-id-prefix` | `sandbox-f3` | префикс client order id |

## Фраза подтверждения

Команда генерирует и печатает обязательную фразу:

```
CONFIRM SANDBOX BUY <TICKER> <LOTS> LOTS MAX <MAX_ORDER_RUB> RUB
```

Примеры:

```
CONFIRM SANDBOX BUY T 3 LOTS MAX 1000 RUB
CONFIRM SANDBOX BUY VTBR 14 LOTS MAX 1000 RUB
```

Если задан `--send-sandbox`, но `--confirm` отсутствует или не совпадает точно:
заявка не отправляется, код возврата `1`, отчёт показывает
`required_confirmation_phrase`, `sandbox_order_sent=false`.

## Dry-run vs send-sandbox

- **dry-run** (по умолчанию): без `--send-sandbox` sandbox-заявка не отправляется.
  Команда строит отчёт, preflight и фразу подтверждения. `mode=DRY_RUN`.
- **send-sandbox**: с `--send-sandbox` заявка может уйти в sandbox **только если**
  пройдены все gate'ы (точная фраза, sandbox account id, sandbox-токен, доступная
  цена, отклонение цены в пределах лимита). `mode=SANDBOX_SEND`. Один запуск = один
  тикер = максимум одна sandbox-заявка. Только BUY, только LIMIT.

## Изоляция токена

- Sandbox-токен читается **только** из отдельной переменной окружения
  `TINKOFF_SANDBOX_TOKEN`.
- Live read/full-access токен (`TINKOFF_READ_TOKEN` и т.п.) для исполнения **не**
  используется.
- При dry-run sandbox-токен **не обязателен**.
- Токен **никогда** не печатается и не попадает в отчёт; account id в отчёте только
  маскированный.

## Sandbox-транспорт (F3.1 verified transport) и no live Orders-сервис

Реальная sandbox-отправка проходит через интерфейс `SandboxOrderAdapter` (только
sandbox, только BUY/LIMIT). Выбор транспорта — флаг `--sandbox-transport`:

| Значение | Поведение |
| --- | --- |
| `unconfigured` (по умолчанию) | транспорт не выбран; реальная отправка блокируется (`SANDBOX_TRANSPORT_UNCONFIGURED`); dry-run и preflight работают полностью |
| `verified-rest` | проверенный sandbox REST-адаптер `VerifiedSandboxRestAdapter` (`modules/tinvest_sandbox_transport.py`) |
| `verified-sdk` | зарезервировано под официальный SDK; SDK в окружении нет → отправка блокируется (`SANDBOX_SDK_NOT_AVAILABLE`) |

### Источник проверенного контракта

`verified-rest` использует **не догадки**, а подтверждённый официальный контракт:

- транспортное соглашение — тот же gRPC-over-REST pattern, что у read-only
  `brokers/tinkoff/rest_client.py` (base URL `https://invest-public-api.tinkoff.ru/rest`,
  путь `/{service}/{method}`, Bearer-токен, JSON в camelCase,
  `Quotation = {units, nano}`);
- сервис/методы/поля — официальные proto-файлы RussianInvestments/investAPI:
  - `src/docs/contracts/sandbox.proto` → пакет
    `tinkoff.public.invest.api.contract.v1`, сервис `SandboxService`, методы
    `PostSandboxOrder(PostOrderRequest) → PostOrderResponse` и
    `GetSandboxOrderState(GetOrderStateRequest) → OrderState`;
  - `src/docs/contracts/orders.proto` → `PostOrderRequest`
    (`quantity` = **количество лотов**, `price` = `Quotation`, `direction`,
    `account_id`, `order_type`, `order_id`, `instrument_id`), `PostOrderResponse`
    (`order_id`, `execution_report_status`, `lots_requested`, `lots_executed`,
    `total_order_amount`, `message`), enum `OrderDirection`/`OrderType`,
    `GetOrderStateRequest` (`account_id`, `order_id`).

Wire-payload (camelCase JSON): `quantity` (строка, лоты), `price`, `direction`
(`ORDER_DIRECTION_BUY`), `accountId`, `orderType` (`ORDER_TYPE_LIMIT`), `orderId`
(идемпотентный client order id), `instrumentId` (uid/figi). Контракт фиксируется в
отчёте в `sandbox_transport.contract_source`.

#### Выбор instrumentId: UID-first

Поле wire-payload называется `instrumentId`. По умолчанию (`auto`) адаптер берёт
**uid first, figi fallback** — uid присутствует в F2 preview и для `PostSandboxOrder`
надёжнее. Поведение управляется флагом `--instrument-id-source auto|uid|figi`:

| Значение | Поведение |
| --- | --- |
| `auto` (по умолчанию) | uid, если есть; иначе figi; если нет обоих — hard fail |
| `uid` | только uid (если uid нет — hard fail) |
| `figi` | только figi (если figi нет — hard fail) |

Фактический использованный источник фиксируется в отчёте как
`sandbox_order_request_wire_sanitized.instrument_id_source`.

Адаптер принимает **уже подготовленные безопасные параметры** из F3 preflight и сам
НЕ выбирает инструмент/цену/лоты, НЕ читает live account, НЕ использует live токен,
НЕ вызывает live order-endpoint. MARKET-заявка или не-BUY → hard fail до сети.

Live order-endpoint, live `Orders`-сервис и full-access live токен здесь не
реализованы и не вызываются.

### Manual one-shot sandbox test

Реальная sandbox-отправка делается **только вручную, отдельной командой, после
PR**, и никогда в автоматической валидации/CI/smoke (там адаптер/HTTP мокаются):

```powershell
$env:TINKOFF_SANDBOX_TOKEN="<sandbox-token>"   # отдельный sandbox-токен, не live
python main.py income-sandbox-execute-preview `
  --ticker T `
  --sandbox-transport verified-rest `
  --sandbox-account-id <SANDBOX_ACCOUNT_ID> `
  --send-sandbox `
  --confirm "CONFIRM SANDBOX BUY T 3 LOTS MAX 1000 RUB"
```

Один запуск = один тикер = максимум одна sandbox-заявка.

## Preflight gates

Перед реальной sandbox-отправкой:

- перепроверяется свежая read-only reference price (если доступна);
- сравнение со значением из preview; при отклонении > `--max-price-deviation-bps`
  отправка блокируется;
- проверяется наличие sandbox account id;
- проверяется наличие `TINKOFF_SANDBOX_TOKEN` (без печати токена);
- формируется идемпотентный `client_order_id` (prefix + тикер + timestamp + hash);
- проверки: `preview_ready`, `confirmation_matched`, `sandbox_account_present`,
  `sandbox_token_present`, `price_available`, `price_deviation_ok`, `cap_ok`,
  `no_live_execution`, `no_market_order`.

## Отчёт

`data/reports/income_sandbox_execution_report.json` и `.md` (gitignored).

Ключевые поля JSON: `generated_at`, `stage` (`F3_SANDBOX_MANUAL_CONFIRMED_EXECUTION`),
`mode` (`DRY_RUN`/`SANDBOX_SEND`), `ticker`, `preview_source`, `selected_preview`,
`required_confirmation_phrase`, `confirmation_matched`, `preflight`,
`sandbox_transport` (`selected_transport`, `configured`, `contract_source`,
`adapter_class`), `sandbox_order_request` / `sandbox_order_request_sanitized`,
`sandbox_order_request_wire_sanitized`, `sandbox_order_result`,
`sandbox_order_response_sanitized`, `sandbox_order_state_sanitized`,
`sandbox_http_status`, `sandbox_http_error_body`, `sandbox_http_error_json`,
`sandbox_error_method`, `diagnostic_hint`, `guards`, `errors`, `warnings`.

Все ответы адаптера санитизируются: в отчёт попадают только whitelisted-поля
контракта (order id, статус, лоты, сумма, message) — токен/секреты в отчёт и логи
не попадают; account id только маскированный.

### Диагностика отправки (actual wire payload + HTTP-ошибки)

Дополнительные поля отчёта для разбора неудачной sandbox-отправки (все
санитизированы, без токена и без Authorization-заголовка):

- `sandbox_order_request_wire_sanitized` — **фактический** wire-payload, ушедший в
  REST: `instrumentId`, `instrument_id_source` (uid/figi), `quantity` + `quantity_type`
  (должна быть строкой int64), `price`, `direction`, `orderType`, `orderId`,
  `accountId_masked` (account id только маскированный);
- `sandbox_http_status` — HTTP-статус ответа (например `400`);
- `sandbox_http_error_body` — тело ответа API (обрезается), без секретов;
- `sandbox_http_error_json` — распарсенное JSON-тело ошибки, если ответ был JSON;
- `sandbox_error_method` — метод (`PostSandboxOrder`);
- `diagnostic_hint` — короткая подсказка по причине.

### Troubleshooting: 400 от PostSandboxOrder

Если `PostSandboxOrder` вернул `400 Client Error`, отчёт сохраняет причину без
токена. Порядок разбора:

1. Откройте `sandbox_http_error_body` (и `sandbox_http_error_json`) — там тело
   ответа API с `message`/`description` причины.
2. Проверьте `sandbox_order_request_wire_sanitized`:
   - `instrument_id_source` — какой id ушёл (uid/figi). По умолчанию uid-first;
     при проблемах попробуйте `--instrument-id-source figi`.
   - `quantity` — должно быть **строкой** и равно числу **лотов** (не штук).
   - `price` — `Quotation {units, nano}`; проверьте шаг цены (price increment)
     инструмента; неверное приращение цены — частая причина 400.
   - `direction` / `orderType` — должны быть `ORDER_DIRECTION_BUY` /
     `ORDER_TYPE_LIMIT` (enum-значения).
3. `diagnostic_hint` суммирует вероятную причину.

Токен (`TINKOFF_SANDBOX_TOKEN`) и Authorization-заголовок никогда не попадают в
отчёт, тело ошибки и логи.

`guards` фиксируют контракт: `live_order_sent=false`, `sandbox_order_sent`,
`dry_run`, `manual_confirmation_required=true`, `order_send_allowed=false` (для live),
`auto_execution_allowed=false`, `live_orders_service_used=false`,
`sandbox_service_used`, `full_access_live_token_used=false`, `live_token_used=false`,
`token_printed=false`, `sandbox_token_used`, `portfolio_mutated=false`,
`config_mutated=false`, `telegram_sent=false`, `next_stage="F4 ..."`.

## Путь к F4 (отдельный PR)

F3/F3.1 доказывают жизненный цикл sandbox-заявки и отчётность. Реальная (tiny live)
сделка — это этап **F4**, который остаётся **отдельным PR с отдельным одобрением
владельца** и допускается только после того, как: (1) есть F2 preview; (2) проходит
F3 dry-run; (3) есть F3.1 verified sandbox transport (этот PR); (4) хотя бы одна
реальная sandbox-заявка прогнана **вручную** и зафиксирована в отчёте; (5) одобрен
отдельный F4 PR. F4 добавляет: limit-заявки, allowlist инструментов, cap суммы,
отдельный execution-токен (никогда не печатается), kill switch и полный post-trade
отчёт. Live остаётся запрещённым до F4: здесь нет live order-endpoint, live
`Orders`-сервиса и full-access live токена.
