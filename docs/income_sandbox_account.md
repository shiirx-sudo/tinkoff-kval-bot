# income-sandbox-account (F3.2 — sandbox account bootstrap)

Безопасно получает или создаёт **sandbox** account id и при необходимости
пополняет sandbox-счёт виртуальными деньгами, чтобы разблокировать ручной
F3 one-shot sandbox order (`income-sandbox-execute-preview --send-sandbox`).

Это **отдельный** шаг от отправки заявки. Здесь нет ни одной заявки и ни одной
live-операции.

## Зачем account bootstrap вынесен в отдельную команду

`income-sandbox-execute-preview --send-sandbox` требует `--sandbox-account-id`.
Раньше не было безопасной команды, которая показывает/создаёт sandbox-счёт и
пополняет его. Смешивать «создание счёта» и «отправку заявки» в одной команде
небезопасно: разные действия, разные подтверждения, разный риск. Поэтому
bootstrap (счёт) и order (заявка) разделены.

## Действия

```bash
# 1) status — чистая инспекция, sandbox API НЕ вызывается
python main.py income-sandbox-account --action status --dry-run

# 2) list — read-only список sandbox-счетов (нужен TINKOFF_SANDBOX_TOKEN)
python main.py income-sandbox-account --action list

# 3) open — создать sandbox-счёт (точная фраза подтверждения)
python main.py income-sandbox-account --action open \
  --confirm "CONFIRM SANDBOX ACCOUNT OPEN"

# 4) pay-in — пополнить sandbox-счёт sandbox-деньгами
python main.py income-sandbox-account --action pay-in \
  --sandbox-account-id <ID> --pay-in-rub 100000 \
  --confirm "CONFIRM SANDBOX PAYIN 100000 RUB"
```

| action  | mode                    | сеть      | мутация sandbox | подтверждение |
| ------- | ----------------------- | --------- | --------------- | ------------- |
| status  | `DRY_RUN`               | нет       | нет             | не нужно      |
| list    | `SANDBOX_ACCOUNT_LIST`  | read-only | нет             | не нужно      |
| open    | `SANDBOX_ACCOUNT_OPEN`  | да        | да (sandbox)    | обязательно   |
| pay-in  | `SANDBOX_PAYIN`         | да        | да (sandbox)    | обязательно   |

## Фразы подтверждения

Мутирующие действия (`open`, `pay-in`) выполняются **только** при точном
совпадении `--confirm`:

- open: `CONFIRM SANDBOX ACCOUNT OPEN`
- pay-in: `CONFIRM SANDBOX PAYIN <RUB> RUB` (например `CONFIRM SANDBOX PAYIN 100000 RUB`)

Если фраза отсутствует или не совпадает: никакой sandbox-мутации, код возврата
`1`, в отчёте `required_confirmation_phrase`, `sandbox_account_opened=false`,
`sandbox_payin_done=false`. Фраза подтверждения — это явный человеческий «жест»,
который и разрешает мутацию (в автоматической валидации фраза не передаётся,
поэтому реальные open/pay-in там никогда не выполняются).

## Изоляция токена

- sandbox-токен берётся **только** из отдельного env `TINKOFF_SANDBOX_TOKEN`;
- он нужен только для реальных `list` / `open` / `pay-in` (для `status` не нужен);
- токен **никогда** не печатается и **никогда** не пишется в отчёт;
- полный/read live-токен здесь не читается и не используется;
- `--sandbox-transport unconfigured` блокирует любую реальную sandbox-операцию.

## Никакого live и никаких заявок

- только sandbox account operations: list / open / pay-in;
- нет заявок (ни LIVE, ни sandbox);
- нет live order-endpoint, нет live `Orders`-сервиса, нет full-access live токена,
  нет live account, нет market-заявок, нет autonomous trading;
- портфель и config не меняются; Telegram не используется;
- запись только в `data/reports/income_sandbox_account_report.json` и `.md`.

guards в отчёте: `live_order_sent=false`, `sandbox_order_sent=false`,
`live_orders_service_used=false`, `full_access_live_token_used=false`,
`live_token_used=false`, `token_printed=false`, `portfolio_mutated=false`,
`config_mutated=false`, `telegram_sent=false`, `no_live_execution=true`,
`no_order_execution=true`, `sandbox_token_used=<bool>`.

## Контракт sandbox-методов (proto, не догадка)

Тот же подтверждённый источник, что у F3.1 transport — официальные proto
RussianInvestments/investAPI, пакет `tinkoff.public.invest.api.contract.v1`:

- `sandbox.proto`: `SandboxService.GetSandboxAccounts` (list),
  `OpenSandboxAccount` (open), `SandboxPayIn` (pay-in);
- `users.proto`: `Account` (`id`/`type`/`name`/`status`/`openedDate`/`accessLevel`);
- `common.proto`: `MoneyValue` (`currency`/`units` int64→строка/`nano` int32).

Транспорт — тот же gRPC-over-REST pattern, что у read-only
`brokers/tinkoff/rest_client.py` (base `https://invest-public-api.tinkoff.ru/rest`,
Bearer-токен, JSON в camelCase).

> `list` всегда поддержан официальным методом `GetSandboxAccounts`. Если бы
> точного официального метода не было — он бы не реализовывался, а в отчёте было
> бы честно указано `list unsupported`. Здесь метод подтверждён.

## Как это разблокирует one-shot sandbox test

После `open` (и при необходимости `pay-in`) в отчёте есть
`selected_sandbox_account_id`. Markdown печатает готовую переменную и команду:

```powershell
$sandboxAccountId="<SANDBOX_ACCOUNT_ID_FROM_REPORT>"
python main.py income-sandbox-execute-preview `
  --ticker T --sandbox-transport verified-rest `
  --sandbox-account-id $sandboxAccountId --max-order-rub 1000 `
  --send-sandbox --confirm "CONFIRM SANDBOX BUY T 3 LOTS MAX 1000 RUB"
```

## Почему F4 остаётся заблокированным

F4 (tiny live) допускается только после того, как по порядку выполнено:

1. F3.2 sandbox account существует (эта команда);
2. при необходимости сделан sandbox pay-in;
3. F3.1 verified-rest one-shot sandbox order **вручную** отправлен и зафиксирован
   в отчёте (не в CI/автотестах);
4. одобрен **отдельный** F4 PR.
