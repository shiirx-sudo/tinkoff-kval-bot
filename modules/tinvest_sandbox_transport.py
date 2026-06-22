"""
tinvest_sandbox_transport — F3.1 verified sandbox transport adapter (ТОЛЬКО sandbox).

Реализует sandbox-only отправку заявок через ПРОВЕРЕННЫЙ gRPC-over-REST контракт
T-Invest API. Транспортное соглашение — то же, что у read-only клиента
`brokers/tinkoff/rest_client.py` (источник 3: уже существующий проверенный REST
pattern в проекте): base URL `https://invest-public-api.tinkoff.ru/rest`, путь
`/{service}/{method}`, Bearer-токен, JSON в camelCase, `Quotation = {units, nano}`.

Точный контракт sandbox подтверждён по официальным proto-файлам
RussianInvestments/investAPI (источник 2: official proto / generated API contract):

  src/docs/contracts/sandbox.proto
    package tinkoff.public.invest.api.contract.v1;
    service SandboxService {
      rpc PostSandboxOrder(PostOrderRequest) returns (PostOrderResponse);
      rpc GetSandboxOrderState(GetOrderStateRequest) returns (OrderState);
    }

  src/docs/contracts/orders.proto
    message PostOrderRequest {
      int64        quantity      = 2;  // КОЛИЧЕСТВО ЛОТОВ (не штук)
      Quotation    price         = 3;  // лимитная цена
      OrderDirection direction   = 4;  // ORDER_DIRECTION_BUY
      string       account_id    = 5;  // sandbox account id
      OrderType    order_type    = 6;  // ORDER_TYPE_LIMIT
      string       order_id      = 7;  // идемпотентный client order id
      string       instrument_id = 8;  // figi или instrument_uid
    }
    message PostOrderResponse {
      string order_id; OrderExecutionReportStatus execution_report_status;
      int64 lots_requested; int64 lots_executed; MoneyValue total_order_amount;
      string message; ...
    }
    enum OrderDirection { ORDER_DIRECTION_BUY = 1; ORDER_DIRECTION_SELL = 2; }
    enum OrderType { ORDER_TYPE_LIMIT = 1; ORDER_TYPE_MARKET = 2; }
    message GetOrderStateRequest { string account_id = 1; string order_id = 2; }

В gRPC-over-REST gateway имена proto-полей сериализуются в lowerCamelCase
(`account_id` → `accountId`, `order_type` → `orderType`, `order_id` → `orderId`,
`instrument_id` → `instrumentId`), int64 — строкой; ровно так уже работает
`rest_client.py`.

Жёсткий контракт этого адаптера:
- ТОЛЬКО sandbox. ТОЛЬКО BUY. ТОЛЬКО LIMIT. MARKET → hard fail.
- Никакого live order-endpoint, никакого live `Orders`-сервиса, никакого
  full-access live токена, никакого live account.
- Токен берётся только из аргумента (источник — отдельный `TINKOFF_SANDBOX_TOKEN`),
  кладётся только в Authorization header и НИКОГДА не логируется/не печатается.
- Один вызов post_sandbox_order = максимум одна sandbox-заявка.
"""
from __future__ import annotations

import time
from typing import Any, Callable

from loguru import logger

from modules.income_sandbox_execution import (
    ORDER_DIRECTION_BUY,
    ORDER_TYPE_LIMIT,
    SandboxExecutionError,
    SandboxOrderAdapter,
)

# Источник контракта — для отчёта (proto, не догадка).
CONTRACT_SOURCE = (
    "RussianInvestments/investAPI proto: sandbox.proto "
    "(SandboxService.PostSandboxOrder / GetSandboxOrderState), orders.proto "
    "(PostOrderRequest / PostOrderResponse / OrderDirection / OrderType / "
    "GetOrderStateRequest); package tinkoff.public.invest.api.contract.v1"
)

_BASE_URL = "https://invest-public-api.tinkoff.ru/rest"
_DEFAULT_TIMEOUT = 10
_MAX_RETRIES = 5
_RATE_LIMIT_SLEEP = 1.0

# Имя sandbox-сервиса и методов из подтверждённого proto. Здесь нет ни одного
# live order-endpoint токена (никакого Orders-сервиса/live order endpoint).
_SANDBOX_SERVICE = "tinkoff.public.invest.api.contract.v1.SandboxService"
_METHOD_POST = "PostSandboxOrder"
_METHOD_STATE = "GetSandboxOrderState"


class SandboxTransportError(SandboxExecutionError):
    """Ошибка sandbox-транспорта (без traceback, безопасна для пользователя)."""


# Тип тестового транспорта: callable(method, payload, token) -> dict.
TransportCallable = Callable[[str, dict[str, Any], str], dict[str, Any]]


