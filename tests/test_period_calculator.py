"""Тесты расчёта квалификационного периода (чистая логика дат)."""
from __future__ import annotations

from datetime import date

import pytest

from modules.period_calculator import (
    Quarter,
    calculate_kval_period,
)


class TestQuarter:
    def test_start_end_q1(self):
        q = Quarter(2025, 1)
        assert q.start == date(2025, 1, 1)
        assert q.end == date(2025, 3, 31)

    def test_start_end_q4(self):
        q = Quarter(2025, 4)
        assert q.start == date(2025, 10, 1)
        assert q.end == date(2025, 12, 31)

    def test_label(self):
        assert Quarter(2026, 2).label == "2026Q2"

    def test_from_date(self):
        assert Quarter.from_date(date(2026, 6, 11)) == Quarter(2026, 2)
        assert Quarter.from_date(date(2026, 1, 1)) == Quarter(2026, 1)
        assert Quarter.from_date(date(2026, 12, 31)) == Quarter(2026, 4)

    def test_prev_next_wrap(self):
        assert Quarter(2026, 1).prev() == Quarter(2025, 4)
        assert Quarter(2025, 4).next() == Quarter(2026, 1)

    def test_is_complete(self):
        q = Quarter(2025, 4)  # end 2025-12-31
        assert q.is_complete(date(2026, 1, 1)) is True
        assert q.is_complete(date(2025, 12, 31)) is False

    def test_contains(self):
        q = Quarter(2025, 2)
        assert q.contains(date(2025, 5, 15)) is True
        assert q.contains(date(2025, 7, 1)) is False


class TestCalculateKvalPeriod:
    @pytest.mark.parametrize(
        "as_of, start, end, current",
        [
            # ВНИМАНИЕ: значения отражают ФАКТИЧЕСКОЕ поведение кода
            # Правило периода зафиксировано: вариант A —
            # official_completed_quarters (4 завершённых квартала,
            # предшествующих текущему незавершённому).
            (date(2026, 6, 11), date(2025, 4, 1), date(2026, 3, 31), "2026Q2"),
            (date(2026, 1, 15), date(2025, 1, 1), date(2025, 12, 31), "2026Q1"),
            (date(2026, 3, 31), date(2025, 1, 1), date(2025, 12, 31), "2026Q1"),
            (date(2026, 4, 1), date(2025, 4, 1), date(2026, 3, 31), "2026Q2"),
            (date(2026, 7, 1), date(2025, 7, 1), date(2026, 6, 30), "2026Q3"),
        ],
    )
    def test_actual_behavior(self, as_of, start, end, current):
        p = calculate_kval_period(as_of)
        assert p.start == start
        assert p.end == end
        assert p.current_quarter.label == current

    def test_just_closed_quarter_enters_next_day(self):
        # 2026-06-30: 2026Q2 ещё идёт; 2026-07-01: уже закрылся и вошёл
        assert calculate_kval_period(date(2026, 6, 30)).quarters[-1].label == "2026Q1"
        assert calculate_kval_period(date(2026, 7, 1)).quarters[-1].label == "2026Q2"

    def test_current_quarter_excluded_2026_06_11(self):
        p = calculate_kval_period(date(2026, 6, 11))
        assert "2026Q2" not in [q.label for q in p.quarters]
        assert p.current_quarter.label == "2026Q2"

    def test_just_closed_quarter_included_2026_07_01(self):
        p = calculate_kval_period(date(2026, 7, 1))
        assert "2026Q2" in [q.label for q in p.quarters]
        assert (p.start, p.end) == (date(2025, 7, 1), date(2026, 6, 30))

    def test_always_four_quarters(self):
        p = calculate_kval_period(date(2026, 6, 11))
        assert len(p.quarters) == 4

    def test_quarters_chronological(self):
        p = calculate_kval_period(date(2026, 6, 11))
        labels = [q.label for q in p.quarters]
        assert labels == ["2025Q2", "2025Q3", "2025Q4", "2026Q1"]

    def test_all_quarters_complete(self):
        as_of = date(2026, 6, 11)
        p = calculate_kval_period(as_of)
        for q in p.quarters:
            assert q.is_complete(as_of)

    def test_period_contains(self):
        p = calculate_kval_period(date(2026, 6, 11))
        assert p.contains(date(2025, 12, 31)) is True
        assert p.contains(date(2026, 4, 1)) is False
