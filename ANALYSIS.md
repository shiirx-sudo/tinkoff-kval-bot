# Анализ архива MOEX Advisor v0.4.0-alpha21 и план переноса

Документ фиксирует, что из MOEX Advisor переносится в **T-Invest Kval Bot**, что — нет,
и какие архитектурные решения приняты. Новый проект **не является форком** MOEX Advisor
и **не наследует** его акционную стратегию.

## Ключевое архитектурное решение: REST вместо SDK

Read-only коннектор MOEX Advisor (`brokers/tinkoff/client.py`) обращается к
Tinkoff Invest API **напрямую по REST** (`requests` + Bearer-токен,
`https://invest-public-api.tinkoff.ru/rest/...`), без пакета `tinkoff-investments`.

Поэтому новый проект переведён с SDK на тот же REST-подход. Причины:
1. Это прямой перенос рабочего коннектора, как требует задача.
2. Пакет `tinkoff-investments` на момент работы находится в карантине на PyPI —
   `pip install` по нему падает. REST на `requests` снимает эту зависимость.
3. Меньше веса, проще мокать в unit-тестах (мокается HTTP-слой `_post`).

Следствие: операции приходят как JSON-словари (camelCase), а не объекты SDK.
Модули фильтра и подсчёта оборота переписаны под этот контракт (строковые
`operationType`, `Quotation = {units, nano}`).

## Что перенесено (этап 1)

| Из MOEX Advisor | Куда / как |
|---|---|
| `brokers/tinkoff/client.py` (REST, Bearer, `_quotation_to_float`, кэш инструментов) | `brokers/tinkoff/rest_client.py` — read-only REST, новый namespace, **+ GetOperationsByCursor** с пагинацией, **+ обход всех счетов**. Никаких `postOrder`/`cancelOrder`. |
| `brokers/_shared.py`, `brokers/alfa/read_only.py` (helpers) | `common/helpers.py` — `mask_identifier`, `clean_text`, `stable_hash`, `utc_now`, `as_decimal`, `quotation_to_decimal`. |
| `reports/output_contract.py` (стабильный порядок колонок, валидация, метаданные) | `reports/output_contract.py` — та же идея, но на stdlib (`csv`/`json`), без pandas. |
| `broker_sync_status` схема + `connection_status` | `reports/output_contract.py` → отчёт `broker_sync_status.csv`. |
| `runtime_doctor.py`, `validate_existing_reports.py` | `reports/runtime_doctor.py` — проверка окружения/конфигурации и валидация выходных отчётов по контракту. |
| Маскирование account_id, разделение security/cash | Учтено в нормализации счетов (`kval_accounts.csv`). |

Новые отчёты этапа 1 (папка `reports/` на выходе): `kval_progress.json`,
`kval_progress.csv`, `kval_accounts.csv`, `kval_trades.csv`, `broker_sync_status.csv`.

## Что переносится позже (компоненты 3–6) — с маппингом

- **Telegram-форматтер** (`reports/telegram_formatter.py`, `telegram_sent_history.csv`):
  идея группировки сообщений + дедуп по `dedup_key`/`message_hash`. Адаптировать
  под: kval status, monthly/quarterly trade check, turnover target, missing trades,
  paper-cycle alerts. → следующий коммит.
- **Execution gate** (`reports/auto_entry_check.py`): структура «набор статусов →
  финальный статус». Переделать в `execution_gate` со статусами `spread_status`,
  `orderbook_depth_status`, `commission_status`, `turnover_status`,
  `self_trade_risk_status`, `cancel_rate_status`, `daily_limit_status`,
  `final_execution_status`. → коммит на этапе paper-стратегии.
- **Tax / transactions foundation** (`tax/transactions.py`, `tax/lots.py`,
  `tax/storage.py`): переносим идеи — `Decimal` для денег, стабильные `txn_id`
  (sha1 от полей сделки), FIFO-сопоставление лотов, комиссии, `account/source/status`.
  Enum `AssetType` расширить до `fund` / `etf` / `money_market_fund`. → отдельный коммит.

## Что НЕ переносится (явно)

Alfa-коннектор; gifted shares; дивидендный календарь; MOEX equity universe;
старая стратегия Strong Buy / Buy / Sell; Smart-Lab news scanner как сигнал;
full-run логика целиком; старые `data/reports` и `data/manual` как исходные данные.

## Зафиксировано по бэктест-диагностике

Модуль `backtesting/diagnostics.py` MOEX Advisor считает горизонты 1m/3m/6m
(`score_return_corr_3m`, `strong_buy_avg_3m`, `sell_avg_3m` и т.д.). Легаси-`final_signal`
имел **FAIL по 3M-диагностике** и **не переносится как торговая модель**. Здесь он
используется только как референс структуры диагностики, не как сигнал.

## Требования безопасности (соблюдены)

Новый проект без `.env` в репозитории (только `.env.example`); `data/manual` и
`data/reports` не коммитятся (см. `.gitignore`); full-access токен не используется;
`LIVE_ENABLED=false` принудительно; этап 1 — только read-only; реальные API-запросы
отделены от unit-тестов моками HTTP-слоя.