class VerifiedSandboxRestAdapter(SandboxOrderAdapter):
    """Проверенный sandbox REST-адаптер (BUY/LIMIT only).

    Принимает уже подготовленные безопасные параметры из F3 preflight. Сам НЕ
    выбирает инструмент/цену/лоты, НЕ читает live account, НЕ использует live токен
    и НЕ вызывает live order-endpoint.
    """

    CONTRACT_SOURCE = CONTRACT_SOURCE

    def __init__(self, *, transport: TransportCallable | None = None,
                 timeout_seconds: int = _DEFAULT_TIMEOUT,
                 max_retries: int = _MAX_RETRIES) -> None:
        # transport инъектируется в тестах (никакой реальной сети); в проде None.
        self._transport = transport
        self._timeout = timeout_seconds
        self._max_retries = max_retries

    # ─── транспорт ────────────────────────────────────────────────────────────

    def _post(self, method: str, payload: dict[str, Any], token: str) -> dict[str, Any]:
        if self._transport is not None:
            return self._transport(method, payload, token)
        return self._http_post(method, payload, token)

    def _http_post(self, method: str, payload: dict[str, Any],
                   token: str) -> dict[str, Any]:
        # requests импортируется лениво; токен только в заголовке, не логируется.
        import requests

        if not token:
            raise SandboxTransportError("Пустой sandbox-токен: отправка невозможна.")
        url = f"{_BASE_URL}/{_SANDBOX_SERVICE}/{method}"
        session = requests.Session()
        session.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        last_exc: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                resp = session.post(url, json=payload, timeout=self._timeout)
                if resp.status_code == 429:
                    wait = _RATE_LIMIT_SLEEP * attempt
                    logger.warning(
                        f"Rate-limit на sandbox {method} (попытка {attempt}). "
                        f"Ждём {wait:.1f}с")
                    time.sleep(wait)
                    last_exc = requests.HTTPError("429 Too Many Requests")
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.HTTPError as exc:
                msg = str(exc).lower()
                if "resource_exhausted" in msg or "429" in msg:
                    wait = _RATE_LIMIT_SLEEP * attempt
                    logger.warning(
                        f"Rate-limit на sandbox {method} (попытка {attempt}). "
                        f"Ждём {wait:.1f}с")
                    time.sleep(wait)
                    last_exc = exc
                    continue
                raise SandboxTransportError(f"Ошибка sandbox {method}: {exc}") from exc
        assert last_exc is not None
        raise SandboxTransportError(f"Sandbox {method}: исчерпаны ретраи (429).")

    # ─── sandbox-only API ──────────────────────────────────────────────────────

    def post_sandbox_order(self, *, request: dict, account_id: str,
                           token: str) -> dict:
        """PostSandboxOrder: ровно одна sandbox-заявка BUY/LIMIT по proto-контракту."""
        # Жёсткие предохранители: только LIMIT BUY, только sandbox, только с токеном.
        order_type = request.get("order_type")
        if order_type != ORDER_TYPE_LIMIT:
            raise SandboxTransportError(
                f"Sandbox transport принимает только {ORDER_TYPE_LIMIT}; "
                f"order_type={order_type}. MARKET-заявки запрещены. Не отправлено.")
        direction = request.get("direction")
        if direction != ORDER_DIRECTION_BUY:
            raise SandboxTransportError(
                f"Sandbox transport принимает только {ORDER_DIRECTION_BUY}; "
                f"direction={direction}. Не отправлено.")
        if not account_id:
            raise SandboxTransportError("Не задан sandbox account id. Не отправлено.")
        if not token:
            raise SandboxTransportError("Не задан sandbox-токен. Не отправлено.")

        instrument = request.get("instrument") or {}
        instrument_id = instrument.get("figi") or instrument.get("uid")
        if not instrument_id:
            raise SandboxTransportError(
                "Нет instrument id (figi/uid) для sandbox-заявки. Не отправлено.")

        lots = request.get("lots")
        if not isinstance(lots, int) or isinstance(lots, bool) or lots <= 0:
            raise SandboxTransportError(
                f"Некорректное число лотов lots={lots}. Не отправлено.")

        price_q = request.get("limit_price_quotation")
        if not isinstance(price_q, dict) or "units" not in price_q:
            raise SandboxTransportError(
                "Нет лимитной цены (Quotation) для LIMIT-заявки. Не отправлено.")

        client_order_id = request.get("client_order_id")
        if not client_order_id:
            raise SandboxTransportError("Нет client_order_id. Не отправлено.")

        # PostOrderRequest (camelCase JSON; quantity = ЛОТЫ, int64 → строка).
        payload = {
            "quantity": str(int(lots)),
            "price": price_q,
            "direction": ORDER_DIRECTION_BUY,
            "accountId": account_id,
            "orderType": ORDER_TYPE_LIMIT,
            "orderId": client_order_id,
            "instrumentId": instrument_id,
        }
        return self._post(_METHOD_POST, payload, token)

    def get_sandbox_order_state(self, *, account_id: str, order_id: str,
                                token: str) -> dict | None:
        """GetSandboxOrderState: read-only статус sandbox-заявки (GetOrderStateRequest)."""
        if not account_id or not order_id or not token:
            return None
        payload = {"accountId": account_id, "orderId": order_id}
        return self._post(_METHOD_STATE, payload, token)
