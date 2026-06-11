"""Тесты контракта отчётов и их записи."""
from __future__ import annotations

from datetime import date

from modules.kval_tracker import KvalTracker
from reports import kval_reports
from reports.output_contract import REPORT_COLUMN_ORDER
from reports.runtime_doctor import validate_existing_reports
from tests.conftest import make_account, make_operation, make_trade


class FakeClient:
    def get_broker_accounts(self):
        return [make_account("acc-1", "Основной")]

    def get_operations(self, account_id, from_dt, to_dt):
        return [make_operation("OPERATION_TYPE_BUY", id="a",
                               date="2025-05-10T10:00:00Z",
                               trades=[make_trade("1000", 10)])]


def _progress():
    return KvalTracker(client=FakeClient()).analyze(as_of=date(2026, 6, 11))


def test_write_all_creates_files(tmp_path):
    written = kval_reports.write_all(_progress(), tmp_path)
    for name in ("kval_progress.json", "kval_progress.csv", "kval_accounts.csv",
                 "kval_trades.csv", "broker_sync_status.csv"):
        assert name in written
        assert written[name].exists()


def test_csv_headers_match_contract(tmp_path):
    kval_reports.write_all(_progress(), tmp_path)
    for name, columns in REPORT_COLUMN_ORDER.items():
        path = tmp_path / f"{name}.csv"
        header = path.read_text(encoding="utf-8-sig").splitlines()[0].split(";")
        assert header == columns, f"{name}: {header}"


def test_validate_existing_reports_ok(tmp_path):
    kval_reports.write_all(_progress(), tmp_path)
    results = dict(validate_existing_reports(tmp_path))
    assert all(status == "ok" for status in results.values()), results


def test_accounts_masked(tmp_path):
    kval_reports.write_all(_progress(), tmp_path)
    content = (tmp_path / "kval_accounts.csv").read_text(encoding="utf-8-sig")
    assert "***" in content  # account id маскируется
