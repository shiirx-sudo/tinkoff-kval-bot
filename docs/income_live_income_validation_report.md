# income-live-income-validation (F4.6 — read-only income/dividend data validation)

> 🔍 **READ-ONLY валидация данных.** Эта команда только **проверяет**, есть ли
> НАДЁЖНЫЕ данные о доходе/дивидендах для конкретного инструмента/позиции и можно
> ли их безопасно использовать. Она **не торгует**, **ничего не исполняет** и
> **ничего не угадывает**.

После F4.5 income-поля всё ещё `null` («надёжных данных о доходе нет»). F4.6
отвечает на вопрос **отдельно от торговли**: существует ли надёжный read-only
источник дохода для этого инструмента, и если да — какой ожидаемый доход он даёт
для новой сделки и для всей позиции (раздельно).

## Что F4.6 делает и чего НЕ делает

- ✅ читает идентификаторы инструмента и контекст позиции из отчётов F4.1–F4.5;
- ✅ проверяет доходные данные через существующий read-only механизм проекта
  `modules/income_sources.fetch_dividend_data` (T-Invest `GetDividends`);
- ✅ при НАДЁЖНОМ годовом дивиденде на единицу считает ожидаемый доход для новой
  сделки и для всей позиции **раздельно**, плюс покрытие цели `150000 RUB/мес`;
- ✅ сообщает ближайшее известное дивидендное событие **отдельно** от аннуализации;
- ❌ **не** торгует, **не** вызывает PostOrder, **не** отменяет, **не** продаёт,
  **не** ретраит, **не** использует MARKET; **не** мутирует портфель/config;
  **не** шлёт Telegram;
- ❌ **не** требует/не использует `TINKOFF_LIVE_TRADING_TOKEN`;
- ❌ **не** использует `TINKOFF_SANDBOX_TOKEN`.

## Без угадывания дивидендов

Доход считается **только** при надёжном источнике. Конкретно:

- **Надёжно** (`reliable_income_data_found=true`, confidence `high`): источник дал
  объявленную будущую годовую выплату на единицу (`api_known_future`) или
  пользователь задал её вручную (`manual_override`). Тогда считаются ожидаемый
  доход new-fill/total и покрытие цели.
- **Trailing-оценка** (`api_trailing_12m`): историческая выплата за 12 месяцев —
  это **ОЦЕНКА, не гарантия** будущих выплат. Как надёжный годовой доход **не**
  используется: income-поля `null`, confidence `low`, blocking reason
  `income_estimate_trailing_not_guaranteed`.
- **Одно будущее событие без аннуализации**: событие сообщается отдельно
  (`next_known_income_event_*`), но **не аннуализируется автоматически**; income-
  поля `null`, blocking reason `future_event_present_but_not_annualizable`.
- **Источник отсутствует / неоднозначен / не привязан к точному UID/FIGI**:
  `reliable_income_data_found=false`, income-поля `null`, blocking reason
  `no_reliable_income_source`.

Дивиденды **не** выводятся из старых ценовых графиков, неформальных допущений или
«репутации» тикера. PnL и среднее всей позиции для оценки дохода **не**
используются.

## Разовое событие ≠ надёжный месячный/годовой доход

Известная будущая выплата (`next_known_income_event_*`) — это **не** то же самое,
что надёжный месячный/годовой доход. Аннуализация выполняется **только** если
источник её явно поддерживает (объявленные будущие выплаты). Иначе месячные/годовые
оценки остаются `null`.

## New-fill доход и доход всей позиции — раздельно

- new-fill: `expected_income_rub_yearly_new_fill = per_unit × new_fill_quantity_units`;
  месячный = годовой / 12; покрытие = месячный / 150000 × 100.
- total: `expected_income_rub_yearly_total_position = per_unit × current_total_position_units`;
  аналогично месячный/покрытие.

Это разные величины и держатся раздельно.

## Gross vs net (налог)

Оценка дохода — **БРУТТО** (до налога). Налоговый режим инструмента нам неизвестен,
поэтому net-доход **не** считается (не угадываем): `withholding_tax_assumption` и
`withholding_tax_source` = `null`, добавляется предупреждение. Удержание налога
учитывайте отдельно.

## Token policy

- Опционально аналитический read-only `TINKOFF_TOKEN` — **только** для read-only
  валидации доходных данных (`GetDividends`). Значение не печатается.
- `TINKOFF_LIVE_TRADING_TOKEN` не требуется/не используется
  (`live_token_used=false`); `TINKOFF_SANDBOX_TOKEN` не используется.
- Отсутствие `TINKOFF_TOKEN` **не блокирует**: команда завершается успешно (exit
  `0`) с `reliable_income_data_found=false` и понятным предупреждением; сетевых
  вызовов не выполняется.
- Если токен есть, но клиент не поддерживает метод доходных данных —
  `income_data_source=unsupported_by_current_client`, без выдуманных данных.
- Если в отчётах нет идентификаторов инструмента (figi/uid) — команда падает
  **чисто** (exit `1`) **без сетевых вызовов**.
- account id в отчёте маскируется.

## CLI

```powershell
# Достаточно существующих отчётов F4.1–F4.5. Токен опционален: при наличии
# выполняется read-only проверка дивидендов через GetDividends.
python main.py income-live-income-validation `
  --ticker T --order-id 80578688754 --live-account-id <ACCOUNT_ID>
```

| Опция | По умолчанию | Назначение |
| --- | --- | --- |
| `--ticker` | `T` | тикер |
| `--order-id` | — (обязательно) | id завершённой live-заявки (контекст позиции) |
| `--live-account-id` | — (обязательно) | live account id |
| `--f41-report` | `data/reports/income_live_execution_report.json` | F4.1 (чтение) |
| `--f42-report` | `data/reports/income_live_order_status_report.json` | F4.2 (чтение) |
| `--f43-report` | `data/reports/income_live_position_report.json` | F4.3 (чтение) |
| `--f44-report` | `data/reports/income_live_fill_attribution_report.json` | F4.4 (чтение) |
| `--f45-report` | `data/reports/income_live_fill_economics_report.json` | F4.5 (чтение) |
| `--output-json` | `data/reports/income_live_income_validation_report.json` | JSON |
| `--output-md` | `data/reports/income_live_income_validation_report.md` | Markdown |

## Отчёт

`data/reports/income_live_income_validation_report.json` и `.md` (gitignored).
Ключевые поля: `stage` (`F4_6_LIVE_INCOME_VALIDATION_READ_ONLY`), `mode`
(`INCOME_VALIDATION_READ_ONLY`), идентификаторы инструмента и контекст позиции,
`income_data_checked` / `reliable_income_data_found` / `income_data_confidence`
(`none`/`low`/`medium`/`high`) / `income_data_source` / `income_data_as_of` /
`income_data_sources_checked`, ожидаемый доход (`expected_dividend_per_unit_rub`,
`expected_dividend_yield_pct`, `expected_income_rub_{monthly,yearly}_{new_fill,
total_position}`, `income_target_coverage_pct_{new_fill,total_position}`),
`next_known_income_event_*`, налоговые поля `withholding_tax_*`,
`income_validation_passed` + `income_validation_blocking_reasons`, `checked_at`,
`guards`, `token_policy`, `warnings`, `errors`.

`guards` фиксируют read-only контракт: `live_order_sent=false`,
`post_order_called=false`, `cancel_order_called=false`, `sell_order_sent=false`,
`market_order_used=false`, `retry_execution=false`, `portfolio_mutated=false`,
`config_mutated=false`, `telegram_sent=false`, `live_token_used=false`,
`sandbox_token_used=false`, `token_printed=false`.
