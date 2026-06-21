# income-coupon-validation — read-only диагностика купонов

`income-coupon-validation` — это **read-only** диагностический отчёт по
купонным/облигационным кандидатам, которые `income-universe-audit` относит к
**группе C (coupon-validation)**. Команда отвечает на вопрос «что мешает
считать этих кандидатов income-ready» — и ничего не включает.

## Что команда делает

- читает только локальные отчёты:
  - `data/reports/income_universe_builder_report.json` (результат `build-income-universe`);
  - `data/reports/income_universe_disabled_audit.json` (результат `income-universe-audit`);
- выбирает кандидатов **только из audit group C**;
- классифицирует купон: `floating` / `fixed` / `unknown`;
- определяет `coupon_validation_status` и `income_readiness`;
- блокирует наивную annualization для floating и неполных данных
  (annualization guard);
- в `--offline` работает только по отчётам, без сети;
- в API-режиме (по умолчанию) может дополнить данные read-only методами
  T-Invest (резолв инструмента, купонный календарь, последняя цена); при
  отсутствии токена/ошибке тихо деградирует в offline;
- пишет `data/reports/income_coupon_validation.json` и `.md`.

## Что команда НЕ делает

- не отправляет, не отменяет и не модифицирует заявки; не исполняет; не торгует;
- не использует full-access токен — только read-only методы;
- не меняет income policy, target portfolio, income universe builder enable
  logic или resolver behavior;
- не пишет в `data/config/*.yaml`;
- **не включает (auto-enable)** ни одного disabled-кандидата;
- не даёт инвестиционных рекомендаций (нет слов «купить»/«продать»/«исключить»
  как рекомендации) — это аналитика.

`auto_enable_allowed=false` для всех кандидатов; `recommendation_guard` всегда
`candidate_for_analysis_only`.

## Почему coupon validation нужен до включения OFZ-PK / облигаций

Облигации и особенно ОФЗ-ПК (флоатеры) дают доход не «дивидендом», а потоком
купонов, размер которых зависит от купонного календаря, типа купона
(фиксированный/плавающий), номинала, частоты выплат и цены. Пока эти данные не
провалидированы read-only API, любой расчёт доходности — догадка. Поэтому такие
кандидаты остаются disabled до отдельного policy review.

## Почему floating coupon нельзя annualize наивно

Для плавающего купона (`COUPON_TYPE_FLOATING` / `COUPON_TYPE_OFZ_PK` /
`COUPON_TYPE_VARIABLE`, либо `floatingCouponFlag=true`) будущая ставка
**неизвестна**. Формула вида `купон × частота / цена` по последнему купону даёт
ложную доходность. Annualization guard блокирует расчёт, если:

- купон плавающий;
- неизвестна частота купонов;
- неизвестна цена или номинал;
- нет ближайшего купона;
- нет купонного календаря.

Только когда **все** guard-условия пройдены (фиксированный купон + полный набор
данных), считается **диагностический** gross coupon yield
(`estimated_gross_yield_pct`). Это по-прежнему не рекомендация и не auto-enable.

## Статусы

`coupon_validation_status`:

- `coupon_schedule_available` — есть купонный календарь, тип не определён однозначно;
- `floating_coupon_detected` — плавающий купон;
- `fixed_coupon_detected` — фиксированный купон с календарём;
- `coupon_data_missing` — облигация без доступного календаря;
- `unresolved_instrument` — нет проверенного secid/ISIN/ticker/class_code;
- `insufficient_data` — данных недостаточно (или инструмент не купонный);
- `validation_error` — ошибка валидации.

`income_readiness` (ни одно значение не означает auto-enable):

- `not_ready`, `data_missing`, `needs_floating_coupon_policy`,
  `needs_annualization_guard`, `needs_manual_review`,
  `candidate_for_future_policy_review`.

## Как запускать

```bash
python main.py build-income-universe --force
python main.py income-universe-audit
python main.py income-coupon-validation
```

Только по локальным отчётам, без сети:

```bash
python main.py income-coupon-validation --offline
```

Аргументы:

- `--builder-report` (по умолчанию `data/reports/income_universe_builder_report.json`);
- `--audit-report` (по умолчанию `data/reports/income_universe_disabled_audit.json`);
- `--output-json` (по умолчанию `data/reports/income_coupon_validation.json`);
- `--output-md` (по умолчанию `data/reports/income_coupon_validation.md`);
- `--offline` — без read-only API.

Если входных отчётов нет, команда печатает понятную ошибку и завершается с кодом
1, предлагая сначала запустить `build-income-universe --force` и
`income-universe-audit`.

## Output является diagnostics only

Все поля отчёта — диагностика для последующего ручного policy review. Никакой
строки нельзя интерпретировать как сигнал к сделке или к включению инструмента в
income universe.
