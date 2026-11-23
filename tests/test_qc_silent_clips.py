from pathlib import Path
import tomllib
from types import SimpleNamespace

import pytest

from marathon import qc_marathon


EXPECTED_SILENT_CLIPS = {
    "DEFA Videoarchive Cintec": ("DEFA06955", "DEFA08247", "DEFA01902", "DEFA12880_2"),
    "DEFA Videoarchive Thomas Grimm": ("DEFA17661", "DEFA02244_3"),
}


def test_silent_clips_config():
    assert qc_marathon.silent_clips == EXPECTED_SILENT_CLIPS


@pytest.mark.parametrize(
    ("collection", "identifier"),
    [
        ("DEFA Videoarchive Cintec", "DEFA06955"),
        ("DEFA Videoarchive Cintec", " DEFA12880_2 "),
        ("DEFA Videoarchive Cintec", "DEFA01902_1"),
        ("DEFA Videoarchive Thomas Grimm", "DEFA02244_3"),
    ],
)
def test_silent_clip_matches(collection, identifier):
    assert qc_marathon._silent_clip({"collection": collection, "identifier": identifier}) is True


@pytest.mark.parametrize(
    "job",
    [
        {"collection": "DEFA Videoarchive Cintec", "identifier": "DEFA01584"},
        {"collection": "DEFA Der Augenzeuge", "identifier": "DEFA06955"},
        {"collection": "DEFA Videoarchive cintec", "identifier": "DEFA06955"},
        {"collection": "DEFA Videoarchive Cintec", "identifier": "defa06955"},
        {"collection": "DEFA Videoarchive Cintec"},
    ],
)
def test_silent_clip_rejects(job):
    assert qc_marathon._silent_clip(job) is False


def _capture_process(monkeypatch):
    captured = {}

    def process(sources, tools, output_folder, on_progress):
        captured["tools"] = [entry["tool"] for entry in tools]
        audio_config = next(entry["config"] for entry in tools
                            if entry["tool"] == "stream_specs" and entry["config"].stem == "audio_aac_stereo")
        captured["audio_config"] = tomllib.loads(audio_config.read_text(encoding="utf-8"))
        return SimpleNamespace(state="ok", summary="ok")

    monkeypatch.setattr(qc_marathon.qc_worker, "process", process)
    monkeypatch.setattr(qc_marathon.adapter, "share_path", lambda path: Path(path))
    monkeypatch.setattr(qc_marathon.adapter, "log", lambda message: None)
    monkeypatch.setattr(qc_marathon.adapter, "status", lambda message: None)
    return captured


def _job(collection, identifier):
    return {"collection": collection, "identifier": identifier, "input": "proxy.mp4", "ffe_tafel": False}


def test_silent_clip_job_skips_measurement_and_checks(monkeypatch, tmp_path):
    captured = _capture_process(monkeypatch)

    def unexpected_measurement(source):
        raise AssertionError("silent clips must not be measured")

    monkeypatch.setattr(qc_marathon, "_measured_silent", unexpected_measurement)
    report = qc_marathon.run_job(
        _job("DEFA Videoarchive Cintec", "DEFA06955"), tmp_path, tmp_path / "qc.log"
    )

    assert report.status == "ok"
    assert captured["tools"] == ["stream_specs", "stream_specs"]
    assert "bitrate" not in captured["audio_config"]


def test_other_clip_of_collection_keeps_standard_plan(monkeypatch, tmp_path):
    captured = _capture_process(monkeypatch)

    def unexpected_measurement(source):
        raise AssertionError("unlisted clips must not be measured")

    monkeypatch.setattr(qc_marathon, "_measured_silent", unexpected_measurement)
    qc_marathon.run_job(_job("DEFA Videoarchive Cintec", "DEFA01584"), tmp_path, tmp_path / "qc.log")

    assert captured["tools"] == ["stream_specs", "stream_specs", "digital_silence"]


def test_expected_silence_collection_still_measures(monkeypatch, tmp_path):
    captured = _capture_process(monkeypatch)
    calls = []

    def measured_silent(source):
        calls.append(source)
        return False

    monkeypatch.setattr(qc_marathon, "_measured_silent", measured_silent)
    qc_marathon.run_job(_job("LYNXarchive", "LA000674"), tmp_path, tmp_path / "qc.log")

    assert len(calls) == 1
    assert "digital_silence" in captured["tools"]
