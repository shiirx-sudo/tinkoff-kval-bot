# contribution-plan (F4.10/F4.10.1 — contribution plan & contribution-fact tracking)

> 🔒 **План пополнений локальный; факт — из read-only операций брокера.** ПЛАН
> (цели/старт/график) хранится локально в `data/config/contribution_plan.json`. ФАКТ
> пополнений по умолчанию (F4.10.1) извлекается из **read-only операций брокера**
> тем же путём, что и F4.8 — депозиты счёта считаются взносами. Ручные `facts[]` —
> только **fallback/корректировки**. Модуль **не торгует**, **сам в сеть не ходит**
> и **токены не читает**: список операций ему **передаёт** дашборд F4.8.

## Источник факта: API по умолчанию, manual — fallback

- **`fact_source: api_operations`** (по умолчанию) — факт берётся из read-only
  операций (депозиты `OPERATION_TYPE_INPUT`). Авторитетный расчёт делает дашборд
  F4.8 (`portfolio-dashboard-data`), который и передаёт операции в общий модуль.
- **`fact_source: manual`** — факт берётся только из ручных `facts[]`.
- **`fact_source: mixed`** + `manual_facts_enabled: true` — API-депозиты как основа,
  ручные facts как корректировки (дедуп по `operation_id` или паре дата+сумма).
- Если API-операции **недоступны** (нет токена/ошибка чтения) — модуль делает
  **manual fallback** и помечает предупреждением
  `contribution_api_operations_unavailable_manual_fallback`.

**Что считается фактом:** только денежные пополнения счёта (`OPERATION_TYPE_INPUT`).
Покупки/продажи, дивиденды/купоны, комиссии/налоги — **не** взносы. **Выводы**
(`OPERATION_TYPE_OUTPUT`) учитываются **отдельно** (не уменьшают взнос) и идут в
**net cash flow**. Неизвестный тип операции **не угадывается**: факт не создаётся,
добавляется предупреждение `contribution_api_operation_type_unrecognized`.

`contribution-plan-status` в CLI **операции брокера не читает** — там факт только
локальный/ручной; авторитетный факт пополнений показывает дашборд F4.8.

Общий модуль расчёта — `modules/contribution_plan.py` (единая логика для CLI F4.10 и
блока «Взносы» в F4.8/F4.9).

## Файлы

- **Локальный план (не коммитится):** `data/config/contribution_plan.json`
- **Пример (закоммичен):** `config/contribution_plan.example.json`
- **Отчёты статуса (не коммитятся):**
  `data/reports/contribution_plan_status_report.json` и `.md`

## Схема плана

```json
{
  "enabled": true,
  "currency": "rub",
  "plan_weekly_rub": 50000,
  "plan_monthly_rub": 200000,
  "plan_start_date": "2026-06-01",
  "next_planned_contribution_date": "2026-07-06",
  "source": "manual",
  "fact_source": "api_operations",
  "manual_facts_enabled": false,
  "facts": []
}
```

Правила: `enabled` — bool; `currency` — только `rub`; `plan_weekly_rub`/
`plan_monthly_rub` ≥ 0; даты — валидный ISO; `facts[].amount_rub` > 0; дубликат
`date+amount` не добавляется повторно без `--allow-duplicate`. `fact_source` ∈
{`api_operations` (по умолчанию), `manual`, `mixed`}; `manual_facts_enabled` — bool
(по умолчанию `false`). **Старые конфиги без `fact_source`/`manual_facts_enabled`
по-прежнему загружаются** (подставляются дефолты). `contribution-plan-init`
записывает эти ключи; `contribution-plan-add` работает, но печатает предупреждение,
что ручные facts — это fallback/корректировки.

## Команды

### Инициализация / обновление плана
```powershell
python main.py contribution-plan-init --weekly-rub 50000 --monthly-rub 200000 `
  --start-date 2026-06-01 --next-date 2026-07-06
