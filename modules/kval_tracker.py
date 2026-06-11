"""
Главный модуль расчёта прогресса к статусу квалифицированного инвестора.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from decimal import Decimal

from loguru import logger

from api.client import ReadOnlyClient
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
    """Агрегированный оборот по одному брокерскому счёту."""
    account_id: str
    account_name: str
    total_turnover: Decimal = Decimal("0")
    trade_count: int = 0
    operation_count: int = 0
    approximate_count: int = 0
    by_quarter: dict[str, QuarterTurnover] = field(default_factory=dict)
    operations: list[OperationTurnoverResult] = field(default_factory=list)


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
    def total_operation_count(self) -> int:
        return sum(a.operation_count for a in self.accounts)


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

    def __init__(self, client: ReadOnlyClient | None = None) -> None:
        self.client = client or ReadOnlyClient()

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
        grand_total = Decimal("0")

        for acc in accounts_raw:
            account_id = str(getattr(acc, "id", ""))
            account_name = str(getattr(acc, "name", "") or account_id)

            ap = AccountProgress(
                account_id=account_id,
                account_name=account_name,
                by_quarter={
                    q.label: QuarterTurnover(label=q.label)
                    for q in period.quarters
                },
            )

            operations = self.client.get_operations(account_id, from_dt, to_dt)

            for op in operations:
                if not is_qualifying_operation(op):
                    continue

                result = calculate_operation_turnover(op, account_id)
                op_turnover = (
                    result.turnover_approximate
                    if result.is_approximate
                    else result.turnover_exact
                )

                ap.operations.append(result)
                ap.total_turnover += op_turnover
                ap.operation_count += 1
                ap.trade_count += sum(
                    1 for t in result.trades if not t.is_approximate
                ) or result.trade_count
                all_trades.extend(result.trades)

                if result.is_approximate:
                    ap.approximate_count += 1
                    if result.warning:
                        warnings.append(result.warning)

                # Разнос по кварталам
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

        progress = KvalProgress(
            period=period,
            target=settings.kval_target,
            effective_target=settings.effective_target,
            total_turnover=grand_total,
            accounts=account_results,
            all_trades=all_trades,
            approximate_warnings=warnings,
            generated_at=datetime.now(timezone.utc).isoformat(),
        )

        logger.info(
            f"ИТОГО оборот: {grand_total} ₽ из {settings.kval_target} ₽ "
            f"({progress.progress_pct}%). "
            f"Достигнуто (с буфером): {progress.achieved}"
        )
        return progress


def analyze(as_of: date | None = None) -> KvalProgress:
    """Удобная функция-обёртка."""
    return KvalTracker().analyze(as_of)
