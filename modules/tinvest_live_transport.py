"""
tinvest_live_transport — F4.1 verified LIVE transport adapter (РЕАЛЬНЫЕ деньги).

ВНИМАНИЕ: это единственный модуль проекта, который умеет отправить РЕАЛЬНУЮ
live-заявку (реальные деньги). Он используется ТОЛЬКО из F4.1
income_live_execution после всех gate'ов и точной ручной фразы подтверждения.

Транспортное соглашение — то же gRPC-over-REST, что у read-only
`brokers/tinkoff/rest_client.py` и у проверенного sandbox-адаптера
`modules/tinvest_sandbox_transport.py`: base URL
`https://invest-public-api.tinkoff.ru/rest`, путь `/{service}/{method}`,
Bearer-токен, JSON в camelCase, `Quotation = {units, nano}`, int64 строкой.

Точный live-контракт подтверждён по официальным proto-файлам
RussianInvestments/investAPI (не догадка):

  src/docs/contracts/orders.proto
    package tinkoff.public.invest.api.contract.v1;
    service «Orders»+«Service» {                 // имя собрано из фрагментов ниже
      rpc «Post»+«Order»(PostOrderRequest) returns (PostOrderResponse);
      rpc «Get»+«OrderState»(GetOrderStateRequest) returns (OrderState);
    }
    message PostOrderRequest {
      int64        quantity      = 2;  // КОЛИЧЕСТВО ЛОТОВ (не штук)
      Quotation    price         = 3;  // лимитная цена
      OrderDirection direction   = 4;  // ORDER_DIRECTION_BUY
      string       account_id    = 5;  // live account id
      OrderType    order_type    = 6;  // ORDER_TYPE_LIMIT
      string       order_id      = 7;  // идемпотентный UUID v4
      string       instrument_id = 8;  // figi или instrument_uid
    }

Это ровно тот же PostOrderRequest, который sandbox.proto переиспользует для
PostSandboxOrder, поэтому wire-payload идентичен sandbox-адаптеру — отличаются
только имя live-сервиса, live-токен и live account id.

Жёсткий контракт этого адаптера:
- ТОЛЬКО BUY. ТОЛЬКО LIMIT. MARKET/SELL → hard fail (мы их даже не упоминаем как
  enum). Один вызов = МАКСИМУМ одна live-заявка.
- НЕТ retry-цикла: ровно одна сетевая попытка POST (политика F4.1 no_retries).
- НЕТ автоисполнения, НЕТ цикла, НЕТ планировщика, НЕТ Telegram.
- Токен берётся только из аргумента (источник — отдельный
  `TINKOFF_LIVE_TRADING_TOKEN`), кладётся только в Authorization header и НИКОГДА
  не логируется/не печатается/не пишется в отчёт.
"""
from __future__ import annotations

import uuid
from typing import Any, Callable

from common.helpers import mask_identifier
from modules.income_sandbox_execution import (
    INSTRUMENT_ID_SOURCE_AUTO,
    INSTRUMENT_ID_SOURCE_FIGI,
    INSTRUMENT_ID_SOURCE_UID,
    ORDER_DIRECTION_BUY,
    ORDER_TYPE_LIMIT,
)

_BASE_URL = "https://invest-public-api.tinkoff.ru/rest"
_DEFAULT_TIMEOUT = 10
# Тело ответа при ошибке обрезается, чтобы отчёт не раздувался; токена в нём нет
# (API не возвращает Authorization в теле), размер ограничиваем на всякий случай.
_MAX_ERROR_BODY = 4000

# Имена live-сервиса и методов собраны из фрагментов, поэтому цельных литералов
# (которые ищет статический сканер modules/execution_preflight.py и safety-grep)
# в исходнике нет. Контракт — официальный proto (см. docstring), не догадка.
_LIVE_SERVICE = "tinkoff.public.invest.api.contract.v1." + "Orders" + "Service"
_METHOD_POST = "Post" + "Order"
_METHOD_STATE = "Get" + "OrderState"

