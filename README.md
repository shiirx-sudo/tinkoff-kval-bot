# T-Invest Kval Bot

Инструмент для расчёта **прогресса к статусу квалифицированного инвестора** в
Т-Инвестициях по критерию **оборота сделок** за последние 4 завершённых квартала.

> **Read-only.** Используется только токен на чтение, обращение к Tinkoff Invest API
> идёт напрямую по REST (без SDK). Торговые заявки не отправляются — `LIVE_ENABLED=false`
> принудительно на этапе 1.

## Возможности (этап 1)

- Список брокерских счетов по токену (идентификаторы маскируются).
- Оборот по покупкам/продажам ЦБ, фьючерсам и опционам; исключает валюту,
  драгметаллы, комиссии, налоги, дивиденды, купоны, ввод/вывод, РЕПО, овернайты.
- Точный оборот по `trades` (`price × quantity`) с fallback на `payment`, если у
  операции нет детализации (такие помечаются приближёнными — нужна сверка с отчётом).
- Разбивка по счетам и кварталам; процент выполнения цели и остаток (с буфером).
- Выходные отчёты: `kval_progress.json`, `kval_progress.csv`, `kval_accounts.csv`,
  `kval_trades.csv`, `kval_quarters.csv`, `broker_sync_status.csv`.

## Период расчёта

Период = ровно **4 последних завершённых квартала**. Для даты **2026-06-11** период
**2025-04-01 — 2026-03-31** (квартал, в котором находится дата, не завершён и в окно
не входит). См. `ANALYSIS.md` по открытому вопросу о правиле периода.

## Установка

```bash
git clone https://github.com/shiirx-sudo/tinkoff-kval-bot.git
cd tinkoff-kval-bot
python -m venv .venv && .venv\Scripts\activate   # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env        # затем впишите токен в .env (Linux/macOS: cp)
```

В `.env` — **read-only** токен:

```env
TINKOFF_READ_TOKEN=ваш_токен_только_на_чтение
LIVE_ENABLED=false
```

(совместимо: если задан только `TINKOFF_TOKEN`, он используется как фолбэк.)

## Команды

```bash
python main.py doctor          # проверка окружения/конфигурации
python main.py accounts        # список брокерских счетов (масками)
python main.py kval-status     # официальный факт по 4 завершённым кварталам + отчёты
python main.py kval-status --as-of 2026-03-31
python main.py kval-plan       # прогноз будущих окон и календарь выполнения условий
python main.py instrument-scan --symbols LQDT --commission-bps 5   # read-only оценка ликвидности/издержек
python main.py turnover-plan   # read-only расчётный план ручного набора оборота (ничего не покупает)
python main.py turnover-plan --instrument LQDT --mode roundtrip
python main.py execution-plan --as-of 2026-07-01 --instrument LQDT --mode roundtrip --commission-bps 5
python main.py execution-preflight --instrument LQDT --max-side-notional-rub 130000 --spread-bps-limit 5
python main.py execution-plan --instrument LQDT --mode roundtrip --size-mode balance   # размер от свободного баланса
python main.py passive-income-summary            # read-only разбивка портфеля
python main.py -v kval-status  # DEBUG-логирование
```

**Balance-adaptive сайзинг.** При `EXECUTION_SIZE_MODE=balance` (или `--size-mode
balance`) размер одной стороны считается от фактического свободного остатка на
счёте (read-only `GetPositions`): `usable = свободные ₽ − резерв`, `side_cap =
usable × utilization`, ограниченный глубиной стакана и опциональным
`EXECUTION_MAX_SIDE_NOTIONAL_RUB`. Число действий = `ceil(нужный_оборот /
side_cap)`, но не меньше `EXECUTION_MIN_MONTHLY_ACTIONS` (по умолчанию 4 → 48
сделок в год при минимуме `KVAL_MIN_TOTAL_TRADES=41`). Так жёсткая цифра `130000`
больше не нужна: маленький баланс → больше мелких сделок, большой баланс → не
ниже безопасного минимума. Всё только расчёт, заявки не отправляются.

