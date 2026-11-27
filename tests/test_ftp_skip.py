import csv
import functools
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from marathon import ftp_marathon


@pytest.fixture
def worker(tmp_path, monkeypatch):
    source = tmp_path / "source.wav"
    source.write_bytes(b"source")
    ftp_dir = tmp_path / "ftp"
    ftp_dir.mkdir()
    calls = []
    statuses = []
    logs = []
    monkeypatch.setattr(ftp_marathon.adapter, "share_path", lambda path: tmp_path / path)
    monkeypatch.setattr(ftp_marathon.adapter, "log", logs.append)
    monkeypatch.setattr(ftp_marathon.adapter, "status", statuses.append)

    def process(sources, tools, output_folder, on_progress=None):
        calls.append((sources, tools, output_folder, on_progress))
        return SimpleNamespace(state="ok", summary="Prüfung bestanden")

    monkeypatch.setattr(ftp_marathon.qc_worker, "process", process)
    job = {"input": source.name, "size": source.stat().st_size,
           "ftp_dir": str(ftp_dir), "ftp_name": "export.wav",
           "clip_id": "42", "identifier": "ID42"}
    return SimpleNamespace(tmp_path=tmp_path, source=source, ftp_dir=ftp_dir,
                           calls=calls, statuses=statuses, logs=logs, job=job)


def _write_vorab(folder, rows, reports=None):
    folder.mkdir(parents=True, exist_ok=True)
    reports_folder = folder / "berichte"
    reports_folder.mkdir(exist_ok=True)
    csv_path = folder / "vorab.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ftp_marathon.csv_columns, delimiter=";")
        writer.writeheader()
        writer.writerows(rows)
    for name, data in (reports or {}).items():
        (reports_folder / name).write_text(json.dumps(data), encoding="utf-8")
    return csv_path


def _row(clip_id, status="ok", report_name=None):
    return {"clip_id": str(clip_id), "status": status,
            "bericht": f"C:\\alt\\vorab_1\\berichte\\{report_name}" if report_name else ""}


def _report(source, *, state="ok", passed=True, path=None, tools=None):
    return {"state": state, "passed": passed, "summary": "Vorab bestanden",
            "path": str(path if path is not None else source),
            "tools": tools if tools is not None else [
                {"kind": "audible_sound"}, {"kind": "sound_quality"}]}


def _successful_vorab(worker, *, clip_id="42", report_name="42.wav.aqc.json", report=None):
    folder = worker.tmp_path / "vorab_1"
    _write_vorab(folder, [_row(clip_id, report_name=report_name)],
                 {report_name: report if report is not None else _report(worker.source)})
    return ftp_marathon.load_vorab(folder)


def test_load_vorab(tmp_path):
    folder = tmp_path / "vorab_1"
    csv_path = _write_vorab(folder, [
        _row("1", report_name="1.wav.aqc.json"),
        _row("2", "rejected", "2.wav.aqc.json"),
        _row("3", "error", "3.wav.aqc.json"),
        _row("4", report_name=None),
        _row("5", report_name="5.wav.aqc.json"),
        _row("5", "rejected", "5.wav.aqc.json"),
    ])

    expected = {"1": folder / "berichte" / "1.wav.aqc.json"}
    assert ftp_marathon.load_vorab(folder) == expected
    assert ftp_marathon.load_vorab(csv_path) == expected


def test_load_vorab_errors(tmp_path):
    with pytest.raises(FileNotFoundError, match="vorab.csv nicht gefunden"):
        ftp_marathon.load_vorab(tmp_path / "missing")

    folder = tmp_path / "bad"
    folder.mkdir()
    (folder / "vorab.csv").write_text("clip_id;status\n42;ok\n", encoding="utf-8-sig")
    with pytest.raises(ValueError, match="keine Vorab-Liste"):
        ftp_marathon.load_vorab(folder)


def test_skip_copies_without_check(worker, tmp_path):
    vorab = _successful_vorab(worker)
    report_path = vorab["42"]
    os.utime(report_path, (worker.source.stat().st_mtime + 2, worker.source.stat().st_mtime + 2))
    output_folder = tmp_path / "output"
    output_folder.mkdir()

    result = ftp_marathon.run_job(worker.job, output_folder, tmp_path / "worker.log", vorab)

    target = worker.ftp_dir / worker.job["ftp_name"]
    assert result.status == "ok"
    assert result.result.startswith("Tonprüfung aus Vorab-Prüfung übernommen: ")
    assert worker.calls == []
    assert target.read_bytes() == worker.source.read_bytes()
    assert (output_folder / report_path.name).read_bytes() == report_path.read_bytes()
    assert result.ftp_file == worker.job["ftp_name"]
    assert result.size == worker.job["size"]
    assert any("Tonprüfung aus Vorab-Prüfung übernommen:" in message for message in worker.logs)


