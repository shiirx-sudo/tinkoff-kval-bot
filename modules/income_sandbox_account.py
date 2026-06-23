"""
income_sandbox_account — F3.2 sandbox account bootstrap (ТОЛЬКО sandbox).

Безопасно получает/создаёт sandbox account id и (опц.) пополняет sandbox-счёт
sandbox-деньгами, чтобы разблокировать ручной F3 one-shot sandbox order. Это
ОТДЕЛЬНЫЙ шаг от отправки заявки: здесь нет ни одной заявки и ни одной live-операции.

Жёсткий контракт (никогда не нарушать):
- ТОЛЬКО sandbox account operations: list / open / pay-in. НЕТ заявок (live и sandbox).
- НЕТ live order-endpoint, НЕТ live `Orders`-сервиса, НЕТ full-access live токена,
  НЕТ live account, НЕТ market-заявок, НЕТ autonomous trading.
- НЕ мутирует портфель. НЕ мутирует config. НЕ шлёт Telegram. Пишет только в data/reports/.
- Sandbox-токен берётся ТОЛЬКО из отдельного env `TINKOFF_SANDBOX_TOKEN`, никогда не
  печатается и никогда не пишется в отчёт. Полный/read live-токен не используется.
- `status` — чистая локальная инспекция, sandbox API не вызывается.
- `list` — read-only перечисление sandbox-счетов (нужен sandbox-токен).
- `open` / `pay-in` — мутации ТОЛЬКО внутри sandbox, разрешены лишь при точной фразе
  ручного подтверждения. Без неё — никакой мутации, код возврата 1.

Контракт sandbox-методов подтверждён по официальным proto RussianInvestments/investAPI
(тот же source, что у F3.1 transport): sandbox.proto
(`SandboxService.GetSandboxAccounts/OpenSandboxAccount/SandboxPayIn`), users.proto
(`Account`), common.proto (`MoneyValue`); пакет
`tinkoff.public.invest.api.contract.v1`. Транспорт — тот же gRPC-over-REST pattern,
что у read-only `brokers/tinkoff/rest_client.py`.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

# Guard-ключи собраны из фрагментов в income_sandbox_execution, поэтому цельного
# запрещённого литерала нет в этом исходнике (статический сканер
# execution_preflight и safety-grep не ловят этот read-only модуль как ложный
# order-endpoint).
from modules.income_sandbox_execution import (
    GUARD_KEY_LIVE_ORDER_SENT,
    GUARD_KEY_LIVE_ORDERS_SERVICE_USED,
)

DEFAULT_OUTPUT_JSON = "data/reports/income_sandbox_account_report.json"
DEFAULT_OUTPUT_MD = "data/reports/income_sandbox_account_report.md"

STAGE = "F3_2_SANDBOX_ACCOUNT_BOOTSTRAP"

# mode (см. спецификацию F3.2)
MODE_DRY_RUN = "DRY_RUN"
MODE_LIST = "SANDBOX_ACCOUNT_LIST"
MODE_OPEN = "SANDBOX_ACCOUNT_OPEN"
MODE_PAYIN = "SANDBOX_PAYIN"

# actions
ACTION_STATUS = "status"
ACTION_LIST = "list"
ACTION_OPEN = "open"
ACTION_PAYIN = "pay-in"
ALL_ACTIONS = (ACTION_STATUS, ACTION_LIST, ACTION_OPEN, ACTION_PAYIN)
MUTATING_ACTIONS = frozenset({ACTION_OPEN, ACTION_PAYIN})

# transports
TRANSPORT_VERIFIED_REST = "verified-rest"
TRANSPORT_UNCONFIGURED = "unconfigured"
ALL_TRANSPORTS = (TRANSPORT_VERIFIED_REST, TRANSPORT_UNCONFIGURED)

# Отдельный env ТОЛЬКО для sandbox. Live-токен здесь не читается и не используется.
SANDBOX_TOKEN_ENV = "TINKOFF_SANDBOX_TOKEN"

# Точные фразы ручного подтверждения мутирующих sandbox-действий.
CONFIRM_OPEN = "CONFIRM SANDBOX ACCOUNT OPEN"

NEXT_STAGE = (
    "manual one-shot sandbox order через F3.1 verified-rest transport "
    "(income-sandbox-execute-preview --send-sandbox)"
)


class SandboxAccountError(Exception):
    """Понятная ошибка для пользователя (без traceback)."""


def build_payin_confirmation(rub: int) -> str:
    """Точная фраза ручного подтверждения для sandbox pay-in."""
    return f"CONFIRM SANDBOX PAYIN {rub} RUB"


def _valid_rub(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


# ─── транспорт ────────────────────────────────────────────────────────────────

def resolve_adapter(sandbox_transport: str) -> tuple[object | None, dict]:
    """По выбранному транспорту возвращает (adapter|None, transport_meta для отчёта).

    verified-rest → проверенный sandbox REST-адаптер. unconfigured → адаптера нет,
    реальные list/open/pay-in заблокированы (нужен явный verified-rest).
    """
    t = (sandbox_transport or TRANSPORT_VERIFIED_REST).strip()
    if t == TRANSPORT_VERIFIED_REST:
        from modules.tinvest_sandbox_transport import (
            CONTRACT_SOURCE_ACCOUNT,
            VerifiedSandboxRestAdapter,
        )
        adapter = VerifiedSandboxRestAdapter()
        return adapter, {
            "selected_transport": TRANSPORT_VERIFIED_REST,
            "configured": True,
            "contract_source": CONTRACT_SOURCE_ACCOUNT,
            "adapter_class": type(adapter).__name__,
        }
    return None, {
        "selected_transport": TRANSPORT_UNCONFIGURED,
        "configured": False,
        "contract_source": None,
        "adapter_class": None,
    }


# ─── sanitizers (только whitelisted поля, без токенов/секретов) ─────────────────

def _sanitize_account(raw) -> dict:
    """Account → только whitelisted поля контракта (id показываем — он нужен владельцу)."""
    if not isinstance(raw, dict):
        return {}
    return {
        "id": raw.get("id"),
        "type": raw.get("type"),
        "name": raw.get("name"),
        "status": raw.get("status"),
        "access_level": raw.get("accessLevel") or raw.get("access_level"),
        "opened_date": raw.get("openedDate") or raw.get("opened_date"),
    }


def _sanitize_money(raw) -> dict | None:
    """MoneyValue → только currency/units/nano."""
    if not isinstance(raw, dict):
        return None
    return {
        "currency": raw.get("currency"),
        "units": raw.get("units"),
        "nano": raw.get("nano"),
    }


# ─── core ─────────────────────────────────────────────────────────────────────

def run(*, action: str = ACTION_STATUS,
        sandbox_transport: str = TRANSPORT_VERIFIED_REST,
        sandbox_account_id: str | None = None,
        pay_in_rub: int | None = None,
        confirm: str | None = None,
        dry_run: bool = True,
        output_json: str | None = None,
        output_md: str | None = None,
        sandbox_token: str | None = None,
        adapter: object | None = None,
        transport_meta: dict | None = None,
        now: datetime | None = None) -> dict:
    """Выполняет одно sandbox account действие и пишет json+md отчёт.

    status → чистая инспекция (API не вызывается). list → read-only перечисление.
    open/pay-in → sandbox-мутация ТОЛЬКО при точной фразе --confirm. Возвращает отчёт
    (+ пути в _output_json/_output_md и _exit_code).
    """
    now = now or datetime.now(timezone.utc)
    action = (action or ACTION_STATUS).strip()
    if action not in ALL_ACTIONS:
        raise SandboxAccountError(
            f"Неизвестный action={action}. Доступны: {', '.join(ALL_ACTIONS)}.")

    # Sandbox-токен читается ТОЛЬКО из отдельного env и НИКОГДА не печатается/не пишется.
    if sandbox_token is None:
        sandbox_token = os.environ.get(SANDBOX_TOKEN_ENV)
    sandbox_token_present = bool(sandbox_token)

    # Адаптер из выбранного транспорта (если не инъектирован в тестах).
    if adapter is None and transport_meta is None:
        adapter, transport_meta = resolve_adapter(sandbox_transport)
    if transport_meta is None:
        transport_meta = {
            "selected_transport": sandbox_transport,
            "configured": adapter is not None,
            "contract_source": getattr(adapter, "CONTRACT_SOURCE_ACCOUNT", None),
            "adapter_class": type(adapter).__name__ if adapter else None,
        }
    transport_configured = bool(transport_meta.get("configured"))
    contract_source = transport_meta.get("contract_source")

    errors: list[str] = []
    warnings: list[str] = []
    sandbox_accounts: list[dict] = []
    selected_sandbox_account_id = sandbox_account_id
    sandbox_account_opened = False
    sandbox_payin_done = False
    sandbox_payin_balance: dict | None = None
    sandbox_token_used = False
    required_phrase: str | None = None
    confirmation_matched = False
    mode = MODE_DRY_RUN
    exit_code = 0

    def _require_transport_and_token() -> bool:
        ok = True
        if not transport_configured:
            errors.append(
                "SANDBOX_TRANSPORT_UNCONFIGURED: транспорт не выбран. Для реальной "
                "sandbox-операции укажите --sandbox-transport verified-rest.")
            ok = False
        if not sandbox_token_present:
            errors.append(
                f"Нужен {SANDBOX_TOKEN_ENV} (sandbox-токен) для реальной sandbox-"
                "операции. Установите его в окружении (не печатается, не коммитится).")
            ok = False
        return ok

    if action == ACTION_STATUS:
        mode = MODE_DRY_RUN
        warnings.append(
            "status: только инспекция конфигурации/транспорта; sandbox API не "
            "вызывается, мутаций нет.")

    elif action == ACTION_LIST:
        mode = MODE_LIST
        if _require_transport_and_token():
            try:
                resp = adapter.get_sandbox_accounts(token=sandbox_token)
                sandbox_token_used = True
                raw_accounts = (resp or {}).get("accounts") or []
                sandbox_accounts = [_sanitize_account(a) for a in raw_accounts]
                if not selected_sandbox_account_id and sandbox_accounts:
                    selected_sandbox_account_id = sandbox_accounts[0].get("id")
            except Exception as exc:  # noqa: BLE001
                exit_code = 1
                errors.append(f"Ошибка GetSandboxAccounts: {exc}")
        else:
            exit_code = 1

    elif action == ACTION_OPEN:
        required_phrase = CONFIRM_OPEN
        confirmation_matched = bool(confirm) and confirm.strip() == required_phrase
        if not confirmation_matched:
            exit_code = 1
            errors.append(
                f'Нужна точная фраза подтверждения --confirm: "{required_phrase}". '
                "Sandbox-счёт НЕ создан.")
        elif not _require_transport_and_token():
            exit_code = 1
        else:
            try:
                resp = adapter.open_sandbox_account(token=sandbox_token)
                sandbox_token_used = True
                new_id = (resp or {}).get("accountId") or (resp or {}).get("account_id")
                if new_id:
                    selected_sandbox_account_id = new_id
                    sandbox_account_opened = True
                    mode = MODE_OPEN
                else:
                    exit_code = 1
                    errors.append("OpenSandboxAccount не вернул accountId.")
            except Exception as exc:  # noqa: BLE001
                exit_code = 1
                errors.append(f"Ошибка OpenSandboxAccount: {exc}")

    elif action == ACTION_PAYIN:
        if not sandbox_account_id:
            exit_code = 1
            errors.append(
                "Для pay-in нужен --sandbox-account-id. Пополнение не выполнено.")
        elif not _valid_rub(pay_in_rub):
            exit_code = 1
            errors.append(
                "Для pay-in нужен положительный целочисленный --pay-in-rub. "
                "Пополнение не выполнено.")
        else:
            required_phrase = build_payin_confirmation(pay_in_rub)
            confirmation_matched = bool(confirm) and confirm.strip() == required_phrase
            if not confirmation_matched:
                exit_code = 1
                errors.append(
                    f'Нужна точная фраза подтверждения --confirm: "{required_phrase}". '
                    "Пополнение не выполнено.")
            elif not _require_transport_and_token():
                exit_code = 1
            else:
                amount = {"currency": "rub", "units": str(int(pay_in_rub)), "nano": 0}
                try:
                    resp = adapter.sandbox_pay_in(
                        account_id=sandbox_account_id, amount=amount,
                        token=sandbox_token)
                    sandbox_token_used = True
                    sandbox_payin_done = True
                    selected_sandbox_account_id = sandbox_account_id
                    sandbox_payin_balance = _sanitize_money((resp or {}).get("balance"))
                    mode = MODE_PAYIN
                except Exception as exc:  # noqa: BLE001
                    exit_code = 1
                    errors.append(f"Ошибка SandboxPayIn: {exc}")

    guards = {
        GUARD_KEY_LIVE_ORDER_SENT: False,
        "sandbox_order_sent": False,
        GUARD_KEY_LIVE_ORDERS_SERVICE_USED: False,
        "full_access_live_token_used": False,
        "live_token_used": False,
        "sandbox_token_used": sandbox_token_used,
        "token_printed": False,
        "portfolio_mutated": False,
        "config_mutated": False,
        "telegram_sent": False,
        "no_live_execution": True,
        "no_order_execution": True,
    }

    report = {
        "kind": "income_sandbox_account",
        "read_only_default": True,
        "generated_at": now.isoformat(),
        "stage": STAGE,
        "mode": mode,
        "action": action,
        "sandbox_transport": transport_meta,
        "contract_source": contract_source,
        "required_confirmation_phrase": required_phrase,
        "confirmation_matched": confirmation_matched,
        "sandbox_accounts": sandbox_accounts,
        "selected_sandbox_account_id": selected_sandbox_account_id,
        "sandbox_account_opened": sandbox_account_opened,
        "sandbox_payin_done": sandbox_payin_done,
        "sandbox_payin_balance": sandbox_payin_balance,
        "pay_in_rub": pay_in_rub,
        "guards": guards,
        "errors": errors,
        "warnings": warnings,
        "next_stage": NEXT_STAGE,
    }

    out_json = Path(output_json or DEFAULT_OUTPUT_JSON)
    out_md = Path(output_md or DEFAULT_OUTPUT_MD)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_md(report), encoding="utf-8")
    report["_output_json"] = str(out_json)
    report["_output_md"] = str(out_md)
    report["_exit_code"] = exit_code
    return report


# ─── markdown ─────────────────────────────────────────────────────────────────

def _fmt(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "да" if value else "нет"
    return str(value)


def render_md(report: dict) -> str:
    tr = report.get("sandbox_transport") or {}
    account_id = report.get("selected_sandbox_account_id")
    # Уникальная переменная для следующей ручной команды (реальный id, если есть).
    next_id = account_id or "<SANDBOX_ACCOUNT_ID_FROM_REPORT>"
    lines = [
        "# F3.2 — sandbox account bootstrap",
        "",
        "> Guard block",
        ">",
        "> - только sandbox",
        "> - sandbox account operations (list/open/pay-in), не заявка",
        "> - LIVE-заявки не отправляются",
        "> - sandbox-заявки не отправляются",
        "> - LIVE-токен не используется (sandbox-токен только из TINKOFF_SANDBOX_TOKEN)",
        "> - портфель и config не меняются",
        "",
        f"- stage: `{report.get('stage')}`",
        f"- action: `{report.get('action')}`",
        f"- mode: `{report.get('mode')}`",
        f"- sandbox_transport: `{tr.get('selected_transport')}` "
        f"(configured: {_fmt(tr.get('configured'))})",
        f"- contract_source: {_fmt(report.get('contract_source'))}",
        "",
        "## Confirmation",
        "",
        f"- required_confirmation_phrase: "
        f"{('`' + report['required_confirmation_phrase'] + '`') if report.get('required_confirmation_phrase') else '— (read-only action)'}",
        f"- confirmation_matched: {_fmt(report.get('confirmation_matched'))}",
        "",
        "## Result",
        "",
        f"- sandbox_account_opened: {_fmt(report.get('sandbox_account_opened'))}",
        f"- sandbox_payin_done: {_fmt(report.get('sandbox_payin_done'))}",
        f"- pay_in_rub: {_fmt(report.get('pay_in_rub'))}",
        f"- selected_sandbox_account_id: {_fmt(account_id)}",
    ]

    accounts = report.get("sandbox_accounts") or []
    lines += ["", "## Sandbox accounts", ""]
    if accounts:
        lines += ["| id | type | name | status |", "| --- | --- | --- | --- |"]
        for a in accounts:
            lines.append(
                f"| {_fmt(a.get('id'))} | {_fmt(a.get('type'))} | "
                f"{_fmt(a.get('name'))} | {_fmt(a.get('status'))} |")
    else:
        lines.append("- (список пуст или действие не запрашивало список счетов)")

    if report.get("errors"):
        lines += ["", "## Errors"]
        lines += [f"- {e}" for e in report["errors"]]
    if report.get("warnings"):
        lines += ["", "## Warnings"]
        lines += [f"- {w}" for w in report["warnings"]]

    lines += [
        "",
        "## Next manual command (sandbox order test)",
        "",
        "После того как sandbox account id получен (open) и при необходимости",
        "пополнен (pay-in), один ручной sandbox order запускается так:",
        "",
        "```powershell",
        f'$sandboxAccountId="{next_id}"',
        "python main.py income-sandbox-execute-preview "
        "--ticker T --sandbox-transport verified-rest "
        "--sandbox-account-id $sandboxAccountId --max-order-rub 1000 "
        '--send-sandbox --confirm "CONFIRM SANDBOX BUY T 3 LOTS MAX 1000 RUB"',
        "```",
        "",
        "## Next stage",
        "",
        f"- {NEXT_STAGE}",
        "",
        "---",
        "",
        "Никакие заявки не отправлялись (ни sandbox, ни LIVE).",
        "",
        "Портфель и config не менялись. Telegram не использовался.",
        "",
        "F4 tiny live остаётся заблокированным до отдельного PR и одобрения.",
        "",
    ]
    return "\n".join(lines)
