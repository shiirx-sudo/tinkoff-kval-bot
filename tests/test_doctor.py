"""Тесты runtime doctor."""
from __future__ import annotations

import os

from reports.runtime_doctor import run_doctor


def test_doctor_ok_with_token():
    os.environ["TINKOFF_READ_TOKEN"] = "x"
    os.environ["LIVE_ENABLED"] = "false"
    rep = run_doctor()
    assert rep.ok is True


def test_doctor_flags_live_enabled():
    os.environ["LIVE_ENABLED"] = "true"
    try:
        rep = run_doctor()
        statuses = {name: status for name, status, _ in rep.checks}
        assert statuses["live_disabled"] == "fail"
        assert rep.ok is False
    finally:
        os.environ["LIVE_ENABLED"] = "false"


def _statuses(rep):
    return {name: status for name, status, _ in rep.checks}


def _details(rep):
    return {name: detail for name, status, detail in rep.checks}


def test_doctor_reads_token_from_dotenv(tmp_path, monkeypatch):
    """.env содержит токен, shell env пуст → doctor видит token_present."""
    monkeypatch.delenv("TINKOFF_READ_TOKEN", raising=False)
    monkeypatch.delenv("TINKOFF_TOKEN", raising=False)
    monkeypatch.setenv("LIVE_ENABLED", "false")
    (tmp_path / ".env").write_text(
        "TINKOFF_READ_TOKEN=dotenv-token-value\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    rep = run_doctor()

    assert _statuses(rep)["token_present"] == "ok"


def test_doctor_dotenv_does_not_override_shell_env(tmp_path, monkeypatch):
    """shell env задан, .env содержит другое значение → override=False."""
    monkeypatch.setenv("TINKOFF_READ_TOKEN", "shell-token-value")
    monkeypatch.delenv("TINKOFF_TOKEN", raising=False)
    monkeypatch.setenv("LIVE_ENABLED", "false")
    (tmp_path / ".env").write_text(
        "TINKOFF_READ_TOKEN=dotenv-token-value\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    rep = run_doctor()

    assert _statuses(rep)["token_present"] == "ok"
    # shell env не должен быть перетёрт значением из .env
    assert os.environ["TINKOFF_READ_TOKEN"] == "shell-token-value"


def test_doctor_reports_absent_without_token(tmp_path, monkeypatch):
    """Токена нет нигде → doctor показывает absent, не падает."""
    monkeypatch.delenv("TINKOFF_READ_TOKEN", raising=False)
    monkeypatch.delenv("TINKOFF_TOKEN", raising=False)
    monkeypatch.setenv("LIVE_ENABLED", "false")
    monkeypatch.chdir(tmp_path)  # пустой каталог без .env

    rep = run_doctor()

    assert _statuses(rep)["token_present"] == "fail"


def test_doctor_does_not_print_token_value(tmp_path, monkeypatch):
    """Значение токена не должно попадать в detail-вывод doctor."""
    secret = "super-secret-token-value-12345"
    monkeypatch.setenv("TINKOFF_READ_TOKEN", secret)
    monkeypatch.setenv("LIVE_ENABLED", "false")
    monkeypatch.chdir(tmp_path)

    rep = run_doctor()

    for _name, _status, detail in rep.checks:
        assert secret not in detail


def test_doctor_reports_live_disabled(monkeypatch):
    monkeypatch.setenv("TINKOFF_READ_TOKEN", "x")
    monkeypatch.setenv("LIVE_ENABLED", "false")

    rep = run_doctor()

    assert _statuses(rep)["live_disabled"] == "ok"