```
Создаёт `data/config/contribution_plan.json`. При обновлении существующего плана
**сохраняет facts** (очистить — `--reset-facts`).

### Добавить факт пополнения
```powershell
python main.py contribution-plan-add --date 2026-06-28 --amount-rub 50000
```
Добавляет факт, сортирует по дате; дубликат `date+amount` отклоняется (если не
передан `--allow-duplicate`); печатает обновлённый статус.

### Статус (пишет отчёты)
```powershell
python main.py contribution-plan-status
```
Печатает статус и пишет `data/reports/contribution_plan_status_report.{json,md}`.
Если плана нет — статус `NOT_CONFIGURED`, **exit 0** и подсказка-команда настройки.
`contribution-plan-report` — алиас той же команды.

Дата расчёта — текущая локальная, либо `--as-of YYYY-MM-DD`.

## Определения расчётов

- **Факт за неделю** — сумма facts с **понедельника** текущей недели по `as_of`
  включительно.
- **Факт за месяц** — с **первого числа** месяца по `as_of`.
- **Факт YTD** — с **1 января** (или `plan_start_date`, что позже) по `as_of`.
- **Ожидается:** неделя = `plan_weekly_rub`, месяц = `plan_monthly_rub`;
  YTD = `plan_monthly_rub * (дней с начала / 30.4375)` (30.4375 = 365.25/12 —
  среднее число дней в месяце; формула в отчёте: `expected_ytd_formula`).
- **Разрыв** = `max(ожидается − факт, 0)`.
- **Пропущено:** неделя = 1, если недельный план > 0 и недельный факт < ожидания,
  иначе 0; месяц/YTD = `ceil(разрыв / plan_weekly_rub)` если недельный план > 0,
  иначе `1` при разрыве > 0.
- **Нужно довнести** = месячный разрыв.
- **Выводы / net cash flow** (вторично): выводы суммируются отдельно;
  `net_cash_flow = взносы − выводы` по неделе/месяцу/YTD.
- **Статус:** `ON_TRACK` (недельный и месячный разрыв = 0), `BEHIND` (разрыв > 0),
  `NOT_STARTED` (до даты `plan_start_date`), `DISABLED` (`enabled=false`),
  `NOT_CONFIGURED` (плана нет).

### До старта плана (`as_of < plan_start_date`)

Пока план не стартовал, **долг не создаётся**: ожидаемые взносы за неделю/месяц/YTD
= `0`, разрывы = `0`, пропущено = `0`, статус `NOT_STARTED`. Факт пополнений
всё равно показывается, если он есть. Поля `contribution_plan_started=false` и
`days_until_plan_start` сообщают, сколько дней до старта. С даты старта включительно
действует обычная логика.

## Связь с F4.8/F4.9

F4.8 `portfolio-dashboard-data` использует **тот же** модуль расчёта и **передаёт в
него read-only операции** (тот же путь, что для оборота). `contributions_summary`
содержит источник/качество и вторичные метрики:

- `contribution_source` — `readonly_operations_api` | `manual_fallback` |
  `mixed_api_plus_manual_adjustments`;
- `contribution_data_quality` — `full` | `partial` | `manual_fallback`;
- `contribution_fact_source_preferred`, `contribution_api_deposit_facts_count`,
  `contribution_manual_facts_count`, `contribution_api_withdrawal_facts_count`;
- `withdrawal_fact_{weekly,monthly,ytd}_rub`, `net_cash_flow_{weekly,monthly,ytd}_rub`;
- `last_contribution_date`, `last_contribution_amount_rub`,
  `contribution_facts_preview`, `contribution_warnings`;
- `contribution_plan_started`, `days_until_plan_start`.

F4.9 показывает источник факта, качество данных, статус старта плана, последний
взнос, число API-депозитов/ручных фактов, выводы и net cash flow как вторичные
метрики, и предупреждение при manual fallback. UI обновляется автоматически после
перезапуска F4.8.

> Это не инвестиционная рекомендация. Депозиты идут в зачёт плана пополнений;
> выводы трекаются отдельно; net cash flow показывается отдельно.

Обновить дашборд после изменения плана:

1. `python main.py contribution-plan-status` (или `...-add`/`...-init`);
2. `python main.py portfolio-dashboard-data --live-account-id <ACCOUNT_ID>`;
3. перезапустите/обновите `python main.py portfolio-dashboard --host 127.0.0.1 --port 8766`.

## Безопасность

- Мутирует **только** локальные `data/config/contribution_plan.json` и
  `data/reports/contribution_plan_status_report.{json,md}` — все gitignored.
- Не трогает портфель/брокер/`.env`. Нет сети/токенов/торговли/Telegram/
  планировщика. Секреты в вывод не попадают. `guards`/`token_policy` фиксируют
  контракт (`broker_api_called=false`, `config_mutated=true` только для
  init/add, `token_printed=false`).
