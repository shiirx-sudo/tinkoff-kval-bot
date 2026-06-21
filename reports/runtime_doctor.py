"""
Runtime doctor: проверка окружения и конфигурации перед запуском.
Идея перенесена из MOEX Advisor (reports/runtime_doctor.py,
validate_existing_reports.py), упрощена и без pandas.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from reports.output_contract import REPORT_COLUMN_ORDER


@dataclass
class DoctorReport:
    checks: list[tuple[str, str, str]] = field(default_factory=list)  # (name, status, detail)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append((name, "ok" if ok else "fail", detail))

    @property
    def ok(self) -> bool:
        return all(status == "ok" for _, status, _ in self.checks)


def _load_dotenv_safe() -> None:
    """Подхватывает .env для diagnostics, не перетирая уже заданные OS env.

    override=False — явно заданный shell env приоритетнее .env. Если
    python-dotenv недоступен, продолжаем diagnostics с текущим окружением,
    не падая. Значение токена при этом нигде не печатается.
    """
    try:
        from dotenv import find_dotenv, load_dotenv
    except ImportError:
        return
    load_dotenv(find_dotenv(usecwd=True), override=False)


def run_doctor() -> DoctorReport:
    """Проверяет токен, режим LIVE и доступность зависимостей."""
    rep = DoctorReport()

    _load_dotenv_safe()

    token = (os.getenv("TINKOFF_READ_TOKEN") or os.getenv("TINKOFF_TOKEN") or "").strip()
    rep.add("token_present", bool(token),
            "TINKOFF_READ_TOKEN задан" if token else "нет TINKOFF_READ_TOKEN/TINKOFF_TOKEN")

    live = (os.getenv("LIVE_ENABLED", "false").strip().lower() == "true")
    rep.add("live_disabled", not live,
            "LIVE_ENABLED=false (read-only)" if not live else "LIVE_ENABLED=true запрещён на этапе 1")

    try:
        import requests  # noqa: F401
        rep.add("requests_available", True, "")
    except ImportError:
        rep.add("requests_available", False, "pip install requests")

    return rep


def validate_existing_reports(reports_dir: str | Path) -> list[tuple[str, str]]:
    """
    Проверяет, что у созданных CSV-отчётов корректный заголовок по контракту.
    Возвращает список (file, status).
    """
    out = Path(reports_dir)
    results: list[tuple[str, str]] = []
    for name, columns in REPORT_COLUMN_ORDER.items():
        path = out / f"{name}.csv"
        if not path.exists():
            results.append((path.name, "missing"))
            continue
        header = path.read_text(encoding="utf-8-sig").splitlines()[:1]
        header_cols = header[0].split(";") if header else []
        results.append((path.name, "ok" if header_cols == columns else "header_mismatch"))
    return results
