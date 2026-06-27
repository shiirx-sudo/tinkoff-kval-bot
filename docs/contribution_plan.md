# contribution-plan (F4.10 — local contribution plan & missed-contribution tracking)

> 🔒 **Локальный ручной учёт пополнений, отдельно от торговли.** F4.10 ведёт план
> пополнений и факты вручную и считает статус (факт/план/разрыв по неделе/месяцу/
> году, число пропущенных взносов). Он **НЕ читает депозиты брокера автоматически**,
> **не торгует**, **не ходит в сеть**, **не использует токены**.

Депозиты T-Invest read-only API надёжно не отдаёт, поэтому факт пополнений
фиксируется вручную. Эта логика — **единый источник** и для CLI F4.10, и для блока
«Взносы» в F4.8/F4.9 (общий модуль `modules/contribution_plan.py`).

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
  "facts": [
    {"date": "2026-06-08", "amount_rub": 50000}
  ]
}
```

Правила: `enabled` — bool; `currency` — только `rub`; `plan_weekly_rub`/
`plan_monthly_rub` ≥ 0; даты — валидный ISO; `facts[].amount_rub` > 0; дубликат
`date+amount` не добавляется повторно без `--allow-duplicate`.

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
- **Статус:** `ON_TRACK` (недельный и месячный разрыв = 0), `BEHIND` (разрыв > 0),
  `DISABLED` (`enabled=false`), `NOT_CONFIGURED` (плана нет).

## Связь с F4.8/F4.9

F4.8 `portfolio-dashboard-data` использует **тот же** модуль расчёта, поэтому
`contributions_summary` стал богаче (добавлены `contribution_gap_ytd_rub`,
`missed_contributions_count_week`, `days_until_next_planned_contribution`,
`contribution_status`). F4.9 UI не меняется и автоматически показывает улучшённые
данные после перезапуска F4.8.

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