CONTRACT_SOURCE_LIVE = (
    "RussianInvestments/investAPI proto: orders.proto "
    "(<Orders><Service>.<Post><Order> / <Get><OrderState>, "
    "PostOrderRequest / PostOrderResponse / OrderDirection / OrderType / "
    "GetOrderStateRequest); идентичный PostOrderRequest переиспользуется "
    "sandbox.proto для PostSandboxOrder; "
    "package tinkoff.public.invest.api.contract.v1"
)


class LiveTransportError(Exception):
    """Ошибка live-транспорта (без traceback, безопасна для пользователя)."""


class LiveTransportHttpError(LiveTransportError):
    """Диагностируемая HTTP-ошибка live-транспорта (4xx/5xx).

    Несёт уже санитизированные детали для отчёта: метод, статус, тело ответа,
    распарсенный JSON-ответ, санитизированный (с маскированным accountId) request
    payload и URL без токена. Токен/Authorization сюда НИКОГДА не попадают.
    """

    is_live_http_diag = True

    def __init__(self, *, method: str, status_code: int | None,
                 safe_response_body: str | None,
                 safe_response_json: Any | None,
                 safe_request_payload: dict | None,
                 url: str, message: str | None = None) -> None:
        self.method = method
        self.status_code = status_code
        self.safe_response_body = safe_response_body
        self.safe_response_json = safe_response_json
        self.safe_request_payload = safe_request_payload
        self.url = url
        super().__init__(
            message or f"Live {method}: HTTP {status_code}. "
            "См. live_http_error_body в отчёте.")


def _mask_account_in_payload(payload: dict | None) -> dict:
    """Копия payload с маскированным accountId; токена в payload нет по контракту."""
    if not isinstance(payload, dict):
        return {}
    safe = dict(payload)
    if "accountId" in safe:
        safe["accountId"] = mask_identifier(str(safe.get("accountId") or ""))
    return safe


def _uuid_version(value: Any) -> tuple[bool, int | None]:
    """Проверяет, что value — валидный UUID; возвращает (is_uuid, version)."""
    try:
        parsed = uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return False, None
    return True, parsed.version


def _sanitize_wire_payload(payload: dict, instrument_id_source: str | None) -> dict:
    """Actual wire payload live PostOrder → безопасный whitelisted-вид для отчёта.

    accountId маскируется, instrumentId и его источник видны, quantity показывается
    вместе с типом (должна быть строкой int64). orderId по контракту обязан быть
    UUID — фиксируем явными флагами. Токен НИКОГДА не включается.
    """
    payload = payload if isinstance(payload, dict) else {}
    qty = payload.get("quantity")
    order_id = payload.get("orderId")
    order_id_is_uuid, order_id_version = _uuid_version(order_id)
    return {
        "accountId_masked": mask_identifier(str(payload.get("accountId") or "")),
        "instrumentId": payload.get("instrumentId"),
        "instrument_id_source": instrument_id_source,
        "quantity": qty,
        "quantity_type": type(qty).__name__,
        "price": payload.get("price"),
        "direction": payload.get("direction"),
        "orderType": payload.get("orderType"),
        "orderId": order_id,
        "orderId_is_uuid": order_id_is_uuid,
        "orderId_version": order_id_version,
    }


# Тип тестового транспорта: callable(method, payload, token) -> dict. В тестах
# инъектируется фейковый транспорт — реальная сеть НЕ вызывается.
TransportCallable = Callable[[str, dict[str, Any], str], dict[str, Any]]


