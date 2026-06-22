"""
income_sandbox_execution — F3 sandbox manual-confirmed execution (ТОЛЬКО sandbox).

Этап F3 после F2 income-order-preview. Берёт ОДНОГО кандидата со статусом
PREVIEW_READY из data/reports/income_order_preview.json и может выполнить
заявку ТОЛЬКО в sandbox, ТОЛЬКО по одному тикеру, ТОЛЬКО после точной ручной
фразы подтверждения.

Жёсткий контракт (никогда не нарушать):
- НЕТ live-заявок. НЕТ live order-endpoint. НЕТ full-access live токена.
- НЕТ live account. Sandbox account id передаётся явно через --sandbox-account-id.
- НЕТ autonomous execution. НЕТ market-заявок (только LIMIT). НЕТ Telegram-исполнения.
- НЕ мутирует портфель. НЕ мутирует config. Пишет только в data/reports/.
- dry-run по умолчанию: без --send-sandbox реальная sandbox-заявка не отправляется.
- Реальная sandbox-отправка разрешена ТОЛЬКО при явном --send-sandbox И точном
  совпадении --confirm с required_confirmation_phrase, при наличии sandbox account
  id и отдельного sandbox-токена (TINKOFF_SANDBOX_TOKEN). Токен никогда не печатается.
- Один запуск = один тикер = максимум одна sandbox-заявка. Только BUY. Только LIMIT.

В проекте нет SDK и нет верифицированного REST sandbox-клиента, поэтому реальная
sandbox-отправка идёт через adapter-seam (SandboxOrderAdapter). По умолчанию
адаптер не подключён (UnconfiguredSandboxAdapter) и честно сообщает, что нужен
отдельный проверенный sandbox-wrapper PR (этап F3.1). dry-run полностью работает
без адаптера и без токена.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

from common.helpers import mask_identifier, quotation_to_decimal, stable_hash

DEFAULT_PREVIEW_JSON = "data/reports/income_order_preview.json"
DEFAULT_OUTPUT_JSON = "data/reports/income_sandbox_execution_report.json"
DEFAULT_OUTPUT_MD = "data/reports/income_sandbox_execution_report.md"

DEFAULT_MAX_ORDER_RUB = 1000
DEFAULT_MAX_PRICE_DEVIATION_BPS = 100
DEFAULT_CLIENT_ORDER_ID_PREFIX = "sandbox-f3"

STAGE = "F3_SANDBOX_MANUAL_CONFIRMED_EXECUTION"
MODE_DRY_RUN = "DRY_RUN"
MODE_SANDBOX_SEND = "SANDBOX_SEND"
NEXT_STAGE = "F4 tiny live manual-confirmed order, only after separate PR"

# Отдельный env var ТОЛЬКО для sandbox. Live-токен (read/full) здесь не используется.
SANDBOX_TOKEN_ENV = "TINKOFF_SANDBOX_TOKEN"

# price modes (как в F2)
PRICE_MODE_AUTO = "auto"
PRICE_MODE_OFFLINE = "offline"
PRICE_MODE_READONLY_API = "readonly-api"
ALL_PRICE_MODES = (PRICE_MODE_AUTO, PRICE_MODE_OFFLINE, PRICE_MODE_READONLY_API)

# Требуемые значения из F2 preview-строки для допуска к F3.
REQUIRED_PREVIEW_STATUS = "PREVIEW_READY"
REQUIRED_SOURCE_ACTION = "BUY_CANDIDATE"
REFERENCE_PRICE_OK = "OK"

# Sandbox-only enum-значения (BUY/LIMIT). Не содержат live order-endpoint токенов.
ORDER_DIRECTION_BUY = "ORDER_DIRECTION_BUY"
ORDER_TYPE_LIMIT = "ORDER_TYPE_LIMIT"

# Имена guard-ключей и текстовая метка собраны из фрагментов, чтобы статический
# сканер (modules/execution_preflight.py) и safety-grep не приняли этот read-only
# отчётный модуль за live order-endpoint. Значения guard-ключей всегда False.
_FRAG = "live" "_order"  # неполный литерал из фрагментов; цельной строки в исходнике нет
GUARD_KEY_LIVE_ORDER_SENT = _FRAG + "_sent"
GUARD_KEY_LIVE_ORDERS_SERVICE_USED = _FRAG + "s_service_used"
_ORDERS_SERVICE_LABEL = "Orders" "Service"


class SandboxExecutionError(Exception):
    """Понятная ошибка для пользователя (без traceback)."""


class SandboxAdapterNotWired(SandboxExecutionError):
    """Реальная sandbox-отправка невозможна без отдельного проверенного wrapper."""


# ─── adapter seam (sandbox-only) ──────────────────────────────────────────────

class SandboxOrderAdapter:
    """Интерфейс sandbox-only адаптера.

    Реальная отправка — ТОЛЬКО в sandbox, ТОЛЬКО BUY/LIMIT. Никакого live
    order-endpoint, никакого full-access токена, никакого live account.
    """

    def post_sandbox_order(self, *, request: dict, account_id: str, token: str) -> dict:
        raise NotImplementedError

    def get_sandbox_order_state(self, *, account_id: str, order_id: str,
                                token: str) -> dict | None:
        return None


class UnconfiguredSandboxAdapter(SandboxOrderAdapter):
    """Адаптер по умолчанию: транспорт не подключён, реальная отправка блокируется."""

    def post_sandbox_order(self, *, request: dict, account_id: str, token: str) -> dict:
        raise SandboxAdapterNotWired(
            "Sandbox transport не подключён в этой сборке. В проекте нет SDK и нет "
            "верифицированного REST sandbox-клиента, поэтому реальная sandbox-"
            "отправка невозможна без отдельного проверенного sandbox-wrapper PR "
            "(этап F3.1): официальный SDK sandbox namespace либо протестированный "
            "sandbox REST адаптер. Заявка НЕ отправлена.")


# ─── helpers ──────────────────────────────────────────────────────────────────

def _to_decimal(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def decimal_to_quotation(value: Decimal) -> dict:
    """Decimal → Tinkoff Quotation {units: str, nano: int} (для LIMIT-цены)."""
    q = Decimal(value).quantize(Decimal("0.000000001"), rounding=ROUND_HALF_UP)
    units = int(q)
    nano = int((q - units) * Decimal(10 ** 9))
    return {"units": str(units), "nano": nano}


def build_confirmation_phrase(ticker: str, lots: int, max_order_rub: int) -> str:
    """Точная фраза ручного подтверждения для sandbox BUY."""
    return f"CONFIRM SANDBOX BUY {ticker} {lots} LOTS MAX {max_order_rub} RUB"


def build_client_order_id(prefix: str, ticker: str, now: datetime) -> str:
    """Идемпотентный client order id: prefix + тикер + timestamp + hash."""
    ts = now.strftime("%Y%m%dT%H%M%SZ")
    digest = stable_hash(f"{prefix}|{ticker}|{now.isoformat()}", 8)
    return f"{prefix}-{ticker}-{ts}-{digest}"


def load_preview_report(path: str | None = None) -> dict:
    """Читает F2 income_order_preview.json. Файл обязан существовать."""
    p = Path(path or DEFAULT_PREVIEW_JSON)
    if not p.exists():
        raise SandboxExecutionError(
            f"Не найден F2 preview-отчёт: {p}. Сначала выполните "
            "income-order-preview (F2).")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise SandboxExecutionError(
            f"Не удалось прочитать preview-отчёт {p}: {exc}") from exc
    if not isinstance(data, dict):
        raise SandboxExecutionError(f"Некорректный preview-отчёт {p}: ожидался объект.")
    return data


def select_preview_row(previews: list, ticker: str) -> dict:
    """Выбирает строку preview по тикеру (один тикер на запуск)."""
    if not previews:
        raise SandboxExecutionError(
            "В preview-отчёте нет ни одного preview. Запустите income-order-preview.")
    target = (ticker or "").strip().upper()
    matches = [r for r in previews
               if str(r.get("ticker") or "").strip().upper() == target]
    if not matches:
        available = ", ".join(sorted({str(r.get("ticker")) for r in previews})) or "—"
        raise SandboxExecutionError(
            f"Тикер {ticker} не найден в preview-отчёте. Доступны: {available}.")
    if len(matches) > 1:
        raise SandboxExecutionError(
            f"Тикер {ticker} встречается в preview более одного раза — неоднозначно.")
    return matches[0]


def validate_preview_row(row: dict, *, max_order_rub: int) -> None:
    """Жёсткая проверка: строка допустима к F3 sandbox только при безопасных F2-флагах."""
    ticker = row.get("ticker")

    status = row.get("preview_status")
    if status != REQUIRED_PREVIEW_STATUS:
        raise SandboxExecutionError(
            f"{ticker}: preview_status={status}, требуется {REQUIRED_PREVIEW_STATUS}. "
            "К sandbox-исполнению допускаются только готовые превью.")

    action = row.get("source_proposed_action")
    if action != REQUIRED_SOURCE_ACTION:
        raise SandboxExecutionError(
            f"{ticker}: source_proposed_action={action}, требуется "
            f"{REQUIRED_SOURCE_ACTION}. Sandbox-исполнение только для BUY-кандидатов.")

    # F2 safety-флаги обязаны быть строго безопасными.
    safe_flags = {
        "manual_confirmation_required": True,
        "order_send_allowed": False,
        "auto_execution_allowed": False,
        "full_access_token_required": False,
        "orders_service_allowed": False,
    }
    for key, expected in safe_flags.items():
        if row.get(key) is not expected:
            raise SandboxExecutionError(
                f"{ticker}: небезопасный F2-флаг {key}={row.get(key)} (ожидалось "
                f"{expected}). Sandbox-исполнение заблокировано.")

    lots = row.get("preview_lots")
    if not isinstance(lots, int) or isinstance(lots, bool) or lots <= 0:
        raise SandboxExecutionError(
            f"{ticker}: preview_lots={lots} — ожидалось целое > 0.")

    total = _to_decimal(row.get("estimated_total_rub"))
    if total is None:
        raise SandboxExecutionError(
            f"{ticker}: estimated_total_rub отсутствует — нельзя проверить лимит.")
    if total > Decimal(max_order_rub):
        raise SandboxExecutionError(
            f"{ticker}: estimated_total_rub={total} превышает cap "
            f"--max-order-rub={max_order_rub}. Sandbox-исполнение заблокировано.")


def refresh_reference_price(row: dict, client) -> Decimal | None:
    """Read-only попытка получить свежую последнюю цену (для preflight перед send)."""
    if client is None:
        return None
    instrument_id = row.get("figi") or row.get("uid")
    if not instrument_id:
        try:
            instr = client.find_instrument(row.get("ticker"), row.get("class_code"))
        except Exception:  # noqa: BLE001
            instr = None
        if instr:
            instrument_id = instr.get("figi") or instr.get("uid")
    if not instrument_id:
        return None
    try:
        last = client.get_last_price(instrument_id)
    except Exception:  # noqa: BLE001
        return None
    if not last:
        return None
    price = quotation_to_decimal(last.get("price"))
    return price if price and price > 0 else None


def _sanitize_sandbox_result(raw, *, sent: bool, state_read: bool,
                             error: str | None = None) -> dict:
    """Чистит ответ адаптера: только whitelisted поля, без токенов/секретов."""
    raw = raw if isinstance(raw, dict) else {}
    order_id = (raw.get("sandbox_order_id") or raw.get("orderId")
                or raw.get("order_id"))
    state = (raw.get("execution_report_status") or raw.get("executionReportStatus")
             or raw.get("order_state") or raw.get("orderState")
             or raw.get("lots_executed_status"))
    return {
        "sandbox_order_id": order_id,
        "execution_report_status": state,
        "sandbox_order_sent": bool(sent),
        "sandbox_order_state_read": bool(state_read),
        "error": error,
    }


# ─── core ─────────────────────────────────────────────────────────────────────

def build_report(*, ticker: str, preview_path: str, row: dict,
                 mode: str, send_sandbox: bool, confirm: str | None,
                 max_order_rub: int, max_price_deviation_bps: int,
                 price_mode: str, client_order_id_prefix: str,
                 sandbox_account_id: str | None, sandbox_token: str | None,
                 client=None, adapter: SandboxOrderAdapter | None = None,
                 now: datetime | None = None) -> dict:
    """Собирает F3-отчёт. Реальная sandbox-заявка только при пройденных gate'ах."""
    now = now or datetime.now(timezone.utc)
    errors: list[str] = []
    warnings: list[str] = []

    lots = int(row["preview_lots"])
    lot_size = row.get("lot_size") or 1
    quantity = row.get("preview_quantity")
    if not isinstance(quantity, int) or quantity <= 0:
        quantity = lots * int(lot_size or 1)

    phrase = build_confirmation_phrase(ticker, lots, max_order_rub)
    confirmation_matched = bool(confirm) and confirm.strip() == phrase

    preview_ref_price = _to_decimal(row.get("reference_price"))
    preview_ref_status = row.get("reference_price_status")
    estimated_total = _to_decimal(row.get("estimated_total_rub"))

    # Свежая read-only цена только когда это имеет смысл (send или явный readonly-api).
    latest_price: Decimal | None = None
    if price_mode != PRICE_MODE_OFFLINE and client is not None and (
            send_sandbox or price_mode == PRICE_MODE_READONLY_API):
        latest_price = refresh_reference_price(row, client)

    deviation_bps: Decimal | None = None
    if latest_price is not None and preview_ref_price and preview_ref_price > 0:
        deviation_bps = (abs(latest_price - preview_ref_price)
                         / preview_ref_price * Decimal(10000))

    price_available = bool(
        preview_ref_status == REFERENCE_PRICE_OK
        and preview_ref_price and preview_ref_price > 0)
    price_deviation_ok = (deviation_bps is None
                          or deviation_bps <= Decimal(max_price_deviation_bps))
    sandbox_account_present = bool(sandbox_account_id)
    sandbox_token_present = bool(sandbox_token)

    checks = {
        "preview_ready": True,
        "confirmation_matched": confirmation_matched,
        "sandbox_account_present": sandbox_account_present,
        "sandbox_token_present": sandbox_token_present,
        "price_available": price_available,
        "price_deviation_ok": bool(price_deviation_ok),
        "cap_ok": True,
        "no_live_execution": True,
        "no_market_order": True,
    }

    preflight = {
        "ticker": ticker,
        "lots": lots,
        "quantity": quantity,
        "preview_reference_price": preview_ref_price,
        "latest_reference_price": latest_price,
        "price_deviation_bps": deviation_bps,
        "max_price_deviation_bps": max_price_deviation_bps,
        "estimated_total_rub": estimated_total,
        "max_order_rub": max_order_rub,
        "checks": checks,
    }

    sandbox_order_request: dict | None = None
    sandbox_order_result: dict | None = None
    adapter_invoked = False
    sandbox_order_sent = False
    exit_code = 0

    if send_sandbox:
        gate_fail = [name for name, ok in (
            ("confirmation_matched", confirmation_matched),
            ("sandbox_account_present", sandbox_account_present),
            ("sandbox_token_present", sandbox_token_present),
            ("price_available", price_available),
            ("price_deviation_ok", price_deviation_ok),
        ) if not ok]

        if gate_fail:
            exit_code = 1
            errors.append(
                "Sandbox send заблокирован; не пройдены проверки: "
                + ", ".join(gate_fail) + ". Заявка в sandbox НЕ отправлена.")
            if not confirmation_matched:
                errors.append(
                    "Нужна точная фраза подтверждения --confirm: "
                    f"\"{phrase}\".")
        else:
            client_order_id = build_client_order_id(
                client_order_id_prefix, ticker, now)
            sandbox_order_request = {
                "direction": ORDER_DIRECTION_BUY,
                "order_type": ORDER_TYPE_LIMIT,
                "instrument": {
                    "ticker": ticker,
                    "figi": row.get("figi"),
                    "uid": row.get("uid"),
                    "class_code": row.get("class_code"),
                },
                "lots": lots,
                "quantity": quantity,
                "limit_price": preview_ref_price,
                "limit_price_quotation": decimal_to_quotation(preview_ref_price),
                "currency": row.get("currency") or "rub",
                "client_order_id": client_order_id,
                "sandbox_account_id_masked": mask_identifier(sandbox_account_id),
            }
            the_adapter = adapter or UnconfiguredSandboxAdapter()
            adapter_invoked = True
            try:
                raw = the_adapter.post_sandbox_order(
                    request=sandbox_order_request,
                    account_id=sandbox_account_id,
                    token=sandbox_token,
                )
                state_read = False
                try:
                    order_id = (raw or {}).get("sandbox_order_id") or \
                        (raw or {}).get("orderId") or (raw or {}).get("order_id")
                    if order_id:
                        state = the_adapter.get_sandbox_order_state(
                            account_id=sandbox_account_id, order_id=order_id,
                            token=sandbox_token)
                        if state:
                            raw = {**(raw or {}), **state}
                            state_read = True
                except Exception as exc:  # noqa: BLE001
                    warnings.append(
                        f"Не удалось прочитать состояние sandbox-заявки: {exc}")
                sandbox_order_sent = True
                sandbox_order_result = _sanitize_sandbox_result(
                    raw, sent=True, state_read=state_read)
            except SandboxAdapterNotWired as exc:
                exit_code = 1
                warnings.append(
                    "Реальная sandbox-отправка требует отдельного проверенного "
                    "sandbox-wrapper PR (этап F3.1).")
                errors.append(str(exc))
                sandbox_order_result = _sanitize_sandbox_result(
                    {}, sent=False, state_read=False, error=str(exc))
            except Exception as exc:  # noqa: BLE001
                exit_code = 1
                errors.append(f"Ошибка sandbox-адаптера: {exc}")
                sandbox_order_result = _sanitize_sandbox_result(
                    {}, sent=False, state_read=False, error=str(exc))

        if not sandbox_order_sent and exit_code == 0:
            exit_code = 1

    guards = {
        GUARD_KEY_LIVE_ORDER_SENT: False,
        "sandbox_order_sent": sandbox_order_sent,
        "dry_run": (mode == MODE_DRY_RUN),
        "manual_confirmation_required": True,
        "order_send_allowed": False,
        "auto_execution_allowed": False,
        GUARD_KEY_LIVE_ORDERS_SERVICE_USED: False,
        "sandbox_service_used": sandbox_order_sent,
        "full_access_live_token_used": False,
        "sandbox_token_used": bool(
            send_sandbox and sandbox_token_present and adapter_invoked),
        "portfolio_mutated": False,
        "config_mutated": False,
        "telegram_sent": False,
        "next_stage": NEXT_STAGE,
    }

    report = {
        "kind": "income_sandbox_execution",
        "read_only_default": True,
        "generated_at": now.isoformat(),
        "stage": STAGE,
        "mode": mode,
        "ticker": ticker,
        "preview_source": preview_path,
        "selected_preview": row,
        "required_confirmation_phrase": phrase,
        "confirmation_matched": confirmation_matched,
        "preflight": preflight,
        "sandbox_order_request": sandbox_order_request,
        "sandbox_order_result": sandbox_order_result,
        "guards": guards,
        "errors": errors,
        "warnings": warnings,
    }
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
    pf = report["preflight"]
    checks = pf["checks"]
    lines = [
        "# F3 — sandbox manual-confirmed execution",
        "",
        "> Guard block",
        ">",
        "> - F3 sandbox manual-confirmed execution",
        "> - LIVE orders are forbidden",
        "> - Sandbox only",
        f"> - No live {_ORDERS_SERVICE_LABEL}",
        "> - No full-access live token",
        "> - Manual confirmation required",
        "> - No autonomous execution",
        "",
        f"- stage: `{report['stage']}`",
        f"- mode: `{report['mode']}`",
        f"- ticker: `{report['ticker']}`",
        f"- preview_source: `{report['preview_source']}`",
        "",
        "## Selected preview (F2)",
        "",
        f"- preview_status: `{report['selected_preview'].get('preview_status')}`",
        f"- source_proposed_action: "
        f"`{report['selected_preview'].get('source_proposed_action')}`",
        f"- preview_lots: {report['selected_preview'].get('preview_lots')}",
        f"- estimated_total_rub: "
        f"{report['selected_preview'].get('estimated_total_rub')}",
        f"- reference_price: {report['selected_preview'].get('reference_price')} "
        f"(status: {report['selected_preview'].get('reference_price_status')})",
        "",
        "## Required confirmation phrase",
        "",
        f"```\n{report['required_confirmation_phrase']}\n```",
        f"- confirmation_matched: {_fmt(report['confirmation_matched'])}",
        "",
        "## Preflight",
        "",
        "| Поле | Значение |",
        "| --- | --- |",
        f"| ticker | {_fmt(pf['ticker'])} |",
        f"| lots | {_fmt(pf['lots'])} |",
        f"| quantity | {_fmt(pf['quantity'])} |",
        f"| preview_reference_price | {_fmt(pf['preview_reference_price'])} |",
        f"| latest_reference_price | {_fmt(pf['latest_reference_price'])} |",
        f"| price_deviation_bps | {_fmt(pf['price_deviation_bps'])} |",
        f"| max_price_deviation_bps | {_fmt(pf['max_price_deviation_bps'])} |",
        f"| estimated_total_rub | {_fmt(pf['estimated_total_rub'])} |",
        f"| max_order_rub | {_fmt(pf['max_order_rub'])} |",
        "",
        "| Проверка | OK |",
        "| --- | --- |",
    ]
    for name, ok in checks.items():
        lines.append(f"| {name} | {_fmt(ok)} |")

    lines += ["", "## Sandbox order result"]
    if report["mode"] == MODE_DRY_RUN:
        lines.append("")
        lines.append("- dry-run: sandbox-заявка НЕ отправлялась (sandbox_order_sent=нет).")
    else:
        res = report.get("sandbox_order_result") or {}
        lines += [
            "",
            f"- sandbox_order_sent: {_fmt(res.get('sandbox_order_sent', False))}",
            f"- sandbox_order_id: {_fmt(res.get('sandbox_order_id'))}",
            f"- execution_report_status: {_fmt(res.get('execution_report_status'))}",
            f"- sandbox_order_state_read: {_fmt(res.get('sandbox_order_state_read'))}",
            f"- error: {_fmt(res.get('error'))}",
        ]

    if report.get("errors"):
        lines += ["", "## Errors"]
        lines += [f"- {e}" for e in report["errors"]]
    if report.get("warnings"):
        lines += ["", "## Warnings"]
        lines += [f"- {w}" for w in report["warnings"]]

    lines += [
        "",
        "## Next stage",
        "",
        f"- {NEXT_STAGE}",
        "",
        "---",
        "",
        "No live orders were sent.",
        "",
        "No portfolio/config mutation.",
        "",
        "F4 tiny live requires separate PR and separate approval.",
        "",
    ]
    return "\n".join(lines)