@pytest.mark.parametrize("case", [
    "rejected", "passed_false", "other_path", "other_tools", "source_newer",
    "missing_report", "bad_json", "missing_clip",
])
def test_no_skip(worker, tmp_path, case):
    report_data = _report(worker.source)
    vorab = _successful_vorab(worker, report=report_data)
    report_path = vorab["42"]
    if case == "rejected":
        report_data["state"] = "rejected"
    elif case == "passed_false":
        report_data["passed"] = False
    elif case == "other_path":
        report_data["path"] = str(worker.tmp_path / "other.wav")
    elif case == "other_tools":
        report_data["tools"] = [{"kind": "audible_sound"}]
    elif case == "source_newer":
        os.utime(report_path, (worker.source.stat().st_mtime - 10, worker.source.stat().st_mtime - 10))
        os.utime(worker.source, (worker.source.stat().st_mtime + 10, worker.source.stat().st_mtime + 10))
    elif case == "missing_report":
        report_path.unlink()
    elif case == "bad_json":
        report_path.write_text("{broken", encoding="utf-8")
    elif case == "missing_clip":
        vorab = {"other": report_path}
    if case not in {"missing_report", "bad_json"}:
        report_path.write_text(json.dumps(report_data), encoding="utf-8")
    output_folder = tmp_path / "output"
    output_folder.mkdir()

    result = ftp_marathon.run_job(worker.job, output_folder, tmp_path / "worker.log", vorab)

    assert result.status == "ok"
    assert result.result == "Prüfung bestanden"
    assert len(worker.calls) == 1
    assert (worker.ftp_dir / worker.job["ftp_name"]).read_bytes() == worker.source.read_bytes()
    if case != "missing_clip":
        assert any("Vorab-Ergebnis nicht verwendet:" in message for message in worker.logs)


def test_skip_size_mismatch(worker, tmp_path):
    vorab = _successful_vorab(worker)
    worker.job["size"] += 1
    output_folder = tmp_path / "output"
    output_folder.mkdir()

    result = ftp_marathon.run_job(worker.job, output_folder, tmp_path / "worker.log", vorab)

    assert result.status == "failed"
    assert "Bytes statt" in result.result
    assert worker.calls == []
    assert not (worker.ftp_dir / worker.job["ftp_name"]).exists()
    assert not (output_folder / vorab["42"].name).exists()


def test_skip_report_copy_fails(worker, tmp_path, monkeypatch):
    vorab = _successful_vorab(worker)
    output_folder = tmp_path / "output"
    output_folder.mkdir()
    def fail_copy(*args, **kwargs):
        raise OSError("nicht schreibbar")

    monkeypatch.setattr(ftp_marathon.shutil, "copy2", fail_copy)

    result = ftp_marathon.run_job(worker.job, output_folder, tmp_path / "worker.log", vorab)

    assert result.status == "failed"
    assert "Vorab-Bericht nicht kopierbar" in result.result
    assert worker.calls == []
    assert not (worker.ftp_dir / worker.job["ftp_name"]).exists()


def test_ask_vorab(tmp_path, monkeypatch):
    messages = []
    monkeypatch.setattr(ftp_marathon, "_say", messages.append)
    folder = tmp_path / "vorab_1"
    _write_vorab(folder, [_row("42", report_name="42.wav.aqc.json")])
    answers = iter([str(tmp_path / "missing"), f'"{folder}"'])
    asked = []

    result = ftp_marathon._ask_vorab(lambda prompt: (asked.append(prompt), next(answers))[1])

    assert result == {"42": folder / "berichte" / "42.wav.aqc.json"}
    assert len(asked) == 2
    assert "Vorab-Ergebnis: 1 bestandene Clips" in messages[-1]
    assert ftp_marathon._ask_vorab(lambda prompt: "") == {}
    assert ftp_marathon._ask_vorab(lambda prompt: (_ for _ in ()).throw(EOFError())) == {}


def test_main_production_passes_vorab(monkeypatch):
    expected = {"42": Path("x")}
    captured = []
    monkeypatch.setattr(ftp_marathon, "_say", lambda message: None)
    monkeypatch.setattr(ftp_marathon, "_check_plan", lambda: None)
    monkeypatch.setattr(ftp_marathon, "_ask_mode", lambda: "produktion")
    monkeypatch.setattr(ftp_marathon, "_ask_vorab", lambda: expected)
    monkeypatch.setattr(ftp_marathon, "_run_production", captured.append)

    ftp_marathon.main()

    assert captured == [expected]


def test_run_production_with_vorab(monkeypatch):
    captured = {}
    vorab = {"42": Path("x")}
    monkeypatch.setattr(ftp_marathon.adapter, "run",
                        lambda settings, run_job: captured.update(settings=settings, run_job=run_job))

    ftp_marathon._run_production(vorab)

    assert isinstance(captured["run_job"], functools.partial)
    assert captured["run_job"].func is ftp_marathon.run_job
    assert captured["run_job"].keywords == {"vorab": vorab}
