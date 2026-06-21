# income-resolver-mapping-diagnostics — read-only resolver/mapping диагностика (group D)

`income-resolver-mapping-diagnostics` — это **read-only** диагностический отчёт по
неразрешённым income-кандидатам из audit **group D** (resolver/mapping). Команда
собирает кандидатов, у которых нет проверенного `secid/ISIN/ticker/class_code`
(source — short-name), объясняет, почему они unresolved, и **готовит безопасный
ручной mapping review**. Она **ничего не маппит и не включает** автоматически.

## Purpose

Дать единое место, где собраны group D unresolved кандидаты и явно зафиксировано:

- почему конкретный кандидат не разрешён резолвером (короткое имя вместо
  проверенного тикера/класса);
- какие read-only совпадения нашлись (если включён API enrichment) — как
  материал для ручного review, а не как готовый mapping;
- что до ручного mapping/отдельного PR кандидат остаётся только
  `candidate_for_mapping_review_only`.

## Inputs

- `data/reports/income_universe_disabled_audit.json` — результат
  `income-universe-audit` (по умолчанию; переопределяется `--input-json`).

Берутся только кандидаты с `audit_group == "D"`. Если входного отчёта нет,
команда печатает понятную ошибку и завершается с кодом 1, предлагая сначала
запустить:

```bash
python main.py build-income-universe --force
python main.py income-universe-audit
```

## Outputs

- `data/reports/income_resolver_mapping_diagnostics.json` (`--output-json`);
- `data/reports/income_resolver_mapping_diagnostics.md` (`--output-md`).

По каждому group D кандидату в отчёте:

- `original_ticker`, `name`, `role`, `policy_bucket`, `excluded_reason`,
  `class_code`, `notes`;
- `reason` — почему кандидат unresolved;
- `mapping_status` — `unresolved` / `candidate_matches_found` /
  `ambiguous_matches` / `no_matches`;
- `candidates_for_manual_review` — найденные read-only совпадения (только в
  API-режиме), каждое с `ticker`, `class_code`, `figi`, `uid`, `isin`, `name`,
  `instrument_type`, `currency`, `exchange`, `match_reason`;
- `auto_enable_allowed=false`, `auto_mapping_allowed=false`,
  `recommendation_guard="candidate_for_mapping_review_only"`.

Summary: `total_candidates`, `unresolved_count`,
`candidate_matches_found_count`, `ambiguous_matches_count`, `no_matches_count`,
`auto_mapping_allowed_count=0`, `auto_enable_allowed_count=0`,
`by_mapping_status`.

## Offline / API mode

- **offline** (`--offline`): команда работает только по audit-отчёту, к сети не
  обращается; `mapping_status` для всех кандидатов = `unresolved` (попытки
  сопоставления не было), `mode="offline"`.
- **API** (по умолчанию): команда делает read-only попытку enrichment через
  `ReadOnlyClient.find_instruments` (InstrumentsService/FindInstrument). Если
  токена нет или API недоступен — деградирует в offline-подобный режим без
  падения. `mode="api"`.

Правила `mapping_status` в API-режиме: `0` совпадений → `no_matches`;
ровно `1` сильное совпадение → `candidate_matches_found`; `>1` → `ambiguous_matches`.

## Why no auto-mapping

Отчёт диагностический. Найденные совпадения — это `candidates_for_manual_review`,
а не applied mapping:

- даже один точный match оставляет `auto_mapping_allowed=false`;
- mapping должен быть **ручным и отдельным PR/изменением**;
- отчёт **не меняет** source candidate, income universe, `data/config`, target
  portfolio, resolver behavior, builder enable logic, income policy или Telegram;
- ни один кандидат не включается автоматически (`auto_enable_allowed=false`).

## How to run

```bash
python main.py income-resolver-mapping-diagnostics
python main.py income-resolver-mapping-diagnostics --offline
```

С явными путями:

```bash
python main.py income-resolver-mapping-diagnostics \
  --input-json data/reports/income_universe_disabled_audit.json \
  --output-json data/reports/income_resolver_mapping_diagnostics.json \
  --output-md data/reports/income_resolver_mapping_diagnostics.md
```

## Validation checklist

- `python main.py doctor` — `token_present` ok, `live_disabled` ok;
- `python -m pytest -q` — зелёный (`tests/test_resolver_mapping_diagnostics.py`);
- `ruff check .` — без замечаний;
- smoke-цепочка `build-income-universe → income-universe-audit →
  income-resolver-mapping-diagnostics [--offline]` завершается с кодом 0;
- в `income_resolver_mapping_diagnostics.json`:
  `auto_mapping_allowed_count=0`, `auto_enable_allowed_count=0`;
- в `income_resolver_mapping_diagnostics.md` присутствуют guard-фразы
  («Аналитика, не рекомендация.», «Заявки не отправляются.»,
  `auto_enable_allowed=false`, `auto_mapping_allowed=false`,
  `candidate_for_mapping_review_only`) и нет торговых рекомендаций;
- `data/config` и `data/reports` не попадают в tracked changes.
