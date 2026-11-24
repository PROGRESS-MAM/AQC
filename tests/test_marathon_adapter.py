import json

import pytest

from marathon import marathon_adapter as adapter


@pytest.fixture(autouse=True)
def set_adapter_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(
        adapter,
        "_settings",
        adapter.Settings(tmp_path, ".marathon", "FTP", 1, 1, tmp_path / "adapter.log", {"x": "1"}),
    )


def test_version_and_ftp_stage():
    assert adapter.app_version == "1.4.0"
    assert adapter.stage_folders == {"Restore": "restore", "Transcode": "transcode", "QC": "qc", "FTP": "ftp"}


def test_report_with_ftp_fields(tmp_path):
    adapter._write_report(tmp_path, "job1", adapter.JobReport("ok", "", "ftp_ton", ftp_file="a.mp4", size=12), "W")

    report = json.loads((tmp_path / "job1.json").read_text(encoding="utf-8"))
    assert report["ftp_file"] == "a.mp4"
    assert report["size"] == 12
    assert "items" not in report
    assert "new_master" not in report


def test_report_without_ftp_fields(tmp_path):
    adapter._write_report(tmp_path, "job1", adapter.JobReport("ok", "", "p"), "W")

    report = json.loads((tmp_path / "job1.json").read_text(encoding="utf-8"))
    assert "ftp_file" not in report
    assert "size" not in report


@pytest.mark.parametrize(
    "fields",
    [
        {"size": True},
        {"size": "12"},
        {"size": -1},
        {"ftp_file": 5},
    ],
)
def test_checked_rejects_bad_ftp_fields(fields):
    report = adapter._checked(adapter.JobReport("ok", "", "p", **fields))

    assert report.status == "failed"
    assert report.result.startswith("Ungültiges Ergebnis")


def test_checked_keeps_fields_on_missing_result():
    report = adapter._checked(adapter.JobReport("rejected", "", "p", ftp_file="a", size=1))

    assert report.result == "ohne Angabe"
    assert report.ftp_file == "a"
    assert report.size == 1
