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
python main.py income-sandbox-execute-preview \
  --ticker T \
  --sandbox-account-id <SANDBOX_ACCOUNT_ID> \
  --send-sandbox \
  --confirm "CONFIRM SANDBOX BUY T 3 LOTS MAX 1000 RUB"
```

Опции:

| Опция | По умолчанию | Назначение |
| --- | --- | --- |
| `--ticker` | — (обязателен) | ровно один тикер из F2 preview |
| `--preview-json` | `data/reports/income_order_preview.json` | вход F2 (только чтение) |
| `--output-json` | `data/reports/income_sandbox_execution_report.json` | JSON-отчёт |
| `--output-md` | `data/reports/income_sandbox_execution_report.md` | Markdown-отчёт |
| `--sandbox-account-id` | none | sandbox account id (обязателен для `--send-sandbox`) |
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

## Sandbox-транспорт (adapter seam) и no live Orders-сервис

В проекте нет SDK и нет верифицированного REST sandbox-клиента. Реальная
sandbox-отправка проходит через интерфейс `SandboxOrderAdapter` (только sandbox,
только BUY/LIMIT). По умолчанию используется `UnconfiguredSandboxAdapter`, который
честно сообщает, что транспорт не подключён и нужен **отдельный проверенный
sandbox-wrapper PR (этап F3.1)** — официальный SDK sandbox namespace либо
протестированный sandbox REST адаптер. dry-run полностью работает без адаптера и без
токена; тесты подменяют адаптер моком и проверяют весь путь отправки.

Live order-endpoint, live `Orders`-сервис и full-access live токен здесь не
реализованы и не вызываются.

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
`sandbox_order_request`, `sandbox_order_result`, `guards`, `errors`, `warnings`.

`guards` фиксируют контракт: `live_order_sent=false`, `sandbox_order_sent`,
`dry_run`, `manual_confirmation_required=true`, `order_send_allowed=false` (для live),
`auto_execution_allowed=false`, `live_orders_service_used=false`,
`sandbox_service_used`, `full_access_live_token_used=false`, `sandbox_token_used`,
`portfolio_mutated=false`, `config_mutated=false`, `telegram_sent=false`,
`next_stage="F4 ..."`.

## Путь к F4

F3 доказывает жизненный цикл sandbox-заявки и отчётность. Реальная (tiny live)
сделка — это этап **F4**, который остаётся **отдельным PR с отдельным одобрением
владельца**: limit-заявки, allowlist инструментов, cap суммы, отдельный
execution-токен (никогда не печатается), kill switch и полный post-trade отчёт.
