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
