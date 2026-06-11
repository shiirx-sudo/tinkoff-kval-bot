"""
Расчёт периода для квалификационного оборота.

Правило Т-Инвестиций:
  Период = последние 4 завершённых квартала.

Примеры:
  Дата 2026-06-11 → период 2025-04-01 … 2026-03-31
  Дата 2026-01-15 → период 2024-10-01 … 2025-12-31  (ещё нет Q1-2026)
  Дата 2026-03-31 → период 2024-10-01 … 2025-12-31  (Q1-2026 ещё не завершён)
  Дата 2026-04-01 → период 2025-01-01 … 2025-12-31  (Q1-2026 только завершился)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Quarter:
    year: int
    number: int  # 1-4

    @property
    def start(self) -> date:
        month = (self.number - 1) * 3 + 1
        return date(self.year, month, 1)

    @property
    def end(self) -> date:
        month = self.number * 3
        if month == 12:
            return date(self.year, 12, 31)
        # Последний день последнего месяца квартала
        next_month_first = date(self.year, month + 1, 1)
        from datetime import timedelta
        return next_month_first - timedelta(days=1)

    @property
    def label(self) -> str:
        return f"{self.year}Q{self.number}"

    def prev(self) -> "Quarter":
        if self.number == 1:
            return Quarter(self.year - 1, 4)
        return Quarter(self.year, self.number - 1)

    def next(self) -> "Quarter":
        if self.number == 4:
            return Quarter(self.year + 1, 1)
        return Quarter(self.year, self.number + 1)

    def is_complete(self, as_of: date) -> bool:
        """Квартал завершён, если его последний день строго раньше as_of."""
        return self.end < as_of

    def contains(self, d: date) -> bool:
        return self.start <= d <= self.end

    @staticmethod
    def from_date(d: date) -> "Quarter":
        """Квартал, которому принадлежит дата d."""
        return Quarter(d.year, (d.month - 1) // 3 + 1)


@dataclass(frozen=True)
class KvalPeriod:
    """
    Расчётный период для квалификационного оборота.
    Всегда = ровно 4 последних завершённых квартала.
    """
    quarters: tuple[Quarter, ...]       # ровно 4 завершённых (хронологически)
    current_quarter: Quarter            # незавершённый квартал (текущий)
    as_of_date: date                    # дата расчёта

    @property
    def start(self) -> date:
        return self.quarters[0].start

    @property
    def end(self) -> date:
        return self.quarters[-1].end

    def contains(self, d: date) -> bool:
        return self.start <= d <= self.end


def calculate_kval_period(as_of: date | None = None) -> KvalPeriod:
    """
    Вычисляет расчётный период.

    Parameters
    ----------
    as_of : date, optional
        Дата «на которую считаем». По умолчанию — сегодня.

    Returns
    -------
    KvalPeriod
        Период из ровно 4 завершённых кварталов + текущий незавершённый.

    Examples
    --------
    >>> from datetime import date
    >>> p = calculate_kval_period(date(2026, 6, 11))
    >>> p.start
    datetime.date(2025, 4, 1)
    >>> p.end
    datetime.date(2026, 3, 31)
    >>> p.current_quarter.label
    '2026Q2'
    """
    if as_of is None:
        as_of = date.today()

    current_q = Quarter.from_date(as_of)

    # Убеждаемся, что current_q действительно не завершён
    # (на первый день квартала — он уже текущий и незавершён)
    if current_q.is_complete(as_of):
        # Теоретически невозможно: квартал содержит as_of,
        # значит его end >= as_of. Но если as_of == end,
        # квартал ещё не завершён (end < as_of → False).
        # Оставляем как защитный код.
        current_q = current_q.next()

    # Берём 4 квартала перед текущим (они точно завершены)
    completed: list[Quarter] = []
    q = current_q.prev()
    for _ in range(4):
        assert q.is_complete(as_of), (
            f"Квартал {q.label} должен быть завершён к {as_of}"
        )
        completed.append(q)
        q = q.prev()

    # completed сейчас в обратном порядке → разворачиваем
    completed.reverse()  # [oldest, ..., newest]

    return KvalPeriod(
        quarters=tuple(completed),
        current_quarter=current_q,
        as_of_date=as_of,
    )