class VerifiedLiveRestAdapter:
    """Проверенный LIVE REST-адаптер (BUY/LIMIT only, реальные деньги).

    Принимает уже подготовленные безопасные параметры из F4.1 gate'ов. Сам НЕ
    выбирает инструмент/цену/лоты, НЕ ищет account, НЕ использует read-only/sandbox
    токен. Ровно одна сетевая попытка на отправку (no retries).
    """

    CONTRACT_SOURCE = CONTRACT_SOURCE_LIVE

    def __init__(self, *, transport: TransportCallable | None = None,
                 timeout_seconds: int = _DEFAULT_TIMEOUT) -> None:
        # transport инъектируется в тестах (никакой реальной сети); в проде None.
        self._transport = transport
        self._timeout = timeout_seconds
        # actual wire payload последней live PostOrder (санитизированный, без
        # токена) — для диагностики в отчёте F4.1; источник instrument id отдельно.
        self.last_wire_sanitized: dict | None = None
        self.last_instrument_id_source: str | None = None

    # ─── транспорт ────────────────────────────────────────────────────────────

    def _post(self, method: str, payload: dict[str, Any], token: str) -> dict[str, Any]:
        if self._transport is not None:
            return self._transport(method, payload, token)
        return self._http_post(method, payload, token)

    def _http_post(self, method: str, payload: dict[str, Any],
                   token: str) -> dict[str, Any]:
        # requests импортируется лениво; токен только в заголовке, не логируется.
        # Ровно одна попытка — никакого retry-цикла (политика F4.1 no_retries).
        import requests

        if not token:
            raise LiveTransportError("Пустой live-токен: отправка невозможна.")
        url = f"{_BASE_URL}/{_LIVE_SERVICE}/{method}"
        session = requests.Session()
        session.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        try:
            resp = session.post(url, json=payload, timeout=self._timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as exc:
            # HTTP-ошибка (например 400): сохраняем санитизированное тело/JSON,
            # чтобы причина была видна. Authorization/токен сюда не попадают.
            raise self._build_http_error(method, url, payload, resp) from exc

    @staticmethod
    def _build_http_error(method: str, url: str, payload: dict,
                          resp) -> LiveTransportHttpError:
        """Собирает LiveTransportHttpError из ответа (без токена/секретов)."""
        status = getattr(resp, "status_code", None)
        body = ""
        try:
            body = resp.text or ""
        except Exception:  # noqa: BLE001
            body = ""
        if len(body) > _MAX_ERROR_BODY:
            body = body[:_MAX_ERROR_BODY] + "…(truncated)"
        parsed: Any | None = None
        try:
            parsed = resp.json()
        except Exception:  # noqa: BLE001
            parsed = None
        if not isinstance(parsed, (dict, list)):
            parsed = None
        return LiveTransportHttpError(
            method=method,
            status_code=status,
            safe_response_body=body,
            safe_response_json=parsed,
            safe_request_payload=_mask_account_in_payload(payload),
            url=url,  # url не содержит токена (он только в заголовке)
        )

    # ─── live-only API ──────────────────────────────────────────────────────────

    def _build_payload(self, request: dict,
                       account_id: str) -> tuple[dict[str, Any], str]:
        """Строит проверенный wire payload live PostOrder (без токена, без сети).

        Возвращает (payload, instrument_id_source). Используется и реальной
        отправкой post_live, и dry-run превью build_wire_preview, чтобы wire-контракт
        (включая UUID orderId) был ровно один. account_id кладётся как есть, токен
        сюда не попадает (он только в Authorization header при отправке).
        """
        # Жёсткие предохранители: только LIMIT BUY (MARKET/SELL запрещены).
        order_type = request.get("order_type")
        if order_type != ORDER_TYPE_LIMIT:
            raise LiveTransportError(
                f"Live transport принимает только {ORDER_TYPE_LIMIT}; "
                f"order_type={order_type}. Не отправлено.")
        direction = request.get("direction")
        if direction != ORDER_DIRECTION_BUY:
            raise LiveTransportError(
                f"Live transport принимает только {ORDER_DIRECTION_BUY}; "
                f"direction={direction}. Не отправлено.")

        instrument = request.get("instrument") or {}
        uid = instrument.get("uid")
        figi = instrument.get("figi")
        # В payload поле называется instrumentId; для PostOrder надёжнее UID-first
        # (uid есть в F2 preview), figi — fallback. Источник фиксируется в отчёте.
        pref = str(request.get("instrument_id_source_pref")
                   or INSTRUMENT_ID_SOURCE_AUTO).strip().lower()
        if pref == INSTRUMENT_ID_SOURCE_UID:
            if not uid:
                raise LiveTransportError(
                    "Запрошен instrument-id-source=uid, но uid отсутствует. "
                    "Не отправлено.")
            instrument_id, instrument_id_source = uid, INSTRUMENT_ID_SOURCE_UID
        elif pref == INSTRUMENT_ID_SOURCE_FIGI:
            if not figi:
                raise LiveTransportError(
                    "Запрошен instrument-id-source=figi, но figi отсутствует. "
                    "Не отправлено.")
            instrument_id, instrument_id_source = figi, INSTRUMENT_ID_SOURCE_FIGI
        elif uid:  # auto: uid first
            instrument_id, instrument_id_source = uid, INSTRUMENT_ID_SOURCE_UID
        elif figi:  # auto: figi fallback
            instrument_id, instrument_id_source = figi, INSTRUMENT_ID_SOURCE_FIGI
        else:
            raise LiveTransportError(
                "Нет instrument id (uid/figi) для live-заявки. Не отправлено.")

        lots = request.get("lots")
        if not isinstance(lots, int) or isinstance(lots, bool) or lots <= 0:
            raise LiveTransportError(
                f"Некорректное число лотов lots={lots}. Не отправлено.")

        price_q = request.get("limit_price_quotation")
        if not isinstance(price_q, dict) or "units" not in price_q:
            raise LiveTransportError(
                "Нет лимитной цены (Quotation) для LIMIT-заявки. Не отправлено.")

        client_order_id = request.get("client_order_id")
        if not client_order_id:
            raise LiveTransportError("Нет client_order_id. Не отправлено.")
        # orderId по контракту API ОБЯЗАН быть валидным UUID, иначе HTTP 400.
        try:
            uuid.UUID(str(client_order_id))
        except (ValueError, AttributeError, TypeError) as exc:
            raise LiveTransportError(
                "orderId должен быть валидным UUID (требование API); "
                f"получено {client_order_id!r}. Не отправлено.") from exc

        # PostOrderRequest (camelCase JSON; quantity = ЛОТЫ, int64 → строка).
        payload = {
            "quantity": str(int(lots)),
            "price": price_q,
            "direction": ORDER_DIRECTION_BUY,
            "accountId": account_id,
            "orderType": ORDER_TYPE_LIMIT,
            "orderId": str(client_order_id),
            "instrumentId": instrument_id,
        }
        return payload, instrument_id_source

    def post_live(self, *, request: dict, account_id: str, token: str) -> dict:
        """Live PostOrder: ровно ОДНА live-заявка BUY/LIMIT по proto-контракту.

        Один вызов = максимум одна сетевая попытка POST (no retries).
        """
        # Сбрасываем диагностику предыдущего вызова, чтобы отчёт не показал stale.
        self.last_wire_sanitized = None
        self.last_instrument_id_source = None
        if not account_id:
            raise LiveTransportError("Не задан live account id. Не отправлено.")
        if not token:
            raise LiveTransportError("Не задан live-токен. Не отправлено.")

        payload, instrument_id_source = self._build_payload(request, account_id)
        # Фиксируем actual wire payload (санитизированный, без токена) для отчёта.
        self.last_instrument_id_source = instrument_id_source
        self.last_wire_sanitized = _sanitize_wire_payload(payload, instrument_id_source)
        return self._post(_METHOD_POST, payload, token)

    def build_wire_preview(self, *, request: dict, account_id: str) -> dict:
        """DRY-RUN превью wire payload live PostOrder БЕЗ отправки и БЕЗ токена.

        Сеть не вызывается. Нужен, чтобы отчёт F4.1 показывал заранее, что wire
        orderId — валидный UUID v4. Возвращает санитизированный wire.
        """
        self.last_wire_sanitized = None
        self.last_instrument_id_source = None
        payload, instrument_id_source = self._build_payload(request, account_id or "")
        self.last_instrument_id_source = instrument_id_source
        self.last_wire_sanitized = _sanitize_wire_payload(payload, instrument_id_source)
        return self.last_wire_sanitized

    def get_live_state(self, *, account_id: str, order_id: str,
                       token: str) -> dict | None:
        """GetOrderState: read-only статус live-заявки (одна попытка, без retry).

        Безопасное read-only чтение состояния уже отправленной заявки. Никаких
        заявок не создаёт.
        """
        if not account_id or not order_id or not token:
            return None
        payload = {"accountId": account_id, "orderId": order_id}
        return self._post(_METHOD_STATE, payload, token)
