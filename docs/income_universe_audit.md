# Income universe disabled audit (read-only)

Диагностический отчёт по **disabled**-кандидатам income universe. Это аналитика,
а не рекомендация: ни один инструмент не включается автоматически.

CLI:

```bash
python main.py income-universe-audit \
  --builder-report data/reports/income_universe_builder_report.json \
  --output-json   data/reports/income_universe_disabled_audit.json \
  --output-md     data/reports/income_universe_disabled_audit.md
```

Все три пути имеют дефолты (показаны выше), поэтому обычно достаточно:

```bash
python main.py income-universe-audit
```

## Контракт (read-only)

Команда:

- читает **только** `income_universe_builder_report.json`;
- **не** читает `data/config/*.yaml`;
- **не** вызывает T-Invest API;
- **не** меняет income policy, target portfolio, income universe builder enable
  logic или resolver;
- **не** включает (auto-enable) ни одного disabled-кандидата — `auto_enable_allowed`
  всегда `false`;
- пишет только `data/reports/*.json` и `*.md`.

Если builder-report отсутствует или повреждён — команда выдаёт понятную ошибку и
просит сначала запустить `python main.py build-income-universe`.

## Группы классификации

Каждый disabled-кандидат попадает в одну из групп. Если кандидат подходит сразу
под несколько условий, действует приоритет:

```
D unresolved → C coupon_validation → E explicit guards → A manual → B estimated → E keep_disabled
```

| Группа | Имя | Когда | Что нужно дальше |
|---|---|---|---|
| A | manual audit | `role=dividend_candidate`, `policy_bucket=income_manual` (напр. SBER) | manual audit / дизайн доверенного источника дохода; local rules сами по себе не меняют bucket |
| B | policy review | `policy_bucket=income_estimated` (напр. NVTK) | отдельное policy-решение |
| C | coupon validation | `role` ∈ {`ofz_pk_candidate`, `bond_candidate`}, либо pending coupon, либо OFZ-PK/`SU29…` | валидация купонного календаря / floating coupon / annualization guard (отдельный PR) |
| D | resolver/mapping | `excluded_reason=unresolved`, пустой `class_code`, либо short-name (напр. ГазКЗ-37Д) | проверенный secid/ISIN/ticker/class_code mapping (resolver/mapping PR или data cleanup) |
| E | keep disabled | `excluded_reason` ∈ {`override_disable`, `trailing_yield_above_cap`, `income_unknown`} (GAZP/LKOH/GMKN) | оставить disabled; cap/override не менять без отдельного review |

## Поля audit-строки

`ticker`, `class_code`, `role`, `policy_bucket`, `excluded_reason`, `notes`,
`audit_group`, `audit_group_name`, `why_disabled`, `required_next_step`,
`requires_code_pr`, `requires_local_rules`, `auto_enable_allowed` (всегда `false`),
`recommendation_guard` (`candidate_for_analysis_only`).

## Summary

`total_disabled`, `group_counts` (A/B/C/D/E), `auto_enable_allowed_count`,
`requires_code_pr_count`, `requires_local_rules_count`, `recommended_next_pr`.