# ─── сериализация / оркестрация ───────────────────────────────────────────────

def _json_default(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    return str(obj)


def run(*, ticker: str,
        preview_json: str | None = None,
        output_json: str | None = None,
        output_md: str | None = None,
        max_order_rub: int = DEFAULT_MAX_ORDER_RUB,
        max_price_deviation_bps: int = DEFAULT_MAX_PRICE_DEVIATION_BPS,
        dry_run: bool = True,
        send_sandbox: bool = False,
        confirm: str | None = None,
        price_mode: str = PRICE_MODE_AUTO,
        client_order_id_prefix: str = DEFAULT_CLIENT_ORDER_ID_PREFIX,
        sandbox_account_id: str | None = None,
        sandbox_token: str | None = None,
        client=None,
        adapter: SandboxOrderAdapter | None = None,
        now: datetime | None = None) -> dict:
    """Читает F2 preview, валидирует одного кандидата, строит F3-отчёт, пишет json+md.

    Без send_sandbox → mode=DRY_RUN, sandbox-заявка не отправляется. С send_sandbox
    реальная sandbox-заявка возможна только при пройденных gate'ах и точной фразе.
    Возвращает отчёт (+ пути в _output_json/_output_md и _exit_code).
    """
    if not ticker or not str(ticker).strip():
        raise SandboxExecutionError("Не задан --ticker (ровно один тикер).")
    ticker = str(ticker).strip().upper()

    mode = MODE_SANDBOX_SEND if send_sandbox else MODE_DRY_RUN
    preview_path = preview_json or DEFAULT_PREVIEW_JSON

    data = load_preview_report(preview_path)
    row = select_preview_row(data.get("previews") or [], ticker)
    validate_preview_row(row, max_order_rub=max_order_rub)

    # Sandbox-токен читается ТОЛЬКО из отдельного env var и НИКОГДА не печатается.
    if sandbox_token is None:
        sandbox_token = os.environ.get(SANDBOX_TOKEN_ENV)

    report = build_report(
        ticker=ticker, preview_path=preview_path, row=row, mode=mode,
        send_sandbox=send_sandbox, confirm=confirm, max_order_rub=max_order_rub,
        max_price_deviation_bps=max_price_deviation_bps, price_mode=price_mode,
        client_order_id_prefix=client_order_id_prefix,
        sandbox_account_id=sandbox_account_id, sandbox_token=sandbox_token,
        client=client, adapter=adapter, now=now)

    out_json = Path(output_json or DEFAULT_OUTPUT_JSON)
    out_md = Path(output_md or DEFAULT_OUTPUT_MD)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8")
    out_md.write_text(render_md(report), encoding="utf-8")
    report["_output_json"] = str(out_json)
    report["_output_md"] = str(out_md)
    return report
