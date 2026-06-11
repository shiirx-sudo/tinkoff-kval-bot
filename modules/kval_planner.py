"""
Qualification Planner — календарь выхода на статус квалифицированного инвестора.

Read-only аналитика поверх той же модели завершённых календарных кварталов, что и
KvalTracker. Строит «кандидатные окна» (текущий официальный 4-квартальный период и
последующие, по мере закрытия будущих кварталов), помечает невосполнимые прошлые
пропуски и строит план по месяцам/кварталам до ближайшего достижимого окна.

ВАЖНО: всё считается «по текущей модели расчёта и данным API». Финальное решение о
присвоении статуса принимает брокер — сверяйте с брокерским отчётом.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from loguru import logger

from api.client import ReadOnlyClient
from config.settings import settings
from modules.kval_tracker import MONTH_MIN_TRADES, QUARTER_MIN_TRADES
from modules.operation_filter import is_qualifying_operation
from modules.period_calculator import Quarter, calculate_kval_period
from modules.turnover_calculator import calculate_operation_turnover

DISCLAIMER = (
    "Оценка по текущей модели расчёта и данным API. Это не гарантия: финальное "
    "решение о присвоении статуса принимает брокер. Сверяйте с брокерским отчётом."
)

WINDOW_QUARTERS = 4


# ─── Модель данных ───────────────────────────────────────────────────────────


@dataclass
class CandidateWindow:
    index: int
    period_start: date
    period_end: date
    check_date: date
    included_quarters: list[str]
    total_turnover: Decimal
    remaining_turnover_to_target: Decimal
    months_ok: bool
    quarters_ok: bool
    turnover_ok: bool
    qualification_ready: bool
    impossible_due_to_past_gaps: bool
    # внутреннее: label -> (trade_count, turnover)
    month_counts: dict[str, tuple[int, Decimal]] = field(default_factory=dict)
    quarter_counts: dict[str, tuple[int, Decimal]] = field(default_factory=dict)


@dataclass
class MonthPlan:
    month: str
    status: str               # done_ok | done_fail | future_required
    current_trade_count: int
    required_min_trade_count: int
    missing_trade_count: int
    current_turnover: Decimal
    suggested_turnover: Decimal


@dataclass
class QuarterPlan:
    quarter: str
    current_trade_count: int
    required_min_trade_count: int
    missing_trade_count: int
    current_turnover: Decimal
    suggested_turnover: Decimal
    status: str               # done_ok | done_fail | future_required


@dataclass
class KvalPlan:
    as_of: date
    target: Decimal
    effective_target: Decimal
    goal: Decimal
    target_mode: str
    windows: list[CandidateWindow]
    earliest: CandidateWindow | None
    earliest_reason: str
    monthly_plan: list[MonthPlan]
    quarterly_plan: list[QuarterPlan]
    generated_at: str
    disclaimer: str = DISCLAIMER


# ─── Вспомогательные функции ─────────────────────────────────────────────────


def _to_aware(d: date, end_of_day: bool = False) -> datetime:
    t = time.max if end_of_day else time.min
    return datetime.combine(d, t).replace(tzinfo=timezone.utc)


def _advance(q: Quarter, k: int) -> Quarter:
    for _ in range(k):
        q = q.next()
    return q


def _back(q: Quarter, k: int) -> Quarter:
    for _ in range(k):
        q = q.prev()
    return q


def _month_end(year: int, month: int) -> date:
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


def _months_between(start: date, end: date) -> list[str]:
    months: list[str] = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        months.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return months


def _parse_date(s: str) -> date | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
        except ValueError:
            return None


# ─── Планировщик ─────────────────────────────────────────────────────────────


class KvalPlanner:
    """Строит кандидатные окна и план выхода на квал-статус. Read-only."""

    def __init__(self, client: ReadOnlyClient | None = None, resolver=None) -> None:
        self.client = client or ReadOnlyClient()
        self.resolver = resolver  # резолвер инструментов планировщику не нужен

    def _collect_turnover_points(
        self, from_dt: datetime, to_dt: datetime
    ) -> list[tuple[date, Decimal]]:
        """Список (дата_сделки, оборот) по всем qualifying-операциям всех счетов."""
        points: list[tuple[date, Decimal]] = []
        accounts = self.client.get_broker_accounts()
        for acc in accounts:
            account_id = str(acc.get("id", ""))
            for op in self.client.get_operations(account_id, from_dt, to_dt):
                if not is_qualifying_operation(op):
                    continue
                result = calculate_operation_turnover(op, account_id, resolver=None)
                for t in result.trades:
                    d = _parse_date(t.date)
                    if d is not None:
                        points.append((d, t.turnover))
        return points

    def plan(
        self,
        as_of: date | None = None,
        horizon_quarters: int = 8,
        target_mode: str = "effective",
    ) -> KvalPlan:
        as_of = as_of or date.today()
        target = settings.kval_target
        effective_target = settings.effective_target
        goal = effective_target if target_mode == "effective" else target

        period0 = calculate_kval_period(as_of)
        last_q0 = period0.quarters[-1]

        # Кандидатные окна: текущее (k=0) + следующие horizon_quarters
        windows: list[CandidateWindow] = []
        skeletons: list[tuple[int, Quarter, list[Quarter]]] = []
        for k in range(horizon_quarters + 1):
            last_q = _advance(last_q0, k)
            quarters = [_back(last_q, WINDOW_QUARTERS - 1 - i) for i in range(WINDOW_QUARTERS)]
            skeletons.append((k, last_q, quarters))

        earliest_start = skeletons[0][2][0].start
        from_dt = _to_aware(earliest_start, end_of_day=False)
        to_dt = _to_aware(as_of, end_of_day=True)
        points = self._collect_turnover_points(from_dt, to_dt)

        logger.info(
            f"Планировщик: as_of={as_of}, окон={len(skeletons)}, "
            f"режим цели={target_mode} (goal={goal} ₽), точек оборота={len(points)}"
        )

        for k, last_q, quarters in skeletons:
            start = quarters[0].start
            end = quarters[-1].end
            check_date = last_q.next().start

            month_labels = _months_between(start, end)
            month_counts = {lbl: (0, Decimal("0")) for lbl in month_labels}
            quarter_counts = {q.label: (0, Decimal("0")) for q in quarters}
            total = Decimal("0")

            for d, turnover in points:
                if not (start <= d <= end):
                    continue
                total += turnover
                mk = f"{d.year:04d}-{d.month:02d}"
                if mk in month_counts:
                    c, s = month_counts[mk]
                    month_counts[mk] = (c + 1, s + turnover)
                ql = Quarter.from_date(d).label
                if ql in quarter_counts:
                    c, s = quarter_counts[ql]
                    quarter_counts[ql] = (c + 1, s + turnover)

            months_ok = all(c >= MONTH_MIN_TRADES for c, _ in month_counts.values())
            quarters_ok = all(c >= QUARTER_MIN_TRADES for c, _ in quarter_counts.values())
            turnover_ok = total >= goal
            qualification_ready = turnover_ok and months_ok and quarters_ok

            # Невосполнимые прошлые пропуски: «запертый» месяц/квартал с недобором
            impossible = False
            for lbl, (count, _) in month_counts.items():
                y, m = int(lbl[:4]), int(lbl[5:7])
                if _month_end(y, m) < as_of and count < MONTH_MIN_TRADES:
                    impossible = True
                    break
            if not impossible:
                for q in quarters:
                    count = quarter_counts[q.label][0]
                    if q.end < as_of and count < QUARTER_MIN_TRADES:
                        impossible = True
                        break

            windows.append(CandidateWindow(
                index=k, period_start=start, period_end=end, check_date=check_date,
                included_quarters=[q.label for q in quarters],
                total_turnover=total,
                remaining_turnover_to_target=max(Decimal("0"), goal - total),
                months_ok=months_ok, quarters_ok=quarters_ok, turnover_ok=turnover_ok,
                qualification_ready=qualification_ready,
                impossible_due_to_past_gaps=impossible,
                month_counts=month_counts, quarter_counts=quarter_counts,
            ))

        earliest, reason = self._earliest(windows)
        monthly_plan = self._monthly_plan(earliest, as_of, goal) if earliest else []
        quarterly_plan = self._quarterly_plan(earliest, as_of, goal) if earliest else []

        return KvalPlan(
            as_of=as_of, target=target, effective_target=effective_target,
            goal=goal, target_mode=target_mode, windows=windows,
            earliest=earliest, earliest_reason=reason,
            monthly_plan=monthly_plan, quarterly_plan=quarterly_plan,
            generated_at=datetime.now(timezone.utc).isoformat(),
        )

    @staticmethod
    def _earliest(windows: list[CandidateWindow]) -> tuple[CandidateWindow | None, str]:
        for w in windows:
            if w.impossible_due_to_past_gaps:
                continue
            if w.qualification_ready:
                return w, ("По текущей модели расчёта и данным API окно уже выполнено "
                           f"(проверка {w.check_date}).")
            return w, ("Ближайшее окно без невосполнимых прошлых пропусков "
                       f"(проверка {w.check_date}). Будущие месяцы и кварталы нужно "
                       "заполнить сделками по плану ниже.")
        return None, ("В пределах горизонта нет достижимого окна: в каждом есть "
                      "невосполнимые прошлые пропуски (пустые завершённые месяцы/кварталы).")

    @staticmethod
    def _status(count: int, required: int, locked: bool) -> str:
        if count >= required:
            return "done_ok"
        if locked:
            return "done_fail"
        return "future_required"

    def _monthly_plan(
        self, w: CandidateWindow, as_of: date, goal: Decimal
    ) -> list[MonthPlan]:
        labels = list(w.month_counts.keys())
        plannable = [
            lbl for lbl in labels
            if _month_end(int(lbl[:4]), int(lbl[5:7])) >= as_of
        ]
        remaining = max(Decimal("0"), goal - w.total_turnover)
        per_month = (remaining / len(plannable)) if plannable else Decimal("0")

        plan: list[MonthPlan] = []
        for lbl in labels:
            y, m = int(lbl[:4]), int(lbl[5:7])
            locked = _month_end(y, m) < as_of
            count, turnover = w.month_counts[lbl]
            missing = max(0, MONTH_MIN_TRADES - count)
            suggested = Decimal("0") if locked else per_month
            plan.append(MonthPlan(
                month=lbl,
                status=self._status(count, MONTH_MIN_TRADES, locked),
                current_trade_count=count,
                required_min_trade_count=MONTH_MIN_TRADES,
                missing_trade_count=missing,
                current_turnover=turnover,
                suggested_turnover=suggested,
            ))
        return plan

    def _quarterly_plan(
        self, w: CandidateWindow, as_of: date, goal: Decimal
    ) -> list[QuarterPlan]:
        quarters = [Quarter(int(lbl[:4]), int(lbl[5:])) for lbl in w.included_quarters]
        plannable = [q for q in quarters if q.end >= as_of]
        remaining = max(Decimal("0"), goal - w.total_turnover)
        per_quarter = (remaining / len(plannable)) if plannable else Decimal("0")

        plan: list[QuarterPlan] = []
        for q in quarters:
            locked = q.end < as_of
            count, turnover = w.quarter_counts[q.label]
            missing = max(0, QUARTER_MIN_TRADES - count)
            suggested = Decimal("0") if locked else per_quarter
            plan.append(QuarterPlan(
                quarter=q.label,
                current_trade_count=count,
                required_min_trade_count=QUARTER_MIN_TRADES,
                missing_trade_count=missing,
                current_turnover=turnover,
                suggested_turnover=suggested,
                status=self._status(count, QUARTER_MIN_TRADES, locked),
            ))
        return plan


def plan(as_of: date | None = None, horizon_quarters: int = 8,
         target_mode: str = "effective") -> KvalPlan:
    """Удобная функция-обёртка."""
    return KvalPlanner().plan(as_of, horizon_quarters, target_mode)
