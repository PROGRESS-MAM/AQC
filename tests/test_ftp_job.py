import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from marathon import ftp_marathon
from tests.audio_signals import speech_like, write_wav


@pytest.fixture
def worker(tmp_path, monkeypatch):
    source = tmp_path / "source.wav"
    source.write_bytes(b"source")
    ftp_dir = tmp_path / "ftp"
    ftp_dir.mkdir()
    calls = []
    statuses = []
    monkeypatch.setattr(ftp_marathon.adapter, "share_path", lambda path: tmp_path / path)
    monkeypatch.setattr(ftp_marathon.adapter, "log", lambda message: None)
    monkeypatch.setattr(ftp_marathon.adapter, "status", statuses.append)

    def process(sources, tools, output_folder, on_progress=None):
        calls.append((sources, tools, output_folder, on_progress))
        return SimpleNamespace(state="ok", summary="Prüfung bestanden")

    real_process = ftp_marathon.qc_worker.process
    monkeypatch.setattr(ftp_marathon.qc_worker, "process", process)
    job = {"input": source.name, "size": source.stat().st_size,
           "ftp_dir": str(ftp_dir), "ftp_name": "export.wav",
           "clip_id": "42", "identifier": "ID42"}
    return SimpleNamespace(tmp_path=tmp_path, source=source, ftp_dir=ftp_dir,
                           calls=calls, statuses=statuses, job=job, real_process=real_process)


@pytest.mark.parametrize(("field", "value", "remove", "problem"), [
    ("input", None, True, "input fehlt"),
    ("size", "12", False, "size fehlt oder ist keine positive Ganzzahl"),
    ("size", True, False, "size fehlt oder ist keine positive Ganzzahl"),
    ("size", 0, False, "size fehlt oder ist keine positive Ganzzahl"),
    ("ftp_dir", "ftp", False, "ftp_dir fehlt oder ist kein vollständiger Pfad"),
    ("ftp_name", "a/b.mp4", False, "ftp_name fehlt oder ist kein gültiger Dateiname"),
    ("ftp_name", "", False, "ftp_name fehlt oder ist kein gültiger Dateiname"),
])
def test_job_problems(worker, field, value, remove, problem):
    job = dict(worker.job)
    if remove:
        job.pop(field)
    else:
        job[field] = value

    report = ftp_marathon.run_job(job, worker.tmp_path / "reports", worker.tmp_path / "worker.log")

    assert report.status == "failed"
    assert report.result.startswith(f"Job ungültig: {problem}")
    assert worker.calls == []
    assert not (worker.ftp_dir / worker.job["ftp_name"]).exists()


def test_rejected_no_copy(worker, monkeypatch):
    monkeypatch.setattr(ftp_marathon.qc_worker, "process",
                        lambda *args, **kwargs: SimpleNamespace(state="rejected", summary="Ton abgelehnt"))

    report = ftp_marathon.run_job(worker.job, worker.tmp_path / "reports", worker.tmp_path / "worker.log")

    assert report.status == "rejected"
    assert not (worker.ftp_dir / worker.job["ftp_name"]).exists()


def test_error_is_failed(worker, monkeypatch):
    monkeypatch.setattr(ftp_marathon.qc_worker, "process",
                        lambda *args, **kwargs: SimpleNamespace(state="error", summary="Prüfung fehlgeschlagen"))

    report = ftp_marathon.run_job(worker.job, worker.tmp_path / "reports", worker.tmp_path / "worker.log")

    assert report.status == "failed"
    assert not (worker.ftp_dir / worker.job["ftp_name"]).exists()


def test_ok_copies(worker, monkeypatch):
    data = os.urandom(3 * 1024 * 1024)
    worker.source.write_bytes(data)
    worker.job["size"] = len(data)
    monkeypatch.setattr(ftp_marathon, "copy_chunk", 1024 * 1024)

    report = ftp_marathon.run_job(worker.job, worker.tmp_path / "reports", worker.tmp_path / "worker.log")

    target = worker.ftp_dir / worker.job["ftp_name"]
    assert report.status == "ok"
    assert report.ftp_file == worker.job["ftp_name"]
    assert report.size == len(data)
    assert target.read_bytes() == data
    assert not target.with_name(f".{target.name}.part").exists()
    assert len(worker.statuses) >= 3


