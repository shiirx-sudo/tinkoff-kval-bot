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


def test_console_report_masks_account_id():
    from rich.console import Console
    import io
    from reports import console_report

    class FC:
        def get_broker_accounts(self):
            return [make_account("2000123456", "Основной")]
        def get_operations(self, account_id, from_dt, to_dt):
            return [make_operation("OPERATION_TYPE_BUY", id="a",
                                   date="2025-05-10T10:00:00Z",
                                   trades=[make_trade("1000", 10)])]

    p = KvalTracker(client=FC()).analyze(as_of=date(2026, 6, 11))

    buf = io.StringIO()
    console_report._console = Console(file=buf, width=240, force_terminal=False)
    console_report.render(p)
    out = buf.getvalue()

    assert "2000123456" not in out      # полный id не светится
    assert "***3456" in out             # показан замаскированный


def test_kval_months_csv_written(tmp_path):
    from reports import kval_reports
    p = KvalTracker(client=FakeClient()).analyze(as_of=date(2026, 6, 11))
    written = kval_reports.write_all(p, tmp_path)
    assert "kval_months.csv" in written
    assert written["kval_months.csv"].exists()
    header = (tmp_path / "kval_months.csv").read_text(encoding="utf-8-sig").splitlines()[0]
    assert header.split(";") == ["date", "month", "operation_count", "trade_count", "turnover", "status"]


def test_kval_progress_json_has_status_lists(tmp_path):
    import json
    from reports import kval_reports
    p = KvalTracker(client=FakeClient()).analyze(as_of=date(2026, 6, 11))
    kval_reports.write_all(p, tmp_path)
    data = json.loads((tmp_path / "kval_progress.json").read_text(encoding="utf-8"))
    assert "monthly_status" in data and len(data["monthly_status"]) == 12
    assert "quarterly_status" in data and len(data["quarterly_status"]) == 4
    assert "qualification_ready" in data


def test_kval_trades_has_instrument_columns(tmp_path):
    from reports import kval_reports
    p = KvalTracker(client=FakeClient()).analyze(as_of=date(2026, 6, 11))
    kval_reports.write_all(p, tmp_path)
    header = (tmp_path / "kval_trades.csv").read_text(encoding="utf-8-sig").splitlines()[0].split(";")
    assert "instrument_uid" in header
    assert "instrument_name" in header
    assert "instrument_type" in header


def test_raw_operations_jsonl_written_and_masked(tmp_path):
    from reports import kval_reports
    p = KvalTracker(client=FakeClient()).analyze(as_of=date(2026, 6, 11))
    written = kval_reports.write_all(p, tmp_path)
    assert "kval_operations_raw.jsonl" in written
    content = (tmp_path / "kval_operations_raw.jsonl").read_text(encoding="utf-8")
    assert "account_id_masked" in content
    assert content.strip()  # есть хотя бы одна строка
