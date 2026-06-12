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


def _write_planner_reports(tmp_path):
    from modules.kval_planner import KvalPlanner
    from reports import kval_plan_reports

    class _FC:
        def get_broker_accounts(self):
            return [{"id": "acc-1", "name": "A", "type": "ACCOUNT_TYPE_TINKOFF"}]
        def get_operations(self, account_id, from_dt, to_dt):
            return []

    plan = KvalPlanner(client=_FC()).plan(as_of=date(2026, 7, 1))
    return kval_plan_reports.write_all(plan, tmp_path)


def _write_scanner_reports(tmp_path):
    from datetime import date
    from modules.instrument_scanner import Candidate, InstrumentScanner
    from reports import instrument_scan_reports

    class _SC:
        def find_instrument(self, ticker, class_code):
            return {"figi": "FG", "uid": "u", "name": ticker, "lot": 1,
                    "currency": "rub", "instrumentType": "share", "classCode": "TQBR"}
        def get_trading_status(self, instrument_id):
            return {"tradingStatus": "SECURITY_TRADING_STATUS_NORMAL_TRADING"}
        def get_last_price(self, instrument_id):
            return None
        def get_order_book(self, instrument_id, depth):
            return {"bids": [{"price": {"units": "100", "nano": 0}, "quantity": "2000"}],
                    "asks": [{"price": {"units": "100", "nano": 20000000}, "quantity": "2000"}]}

    rep = InstrumentScanner(client=_SC()).scan(
        [Candidate("TMON", "TQBR")], as_of=date(2026, 7, 1))
    return instrument_scan_reports.write_all(rep, tmp_path)


def test_csv_headers_match_contract(tmp_path):
    kval_reports.write_all(_progress(), tmp_path)
    _write_planner_reports(tmp_path)
    _write_scanner_reports(tmp_path)
    for name, columns in REPORT_COLUMN_ORDER.items():
        path = tmp_path / f"{name}.csv"
        header = path.read_text(encoding="utf-8-sig").splitlines()[0].split(";")
        assert header == columns, f"{name}: {header}"


def test_validate_existing_reports_ok(tmp_path):
    kval_reports.write_all(_progress(), tmp_path)
    _write_planner_reports(tmp_path)
    _write_scanner_reports(tmp_path)
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


def test_raw_export_masks_broker_account_id(tmp_path):
    from reports import kval_reports

    class FCBroker:
        def get_broker_accounts(self):
            return [make_account("2000123456", "Основной")]
        def get_operations(self, account_id, from_dt, to_dt):
            return [{
                "id": "a", "operationType": "OPERATION_TYPE_BUY",
                "figi": "BBG1", "instrumentUid": "uid-1", "instrumentType": "share",
                "date": "2025-05-10T10:00:00Z", "brokerAccountId": "2000123456",
                "tradesInfo": {"trades": [
                    {"num": "1", "quantity": "1", "price": {"units": "100", "nano": 0}},
                ]},
            }]

    p = KvalTracker(client=FCBroker()).analyze(as_of=date(2026, 6, 11))
    kval_reports.write_all(p, tmp_path)
    content = (tmp_path / "kval_operations_raw.jsonl").read_text(encoding="utf-8")
    assert "2000123456" not in content          # полный brokerAccountId не утёк
    assert "account_id_masked" in content
    assert "***3456" in content                 # маскированное значение присутствует


def test_progress_reports_have_period_policy(tmp_path):
    import json
    kval_reports.write_all(_progress(), tmp_path)
    data = json.loads((tmp_path / "kval_progress.json").read_text(encoding="utf-8"))
    assert data["period_policy"] == "official_completed_quarters"
    assert data["period_kind"] == "official_fact"
    assert data["current_quarter_included"] is False
    assert data["note"] == "Only four completed calendar quarters are included."
    assert "as_of" in data
    header = (tmp_path / "kval_progress.csv").read_text(
        encoding="utf-8-sig").splitlines()[0].split(";")
    assert "period_policy" in header and "period_kind" in header
