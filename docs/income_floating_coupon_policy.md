# income-floating-coupon-policy — read-only policy-диагностика ОФЗ-ПК

`income-floating-coupon-policy` — это **read-only** диагностический отчёт по
floating-coupon кандидатам (ОФЗ-ПК / `SU29…`), которые `income-coupon-validation`
помечает как `floating_coupon_detected`. Команда отвечает на вопрос «что нужно
сделать с этими флоатерами дальше» — и **ничего не включает**, не считает
доходность как факт и не даёт рекомендаций.

## Purpose

Дать единое место, где собраны floating-coupon кандидаты и явно зафиксировано:

- почему их доходность нельзя annualize/прогнозировать наивно;
- что потребуется для будущей утверждённой floating-coupon policy;
- что до этой политики они остаются только `candidate_for_analysis_only`.

## Inputs

- `data/reports/income_coupon_validation.json` — результат
  `income-coupon-validation` (по умолчанию; переопределяется `--input-json`).

Команда не читает сеть и не читает `data/config`. Если входного отчёта нет, она
печатает понятную ошибку и завершается с кодом 1, предлагая сначала запустить:

```bash
python main.py build-income-universe --force
python main.py income-universe-audit
python main.py income-coupon-validation
```

## Outputs

- `data/reports/income_floating_coupon_policy.json` (`--output-json`);
- `data/reports/income_floating_coupon_policy.md` (`--output-md`).

По каждому floating-кандидату в отчёте:

- `ticker` / `secid`, `class_code`, `role`;
- `coupon_validation_status`, `income_readiness`;
- `floating_coupon_detected=true`;
- `annualization_allowed=false`, `forecast_allowed=false`,
  `auto_enable_allowed=false`;
- `reason` — почему annualization/forecast запрещены;
- `forecast_method="not_supported_yet"`,
  `policy_status="needs_floating_coupon_policy"`, `readiness="policy_required"`,
  `recommendation_guard="candidate_for_analysis_only"`;
- `policy_requirements` — что нужно для будущей политики.

Summary: `total_candidates`, `floating_coupon_candidates`,
`annualization_allowed_count=0`, `forecast_allowed_count=0`,
`auto_enable_allowed_count=0`, `by_policy_status`, `by_readiness`.

## Why floating coupon is not annualized

ОФЗ-ПК — это флоатеры: размер будущего купона зависит от внешнего ориентира
(RUONIA / ключевая ставка) и официального механизма расчёта. Формула вида
`купон × частота / цена` по последней/следующей выплате даёт **ложную**
доходность, потому что будущая ставка неизвестна. Поэтому `annualization_allowed`
и `forecast_allowed` всегда `false`, а `forecast_method` —
`not_supported_yet`, пока нет отдельной утверждённой floating-coupon policy.

## Why no auto-enable

Отчёт диагностический. Ни одна строка не является сигналом к сделке и не
включает инструмент в income universe. `auto_enable_allowed=false` для всех
кандидатов; включение возможно только через будущий отдельный policy review и
ручное утверждение.

## How to run

```bash
python main.py income-floating-coupon-policy
```

С явными путями:

```bash
python main.py income-floating-coupon-policy \
  --input-json data/reports/income_coupon_validation.json \
  --output-json data/reports/income_floating_coupon_policy.json \
  --output-md data/reports/income_floating_coupon_policy.md
```

## Validation checklist

- `python main.py doctor` — `token_present` ok, `live_disabled` ok;
- `python -m pytest -q` — зелёный (`tests/test_floating_coupon_policy.py`);
- `ruff check .` — без замечаний;
- smoke-цепочка `build-income-universe → income-universe-audit →
  income-coupon-validation → income-floating-coupon-policy` завершается с кодом 0;
- в `income_floating_coupon_policy.json`:
  `annualization_allowed_count=0`, `forecast_allowed_count=0`,
  `auto_enable_allowed_count=0`, все тикеры — `SU29…`;
- в `income_floating_coupon_policy.md` присутствуют guard-фразы
  («Аналитика, не рекомендация.», «Заявки не отправляются.»,
  `auto_enable_allowed=false`, `forecast_allowed=false`,
  `annualization_allowed=false`) и нет торговых рекомендаций;
- `data/config` и `data/reports` не попадают в tracked changes.