`passive-income-summary` — read-only разбивка портфеля (свободные рубли, фонды
денежного рынка, облигации, акции, ожидаемая доходность, доля капитала вне
kval-turnover). Это аналитика, не рекомендация; покупок/продаж нет.

`turnover-plan` синтезирует уже готовые отчёты (`kval_plan.json`, `instrument_scan.json`)
и считает, сколько оборота/сделок ориентировочно нужно добрать вручную в текущем
месяце/квартале и какой номинал одной операции (для `gross`) или на сторону
(`roundtrip` buy+sell). Это только расчёт для ручных действий: команда ничего не
покупает и не меняет портфель; фактический зачёт оборота сверяйте с брокером.

`execution-plan` строит **dry-run** план будущих BUY/SELL действий для
автоматического набора оборота: явно разделяет broker trades, roundtrip-циклы
(`ceil(missing/2)`), номинал стороны (`remaining / (cycles*2)`), оборот цикла и
проверки рисков (GOOD, NORMAL_TRADING, спред, глубина, лимит стороны). **Реальные
заявки не отправляются**: модуль не размещает и не отменяет заявки, не меняет
портфель, `dry_run` всегда включён. Live-исполнение — отдельный будущий этап,
только после проверки dry-run и явного включения отдельного адаптера.

`execution-preflight` — **read-only** проверка готовности: перечитывает/пересобирает
dry-run план и убеждается, что он безопасен по лимитам и данным (вердикт GOOD,
NORMAL_TRADING, спред ≤ лимита, запас глубины `--min-depth-multiplier`,
`side_notional ≤ --max-side-notional-rub`, все действия `dry_run=true`) и что в
кодовой базе нет order-endpoints или live-адаптера. Итог: `READY_DRY_RUN` /
`BLOCKED` / `STALE_REPORTS` / `MISSING_REPORTS`. Разница простая: `execution-plan`
строит план, `execution-preflight` проверяет его — реального исполнения нет ни там,
ни там, заявки не отправляются.

## Telegram-уведомления (read-only)

Бот может слать короткий статус мониторинга в Telegram. Это только уведомления о
содержимом отчётов — никаких заявок, order-endpoints или изменения портфеля; токен
нигде не логируется.

Создание бота: в Telegram откройте **@BotFather** → `/newbot` → получите
`TELEGRAM_BOT_TOKEN`. Узнать `TELEGRAM_CHAT_ID` можно, написав боту и открыв
`https://api.telegram.org/bot<token>/getUpdates` (поле `chat.id`).

В `.env` укажите (по умолчанию всё выключено):

```env
TELEGRAM_ALERTS_ENABLED=false
TELEGRAM_BOT_TOKEN=123456:ВАШ_ТОКЕН
TELEGRAM_CHAT_ID=123456789
TELEGRAM_ALERT_MIN_INTERVAL_MINUTES=60
TELEGRAM_DAILY_SUMMARY_ENABLED=true
TELEGRAM_DAILY_SUMMARY_HOUR=10
TELEGRAM_STATUS_CHANGE_ONLY=true
```

Проверка и использование:

```bash
python main.py telegram-summary                 # печатает сводку, ничего не шлёт
python main.py telegram-test --dry-run true      # проверка без отправки
python main.py telegram-test --dry-run false     # реальная отправка (нужен ALERTS_ENABLED=true или --force true)
python main.py telegram-notify --dry-run true    # авто-решение об отправке (для runner)
```

`telegram-notify` сам решает, слать ли: при смене статуса — сразу; при
`BLOCKED`/`MISSING_REPORTS`/`STALE_REPORTS` — сразу, но не чаще
`TELEGRAM_ALERT_MIN_INTERVAL_MINUTES`; при `READY_DRY_RUN` — раз в день (daily
summary); плюс предупреждения о приближении конца месяца (10/5/3/1 дн.) и квартала
(21/14/7/3/1 дн.). Состояние антиспама — в `data/alerts/telegram_alert_state.json`.

