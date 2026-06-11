"""
Главный модуль расчёта прогресса к статусу квалифицированного инвестора.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from decimal import Decimal

from loguru import logger

from api.client import ReadOnlyClient
from brokers.tinkoff.rest_client import account_type_label
from common.helpers import mask_identifier
from config.settings import settings
from modules.operation_filter import is_qualifying_operation
from modules.period_calculator import KvalPeriod, Quarter, calculate_kval_period
from modules.turnover_calculator import (
    OperationTurnoverResult,
    TradeRecord,
    calculate_operation_turnover,
)


# ─── Dataclasses результатов ────────────────────────────────────────────────


@dataclass
class QuarterTurnover:
    """Оборот в разрезе одного квартала."""
    label: str
    turnover: Decimal = Decimal("0")
    trade_count: int = 0
    operation_count: int = 0


@dataclass
class AccountProgress:
    """Агрегированный оборот по одному брокерскому счёту (включая ИИС)."""
    account_id: str
    account_name: str
    account_type: str = "broker"
    total_turnover: Decimal = Decimal("0")
    operation_count: int = 0
    exact_trade_count: int = 0          # сделки из операций с детализацией trades
    approximate_trade_count: int = 0    # операции без trades, каждая считается за 1
    by_quarter: dict[str, QuarterTurnover] = field(default_factory=dict)
    operations: list[OperationTurnoverResult] = field(default_factory=list)

    @property
    def trade_count(self) -> int:
        """Всего сделок (точные + приближённые)."""
        return self.exact_trade_count + self.approximate_trade_count

    @property
    def approximate_count(self) -> int:
        """Совместимость: число приближённых операций."""
        return self.approximate_trade_count


MONTH_MIN_TRADES = 1     # каждый месяц периода: минимум сделок
QUARTER_MIN_TRADES = 10  # каждый квартал периода: минимум сделок


@dataclass
class MonthCheck:
    """Активность за месяц периода."""
    label: str            # 'YYYY-MM'
    operation_count: int = 0
    trade_count: int = 0
    turnover: Decimal = Decimal("0")

    @property
    def ok(self) -> bool:
        return self.trade_count >= MONTH_MIN_TRADES


@dataclass
class QuarterCheck:
    """Активность за квартал периода."""
    label: str            # 'YYYYQn'
    operation_count: int = 0
    trade_count: int = 0
    turnover: Decimal = Decimal("0")

    @property
    def ok(self) -> bool:
        return self.trade_count >= QUARTER_MIN_TRADES


@dataclass
class KvalProgress:
    """
    Итоговый результат: прогресс по всем счетам к квал-цели.
    """
    period: KvalPeriod
    target: Decimal
    effective_target: Decimal
    total_turnover: Decimal
    accounts: list[AccountProgress]
    all_trades: list[TradeRecord]
    approximate_warnings: list[str]
    generated_at: str
    months: list[MonthCheck] = field(default_factory=list)
    quarter_checks: list[QuarterCheck] = field(default_factory=list)
    raw_operations: list[dict] = field(default_factory=list)

    # ─── Производные метрики ────────────────────────────────────────────────

    @property
    def progress_ratio(self) -> Decimal:
        """Доля выполнения от «голой» цели (без буфера), 0..1+."""
        if self.target <= 0:
            return Decimal("0")
        return self.total_turnover / self.target

    @property
    def progress_pct(self) -> Decimal:
        """Процент выполнения от «голой» цели."""
        return (self.progress_ratio * Decimal("100")).quantize(Decimal("0.01"))

    @property
    def remaining_to_target(self) -> Decimal:
        """Сколько осталось до «голой» цели (не меньше нуля)."""
        return max(Decimal("0"), self.target - self.total_turnover)

    @property
    def remaining_to_effective(self) -> Decimal:
        """Сколько осталось до цели с буфером безопасности."""
        return max(Decimal("0"), self.effective_target - self.total_turnover)

    @property
    def achieved(self) -> bool:
        """Достигнута ли цель с учётом буфера безопасности."""
        return self.total_turnover >= self.effective_target

    @property
    def achieved_bare(self) -> bool:
        """Достигнута ли «голая» цель (без буфера)."""
        return self.total_turnover >= self.target

    @property
    def has_approximate(self) -> bool:
        return len(self.approximate_warnings) > 0

    @property
    def total_trade_count(self) -> int:
        return sum(a.trade_count for a in self.accounts)

    @property
    def total_exact_trade_count(self) -> int:
        return sum(a.exact_trade_count for a in self.accounts)

    @property
    def total_approximate_trade_count(self) -> int:
        return sum(a.approximate_trade_count for a in self.accounts)

    @property
    def approximate_warning_count(self) -> int:
        return len(self.approximate_warnings)

    @property
    def total_operation_count(self) -> int:
        return sum(a.operation_count for a in self.accounts)

    # ─── Право на квал-статус: оборот + помесячная/поквартальная активность ──

    @property
    def turnover_ok(self) -> bool:
        """Оборот достиг цели с буфером безопасности."""
        return self.total_turnover >= self.effective_target

    @property
    def months_ok(self) -> bool:
        """В каждом месяце периода есть минимум сделок."""
        return bool(self.months) and all(m.ok for m in self.months)

    @property
    def quarters_ok(self) -> bool:
        """В каждом квартале периода есть минимум сделок."""
        return bool(self.quarter_checks) and all(q.ok for q in self.quarter_checks)

    @property
    def qualification_ready(self) -> bool:
        """
        Готовность к квал-статусу: оборот достигнут И каждый месяц активен
        (>= 1 сделки) И каждый квартал активен (>= 10 сделок).
        """
        return self.turnover_ok and self.months_ok and self.quarters_ok


# ─── Трекер ─────────────────────────────────────────────────────────────────


def _to_aware_dt(d: date, end_of_day: bool = False) -> datetime:
    """Преобразует date → timezone-aware datetime (UTC)."""
    t = time.max if end_of_day else time.min
    return datetime.combine(d, t).replace(tzinfo=timezone.utc)


def _quarter_for(d: date, period: KvalPeriod) -> Quarter | None:
    """Находит квартал периода, которому принадлежит дата d."""
    for q in period.quarters:
        if q.contains(d):
            return q
    return None


def _period_months(period: KvalPeriod) -> list[str]:
    """Список меток месяцев 'YYYY-MM' от начала до конца периода включительно."""
    months: list[str] = []
    y, m = period.start.year, period.start.month
    end_y, end_m = period.end.year, period.end.month
    while (y, m) <= (end_y, end_m):
        months.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return months


def _mask_raw_operation(op: dict, account_id: str) -> dict:
    """
    Готовит запись операции для raw-экспорта: account_id маскируется,
    любые account-поля внутри тоже. Токенов в операции нет.
    """
    rec = dict(op)
    for key in ("accountId", "account_id"):
        if key in rec:
            rec[key] = mask_identifier(rec[key])
    rec["account_id_masked"] = mask_identifier(account_id)
    return rec


def _parse_op_date(date_str: str) -> date | None:
    """Парсит ISO-строку даты операции в date."""
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str).date()
    except ValueError:
        try:
            return date.fromisoformat(date_str[:10])
        except ValueError:
            return None


class KvalTracker:
    """
    Оркестратор: тянет операции по всем счетам за расчётный период,
    фильтрует, считает оборот и агрегирует прогресс к квал-цели.
    """

    def __init__(self, client: ReadOnlyClient | None = None, resolver=None) -> None:
        self.client = client or ReadOnlyClient()
        if resolver is None and hasattr(self.client, "instrument_resolver"):
            try:
                resolver = self.client.instrument_resolver()
            except Exception:  # noqa: BLE001
                resolver = None
        self.resolver = resolver

    def analyze(self, as_of: date | None = None) -> KvalProgress:
        """
        Полный расчёт прогресса.

        Parameters
        ----------
        as_of : date, optional
            Дата расчёта. По умолчанию — сегодня.
        """
        period = calculate_kval_period(as_of)
        from_dt = _to_aware_dt(period.start, end_of_day=False)
        to_dt = _to_aware_dt(period.end, end_of_day=True)

        logger.info(
            f"Расчётный период: {period.start} … {period.end} "
            f"({', '.join(q.label for q in period.quarters)})"
        )

        accounts_raw = self.client.get_broker_accounts()
        if not accounts_raw:
            logger.warning("Брокерские счета по токену не найдены.")

        account_results: list[AccountProgress] = []
        all_trades: list[TradeRecord] = []
        warnings: list[str] = []
        raw_operations: list[dict] = []
        grand_total = Decimal("0")

        for acc in accounts_raw:
            account_id = str(acc.get("id", ""))
            account_name = str(acc.get("name", "") or account_id)
            account_type = account_type_label(str(acc.get("type", "")))

            ap = AccountProgress(
                account_id=account_id,
                account_name=account_name,
                account_type=account_type,
                by_quarter={
                    q.label: QuarterTurnover(label=q.label)
                    for q in period.quarters
                },
            )

            operations = self.client.get_operations(account_id, from_dt, to_dt)

            for op in operations:
                if not is_qualifying_operation(op):
                    continue

                raw_operations.append(_mask_raw_operation(op, account_id))
                result = calculate_operation_turnover(op, account_id, resolver=self.resolver)
                op_turnover = (
                    result.turnover_approximate
                    if result.is_approximate
                    else result.turnover_exact
                )

                ap.operations.append(result)
                ap.total_turnover += op_turnover
                ap.operation_count += 1
                if result.is_approximate:
                    ap.approximate_trade_count += 1
                    if result.warning:
                        warnings.append(result.warning)
                else:
                    ap.exact_trade_count += sum(
                        1 for t in result.trades if not t.is_approximate
                    )
                all_trades.extend(result.trades)

                # Разнос по кварталам (per-account, для kval_quarters.csv)
                op_date = _parse_op_date(result.date)
                if op_date is not None:
                    q = _quarter_for(op_date, period)
                    if q is not None:
                        bucket = ap.by_quarter[q.label]
                        bucket.turnover += op_turnover
                        bucket.operation_count += 1
                        bucket.trade_count += result.trade_count

            grand_total += ap.total_turnover
            account_results.append(ap)
            logger.info(
                f"Счёт {account_name} ({account_id}): "
                f"оборот {ap.total_turnover} ₽, "
                f"операций {ap.operation_count}, "
                f"приближённых {ap.approximate_count}"
            )

        # Помесячная и поквартальная активность (по фактическим сделкам)
        month_checks = {lbl: MonthCheck(lbl) for lbl in _period_months(period)}
        quarter_checks = {q.label: QuarterCheck(q.label) for q in period.quarters}
        month_ops: dict[str, set[str]] = {lbl: set() for lbl in month_checks}
        quarter_ops: dict[str, set[str]] = {lbl: set() for lbl in quarter_checks}
        for t in all_trades:
            d = _parse_op_date(t.date)
            if d is None:
                continue
            mk = f"{d.year:04d}-{d.month:02d}"
            if mk in month_checks:
                month_checks[mk].trade_count += 1
                month_checks[mk].turnover += t.turnover
                month_ops[mk].add(t.operation_id)
            q = _quarter_for(d, period)
            if q is not None:
                quarter_checks[q.label].trade_count += 1
                quarter_checks[q.label].turnover += t.turnover
                quarter_ops[q.label].add(t.operation_id)
        for mk, mc in month_checks.items():
            mc.operation_count = len(month_ops[mk])
        for qk, qc in quarter_checks.items():
            qc.operation_count = len(quarter_ops[qk])

        progress = KvalProgress(
            period=period,
            target=settings.kval_target,
            effective_target=settings.effective_target,
            total_turnover=grand_total,
            accounts=account_results,
            all_trades=all_trades,
            approximate_warnings=warnings,
            generated_at=datetime.now(timezone.utc).isoformat(),
            months=[month_checks[lbl] for lbl in _period_months(period)],
            quarter_checks=[quarter_checks[q.label] for q in period.quarters],
            raw_operations=raw_operations,
        )

        logger.info(
            f"ИТОГО оборот: {grand_total} ₽ из {settings.kval_target} ₽ "
            f"({progress.progress_pct}%). "
            f"Готовность к квал-статусу: {progress.qualification_ready}"
        )
        return progress


def analyze(as_of: date | None = None) -> KvalProgress:
    """Удобная функция-обёртка."""
    return KvalTracker().analyze(as_of)
