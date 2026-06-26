# dashboard (F4.7 — local read-only web dashboard)

> 🔍 **Локальный READ-ONLY просмотрщик.** F4.7 — это **viewer** отчётов F4.1–F4.6.
> Он **не торгует**, **ничего не исполняет/не отменяет/не продаёт/не повторяет**,
> **не ходит в сеть/брокера**, **не использует токены** и **не имеет** POST/действий
> или кнопок торговли.

Дашборд поднимает локальный веб-сервер (Python stdlib `http.server`), привязанный к
`127.0.0.1`, читает **только** локальные `data/reports/*.json` и рендерит одну
самодостаточную HTML-страницу (CSS встроен; без внешних ресурсов/CDN/JS-библиотек/
интернета).

## Что F4.7 делает и чего НЕ делает

- ✅ читает только локальные `data/reports/*.json` (F4.1–F4.6);
- ✅ показывает: обзор/цепочку статусов, карточку первой live-сделки, позицию,
  экономику новой сделки (gross vs net PnL), валидацию дохода, safety и сырые
  отчёты (сворачиваемые JSON-блоки);
- ✅ не падает, если каких-то отчётов нет/они устарели/повреждены — показывает
  предупреждения;
- ✅ маскирует account id и **редактирует** токеноподобные строки (на случай
  случайного попадания) в state и HTML;
- ❌ **не** торгует; **нет** POST-маршрутов, **нет** `/buy` `/sell` `/cancel`
  `/retry` `/execute` `/order` и любых action-эндпоинтов; **нет** кнопок;
- ❌ **не** инициализирует брокер-клиент; **не** читает значения токенов из
  окружения; **не** использует `TINKOFF_TOKEN` / `TINKOFF_LIVE_TRADING_TOKEN` /
  `TINKOFF_SANDBOX_TOKEN`;
- ❌ **нет** планировщика/Telegram/live-адаптера.

## Маршруты (только GET)

| Маршрут | Назначение |
| --- | --- |
| `GET /` | HTML-дашборд |
| `GET /state.json` | санитизированное состояние (JSON) для отладки |
| прочее | `404` |
| `POST` (любой) | не поддерживается (`501`) |

## Разделы

1. **Overview / status chain** — F4.1…F4.6: stage, mode, статус (по `_exit_code`),
   число warnings/errors, generated_at/checked_at.
2. **First live trade** — ticker, order_id, account (masked), fill qty/price,
   gross amount, commission raw/abs, cash outflow, attribution confidence/method.
3. **Position** — total units, average price, current price, position value,
   total unrealized PnL (с пометкой, что PnL всей позиции **отдельно** от PnL
   новой сделки).
4. **New-fill economics** — gross PnL (до комиссии), net PnL (после комиссии),
   commission drag, break-even, distance to break-even, доля новой сделки.
5. **Income validation** — income_data_checked, reliable_income_data_found,
   confidence, source, ожидаемый дивиденд/единица, доход new-fill и total
   (раздельно), покрытие цели 150000 RUB/мес, ближайшее событие, gross/net
   налоговое предупреждение.
6. **Safety** — guards и token_policy.
7. **Raw reports** — JSON в сворачиваемых блоках (санитизированный).

## Safety-вердикт: read-only стадии vs стадия исполнения

Вердикт безопасности дашборда (`overall_status`) считается по **read-only**
аналитическим стадиям **F4.2–F4.6**: их guard-флаги обязаны быть **все false**.
Если хоть один небезопасный флаг (`live_order_sent`, `post_order_called`,
`cancel_order_called`, `sell_order_sent`, `market_order_used`, `retry_execution`,
`portfolio_mutated`, `config_mutated`, `telegram_sent`, `live_token_used`,
`sandbox_token_used`, `token_printed`) у этих стадий `true` — статус
`BLOCKED_UNSAFE`.

**F4.1 — стадия исполнения**: один manual-confirmed ордер действительно был
отправлен, поэтому её флаги исполнения ожидаемо `true`. Они показываются **отдельно
и прозрачно** (`execution_stage`), но **не** входят в вердикт безопасности read-only
дашборда (иначе статус был бы всегда красным). `token_policy` в safety-карточке
берётся из самой свежей **read-only** стадии.

## Запуск / остановка

```powershell
python main.py dashboard --host 127.0.0.1 --port 8765
# затем открыть в браузере:
#   http://127.0.0.1:8765
# остановить сервер: Ctrl+C
```

| Опция | По умолчанию | Назначение |
| --- | --- | --- |
| `--host` | `127.0.0.1` | адрес привязки |
| `--port` | `8765` | порт |
| `--reports-dir` | `data/reports` | каталог отчётов (только чтение) |

## Безопасность

- По умолчанию сервер слушает **только** `127.0.0.1` (локально). Если явно передать
  другой `--host`, в консоли и на странице выводится **предупреждение** (дашборд
  может стать доступен другим в сети).
- Значения токенов никогда не читаются, не печатаются, не возвращаются и не
  попадают в HTML; account id маскируется; токеноподобные строки редактируются.
- Только чтение: дашборд не пишет в `.env`, `data/config/*` и не коммитит runtime-
  отчёты. Файлы `data/reports/*` — gitignored.

## Тестируемость

Чистые функции вынесены отдельно (тестируются без сервера):

- `load_dashboard_state(reports_dir="data/reports") -> dict` — агрегирует отчёты в
  состояние (`kind`/`stage`/`mode`, `reports_loaded/missing/stale_or_invalid`,
  `overall_status`, summaries, `guards_summary`, `token_policy_summary`, …).
- `build_dashboard_html(state) -> str` — рендерит state в HTML (без внешних
  ресурсов).
- `sanitize_dashboard_state(state) -> dict` — рекурсивно редактирует токеноподобные
  строки и маскирует raw account id.

## Дальше (F4.9, не сейчас)

F4.7 — **только просмотр**. Возможный будущий F4.9 может добавить кнопки для запуска
**read-only** отчётов из дашборда, но **по-прежнему без торговли** (никаких
order/cancel/sell/retry/execute).