def test_existing_target_same_size_kept(worker):
    target = worker.ftp_dir / worker.job["ftp_name"]
    target.write_bytes(b"retain")

    report = ftp_marathon.run_job(worker.job, worker.tmp_path / "reports", worker.tmp_path / "worker.log")

    assert report.status == "ok"
    assert target.read_bytes() == b"retain"


def test_existing_target_other_size_replaced(worker):
    target = worker.ftp_dir / worker.job["ftp_name"]
    target.write_bytes(b"x")

    report = ftp_marathon.run_job(worker.job, worker.tmp_path / "reports", worker.tmp_path / "worker.log")

    assert report.status == "ok"
    assert target.read_bytes() == worker.source.read_bytes()


def test_source_size_mismatch(worker):
    worker.job["size"] += 1

    report = ftp_marathon.run_job(worker.job, worker.tmp_path / "reports", worker.tmp_path / "worker.log")

    assert report.status == "failed"
    assert "Bytes statt" in report.result
    assert worker.calls == []
    assert not (worker.ftp_dir / worker.job["ftp_name"]).exists()


def test_copy_failure_cleans_part(worker, monkeypatch):
    def fail_replace(source, target):
        raise OSError("FTP nicht erreichbar")

    monkeypatch.setattr(ftp_marathon.os, "replace", fail_replace)

    report = ftp_marathon.run_job(worker.job, worker.tmp_path / "reports", worker.tmp_path / "worker.log")

    target = worker.ftp_dir / worker.job["ftp_name"]
    assert report.status == "failed"
    assert "Kopie auf den FTP fehlgeschlagen" in report.result
    assert not target.with_name(f".{target.name}.part").exists()


def test_ctrl_c_during_copy_cleans_part(worker, monkeypatch):
    monkeypatch.setattr(ftp_marathon, "copy_chunk", 2)
    target = worker.ftp_dir / "interrupt.wav"

    def interrupt(done):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        ftp_marathon.copy_to_ftp(worker.source, target, worker.source.stat().st_size, interrupt)

    assert not target.with_name(f".{target.name}.part").exists()


def test_end_to_end_ok(worker, monkeypatch):
    monkeypatch.setattr(ftp_marathon.qc_worker, "process", worker.real_process)
    rate = 48000
    write_wav(worker.source, speech_like(10, rate, db=-30), rate)
    worker.job["size"] = worker.source.stat().st_size
    output_folder = worker.tmp_path / "reports"

    report = ftp_marathon.run_job(worker.job, output_folder, worker.tmp_path / "worker.log")

    target = worker.ftp_dir / worker.job["ftp_name"]
    assert report.status == "ok", report.result
    assert target.read_bytes() == worker.source.read_bytes()
    assert list(output_folder.glob("*.aqc.json"))


def test_run_production_settings(monkeypatch):
    captured = {}

    def run(settings, run_job):
        captured["settings"], captured["run_job"] = settings, run_job

    monkeypatch.setattr(ftp_marathon.adapter, "run", run)

    ftp_marathon._run_production()

    settings = captured["settings"]
    assert settings.stage == "FTP"
    assert settings.versions["ftp_marathon"] == "1.1.0"
    assert settings.versions["marathon_adapter"] == "1.4.0"
    assert captured["run_job"] is ftp_marathon.run_job


def test_main_production(monkeypatch):
    called = []
    monkeypatch.setattr(ftp_marathon, "_say", lambda message: None)
    monkeypatch.setattr(ftp_marathon, "_check_plan", lambda: None)
    monkeypatch.setattr(ftp_marathon, "_ask_mode", lambda: "produktion")
    monkeypatch.setattr(ftp_marathon, "_run_production", lambda: called.append(True))

    ftp_marathon.main()

    assert called == [True]
