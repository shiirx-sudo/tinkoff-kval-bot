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
python main.py kval-status     # прогресс + отчёты в data/reports/
python main.py kval-status --as-of 2026-03-31
python main.py kval-status --reports-dir out/reports
python main.py -v kval-status  # DEBUG-логирование
```

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