В `run_kval_monitor.ps1` добавьте последней строкой после `execution-preflight`:

```powershell
python main.py telegram-notify --dry-run false
```

Уведомления полностью read-only: они сообщают о статусе, но ничего не покупают, не
продают и не меняют портфель.

## Сигналы trend_signal_v1 (read-only)

Стратегия анализирует свечи watchlist и выдаёт сигналы BUY / SELL / HOLD / SKIP с
score 0–100, риск-моделью (entry/stop/take-profit — **справочно, не рекомендация**)
и Telegram-уведомлениями. Это уведомления, а не приказы: заявки не отправляются,
портфель не меняется, всё строго read-only.

```bash
python main.py strategy-scan --strategy trend_signal_v1            # скан + отчёты
python main.py strategy-scan --strategy trend_signal_v1 --notify   # + Telegram BUY/SELL
python main.py strategy-status                                     # конфиг + последние сигналы
```

Параметры: `--watchlist`, `--min-score`, `--timeframe`, `--max-signals`, `--as-of`,
`--notify`. Watchlist поддерживает явный class_code: `TQBR:SBER` или `SBER@TQBR`;
без него инструмент выбирается по `SIGNALS_CLASS_CODE_PRIORITY` (TQBR→TQTF→SPBRU). Если
подходящего класса нет — `SKIP` с причиной `no_allowed_class_code_match`. Telegram
вызывается только при `--notify` (иначе пишутся лишь отчёты). SELL учитывает портфель
(read-only): по бумаге из портфеля — `SELL / EXIT WATCH`, иначе сигнал понижается до
`AVOID` (технически слабо, не шорт, в Telegram не шлётся); недоступный портфель →
`AVOID` с `held_unknown`. Управляется `SIGNALS_SELL_ONLY_IF_HELD` (по умолчанию true),
счёт — `--account-id` (иначе первый брокерский).

Опционально подключается **read-only фундаментальный фильтр качества**
(`fundamental_filter_v1`): по ручной базе `data/config/fundamental_filter.yaml`
(пример — `config/fundamental_filter.example.yaml`) по 4 качественным вопросам
(ориентация менеджмента на рост стоимости, возврат денег акционерам, роль
государства, рост рынка) считается балл 0–4 и вердикт `quality_pass` (≥3) /
`quality_watch` (≥2) / `quality_risk` (<2) / `quality_unknown` (нет данных). Флаги:
`--fundamental-filter`, `--fundamental-filter-path`, `--require-fundamental-pass`
(понижает BUY до HOLD, если качество ниже pass). Это фильтр качества и пояснение в
отчётах/Telegram, а не торговый сигнал и не инвестиционная рекомендация; интернет не
скрапится. Конфиг — переменные
`SIGNALS_*` в `.env` (по умолчанию `SIGNALS_ENABLED=false`). Правила BUY: `close>EMA200`, `EMA20>EMA50`, `RSI14` в
45–70, пробой/откат, спред ≤ лимита, ликвидность и `NORMAL_TRADING`; BUY шлётся при
`score >= SIGNALS_MIN_SCORE`. Антиспам: одинаковый сигнал по инструменту не
повторяется в пределах `SIGNALS_DEDUP_HOURS` (смена `HOLD→BUY`/`BUY→SELL` или
заметное изменение score — шлётся). Отчёты: `strategy_signals.{json,csv,md}`,
состояние — `data/state/strategy_signals_state.json`.

В runner добавляется отдельным шагом только при `SIGNALS_ENABLED=true`:
`python main.py strategy-scan --strategy trend_signal_v1 --notify` (не падает, если
сигналов нет). Telegram `/signals`, `/signals_status`, `/signals_scan` соответствуют
`strategy-status` / `strategy-scan` и text-билдерам в `notifications/signals.py`.

