# income-order-preview — F2 order preview / no-send (read-only)

`income-order-preview` строит **предварительный** расчёт заявки (order preview)
для owner-кандидатов `BUY_CANDIDATE` из F1 owner decision report. Это этап
**F2** дорожной карты controlled execution. Заявки **не отправляются**.

## Purpose

Дать владельцу прозрачный предпросмотр того, как выглядела бы заявка по
кандидату (лоты, количество бумаг, reference price, оценка суммы, комиссия/НКД,
если данные безопасно доступны, cash impact, risk flags), **без** какого-либо
исполнения. Это не приказ на сделку и не публичная инвестиционная рекомендация.

Жёсткий контракт F2 (для каждого preview и в guards):

- заявки не отправляются;
- orders-service не вызывается; `postOrder`/`cancelOrder` не вызываются;
- full-access токен не используется (только read-only методы);
- портфель и config не мутируются; запись только в `data/reports/`;
- нет live/sandbox исполнения, нет autonomous trading, нет market order;
- `order_send_allowed=false`, `auto_execution_allowed=false`,
  `full_access_token_required=false`, `orders_service_allowed=false`,
  `manual_confirmation_required=true`.

## Inputs

- F1 decision report: `data/reports/income_owner_decision_report.json`
  (создаётся `python main.py income-owner-decision-report`).
- Идентификаторы инструмента берутся из самих кандидатов
  (`ticker`, `figi`, `uid`, `isin`, `class_code`, `name`, `lot_size` если есть).
- Опционально read-only T-Invest данные (lot/last price) через
  `ReadOnlyClient.find_instrument` и `ReadOnlyClient.get_last_price` — только если
  `--price-mode` это разрешает и токен доступен. OrdersService не используется.

Если F1-кандидат имеет `order_send_allowed != false` или
`auto_execution_allowed != false`, команда считает F1-источник небезопасным и
завершается с ошибкой (hard fail), ничего не отправляя.

## CLI

```bash
python main.py income-order-preview \
  --decision-json data/reports/income_owner_decision_report.json \
  --output-json   data/reports/income_order_preview.json \
  --output-md     data/reports/income_order_preview.md \
  --candidate-action BUY_CANDIDATE \
  --ticker T --ticker VTBR \
  --max-candidates 5 \
  --max-order-rub 1000 \
  --min-lots 1 \
  [--max-lots N] \
  --price-mode auto      # auto | offline | readonly-api
  # --offline            # ярлык для --price-mode offline
```

- `--ticker` повторяемый: фильтр по тикерам.
- `--candidate-action` по умолчанию `BUY_CANDIDATE`.
- `--offline` — синоним `--price-mode offline`.

## Outputs

- `data/reports/income_order_preview.json`
- `data/reports/income_order_preview.md`

(оба под `data/reports/`, gitignored).

JSON верхнего уровня: `generated_at`, `mode`, `source_decision_report`,
`filters`, `summary`, `previews`, `guards`.

`summary`: `total_decision_candidates`, `selected_candidates`,
`preview_ready_count`, `needs_price_count`, `blocked_count`,
`order_send_allowed_count=0`, `auto_execution_allowed_count=0`,
`full_access_token_used=false`, `orders_service_used=false`.

`guards`: `stage="F2_ORDER_PREVIEW_NO_SEND"`, `order_send_allowed=false`,
`auto_execution_allowed=false`, `full_access_token_used=false`,
`orders_service_used=false`, `portfolio_mutated=false`, `config_mutated=false`,
`execution_requires_manual_confirmation=true`,
`next_stage="F3 sandbox manual-confirmed execution"`.

Каждый `preview`: идентификаторы, `source_proposed_action`, `source_score`,
`owner_review_eligible`, `lot_size`, `min_lots`, `preview_lots`,
`preview_quantity`, `max_order_rub`, `reference_price` (+ `_source`/`_time`/
`_status`), `estimated_notional_rub`, `estimated_commission_rub` (+ `_status`/
`_source`), `estimated_nkd_rub` (+ `_status`), `estimated_total_rub`,
`cash_check_status`, `risk_flags[]`, `preview_status`, `preview_blockers[]`,
`next_required_step` и жёсткие guard-флаги (см. контракт выше).

## Price source modes

`reference_price` берётся по приоритету (цена **никогда не выдумывается**):