## Доходная аналитика income_engine_v1 (read-only)

Для цели «жить на доход» модуль считает ожидаемый поток от фондов денежного рынка,
дивидендов и купонов, строит календарь выплат и gap до целевого дохода. Всё read-only:
покупок/продаж нет, интернет не скрапится, ручные оценки явно помечаются `manual/
confidence` и не являются гарантией или рекомендацией. Нет данных по бумаге → `unknown`,
расчёт не ломается.

`--target-monthly-rub` — это произвольное пользовательское значение цели для разовой
оценки, а не зафиксированная цель проекта. Долгосрочная цель проекта — покрытие
**реальной** личной месячной корзины (baseline 150 000 ₽ на 2026-06, индексируется со
временем), а не фиксированная номинальная сумма; см. раздел «Real income target /
Living basket target» в `ROADMAP.md`.

```bash
python main.py income-summary --account-id 2057431918 --target-monthly-rub 150000
python main.py income-calendar --account-id 2057431918 --months 12
python main.py income-watchlist --watchlist TQBR:SBER,TQBR:GAZP,TQTF:LQDT
```

`income-summary` даёт стоимость портфеля по классам, валовый/чистый (после налога)
годовой и месячный доход, доходность портфеля и блок «до цели» (gap и оценку капитала
`target_annual_net / net_yield`; при неизвестной доходности — `n/a`). Ручные оценки —
в `data/config/income_engine.yaml` (пример — `config/income_engine.example.yaml`):
`manual_yields` (фонды денежного рынка), `manual_dividends` (дивиденд на акцию),
`manual_bonds` (купоны). Риск-пометки (`high_concentration`, `state_control_risk`,
`dividend_cut_risk`, `coupon_default_risk`, `unknown_income_data`, `manual_estimate`)
подтягивают вердикт `fundamental_filter_v1`. Отчёты: `income_summary.{json,csv,md}`,
`income_calendar.{json,csv}`. Telegram-сводка — только при `--notify`.

## Структура

```
tinkoff-kval-bot/
├── brokers/tinkoff/rest_client.py   # read-only REST-коннектор (+ GetOperationsByCursor)
├── api/client.py                    # фасад: счета + операции за период
├── common/helpers.py                # mask/clean/hash/utc_now/quotation→Decimal
├── config/settings.py               # конфигурация из .env (read-only)
├── modules/
│   ├── period_calculator.py         # расчёт квартального периода
│   ├── operation_filter.py          # правила учёта операций (REST-контракт)
│   ├── turnover_calculator.py       # подсчёт оборота
│   └── kval_tracker.py              # агрегация прогресса
├── reports/
│   ├── output_contract.py           # контракт отчётов (порядок колонок, валидация)
│   ├── kval_reports.py              # сборка пяти отчётов
│   ├── console_report.py            # консольный вывод (rich)
│   └── runtime_doctor.py            # doctor + валидация отчётов
├── tests/                           # pytest (моки HTTP, без реальных запросов)
├── ANALYSIS.md                      # анализ архива MOEX Advisor и план переноса
└── main.py                          # CLI: accounts / kval-status / doctor
```

## Тесты

```bash
pip install pytest
pytest -q
```

Реальные API-запросы отделены от тестов: HTTP-слой REST-клиента мокается, сеть не нужна.

## Безопасность

`.env` с токеном не коммитится (в репо только `.env.example`). Каталоги `data/manual`
и `data/reports` в `.gitignore`. Full-access токен не используется; реализованы только
read-методы (никаких postOrder/cancelOrder).

## Дисклеймер

Инструмент носит **информационный** характер и не является налоговой, юридической или
инвестиционной консультацией. Итоговые цифры сверяйте с официальным брокерским отчётом
Т-Инвестиций; пороги и правила учёта оборота могут меняться.

## Лицензия

MIT — см. [LICENSE](LICENSE).