1. свежая read-only API last price (если `--price-mode` ≠ `offline` и токен есть);
2. локальная цена из кандидата, если присутствует и не устарела;
3. цены нет → `reference_price_status` = `NEEDS_PRICE`
   (или `PRICE_UNAVAILABLE`, если API пробовали и не получили цену).

`reference_price_status`: `OK` / `NEEDS_PRICE` / `STALE_PRICE` /
`PRICE_UNAVAILABLE`. Устаревшая цена (`STALE_PRICE`) даёт risk flag, но не
блокирует preview.

Режимы:

- `auto` (по умолчанию): пробует read-only API, при отсутствии токена/ошибке
  безопасно деградирует в offline (`NEEDS_PRICE`).
- `offline`: только локальный decision report, без сети.
- `readonly-api`: использует read-only API last price/lot.

При отсутствии токена или недоступности market data команда **не падает** и
**ничего не отправляет** — соответствующие preview помечаются `NEEDS_PRICE` /
`PRICE_UNAVAILABLE`.

## Lot / amount calculation

- `lot_size`: из кандидата → из read-only instrument data → иначе
  `preview_status=BLOCKED`, blocker `LOT_SIZE_UNAVAILABLE`.
- `preview_lots`: целое число лотов, не меньше `--min-lots`, заполняет preview cap
  `--max-order-rub`, опционально ограничено `--max-lots`. Дробные лоты не
  предлагаются.
- Если даже `--min-lots` лот(ов) превышает `--max-order-rub` →
  `preview_status=BLOCKED`, blocker `MIN_LOT_EXCEEDS_CAP`.
- `preview_quantity = preview_lots * lot_size`.
- `estimated_notional_rub = preview_quantity * reference_price` (только при
  известной цене).
- Комиссия: считается **только** если задана безопасная fee model
  (`settings.commission_bps` из `.env`), иначе
  `estimated_commission_status=UNAVAILABLE` (комиссия не выдумывается).
- НКД: для акций/ETF/money-market → `NOT_APPLICABLE`; для облигаций — только если
  безопасно доступно, иначе `UNAVAILABLE`.
- `estimated_total_rub = notional + commission (если есть) + НКД (если есть)`.
- `cash_check_status` по умолчанию `UNKNOWN` (account-id не запрашивается; портфель
  не читается и не мутируется).

## max_order_rub cap

`--max-order-rub` — это **только preview cap** (ограничение размера предпросмотра),
а **не** лимит реальной заявки. F2 не отправляет заявок, поэтому это число влияет
только на то, сколько лотов показать в превью.

## Why no order is sent in F2

F2 — это стадия предпросмотра. По дорожной карте (ROADMAP, Milestone F) реальное
исполнение возможно только на стадиях F3/F4, отдельными PR, с full-access токеном
исключительно для execution-модуля, явным подтверждением владельца на каждую
заявку, preflight и kill switch. F2 сознательно не содержит ни order/execution
API, ни full-access токена, ни мутаций портфеля/конфига.

## How this leads to F3

PREVIEW_READY-кандидаты — это материал для ручного review владельца. Следующий шаг
перед любой сделкой: **F3 sandbox manual-confirmed execution** (sandbox-исполнение
с явным подтверждением и preflight). Ни один preview не разрешает отправку или
авто-исполнение.

## Validation checklist

- `python main.py income-owner-decision-report` создаёт F1 report;
- `python main.py income-order-preview --ticker T --ticker VTBR --max-order-rub 1000`;
- в JSON: `guards.stage == "F2_ORDER_PREVIEW_NO_SEND"`,
  `guards.order_send_allowed == false`, `guards.auto_execution_allowed == false`,
  `guards.full_access_token_used == false`, `guards.orders_service_used == false`;
- `summary.order_send_allowed_count == 0`,
  `summary.auto_execution_allowed_count == 0`;
- каждый preview: `manual_confirmation_required == true`,
  `order_send_allowed == false`, `auto_execution_allowed == false`,
  `full_access_token_required == false`, `orders_service_allowed == false`;
- MD содержит фразы "Заявки не отправляются", "full-access token не используется",
  "No orders were sent.", "No portfolio/config mutation.";
- safety grep по коду не показывает реальной order-implementation (только
  negative guard/test строки);
- `pytest -q` и `ruff check .` зелёные.
