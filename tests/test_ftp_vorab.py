import csv
import json
import tomllib
from pathlib import Path

import pytest

from marathon import ftp_marathon
from tests.audio_signals import pink, speech_like, write_wav


def _clip(clip_id, *, ready=True, active=True, proxy=True, folder="DEFA", name=None, ftp=None):
    files = {"proxy": {"folder": folder, "name": name or f"{clip_id}.wav"} if proxy else {"proxy": None}}
    return {
        "clip_id": clip_id,
        "ready": ready,
        "active": active,
        "files": files,
        "ftp": ftp or {},
        "collection": "Testkollektion",
        "identifier": f"ID{clip_id}",
        "title": f"Titel {clip_id}",
    }


def _write_state(path, clips, schema_version=8):
    path.write_text(json.dumps({"schema_version": schema_version, "clips": {str(clip["clip_id"]): clip for clip in clips}}), encoding="utf-8")
    return path


def test_check_plan():
    ftp_marathon._check_plan()


def test_config_values():
    config_folder = Path(ftp_marathon.__file__).resolve().parent / "configs"
    assert tomllib.loads((config_folder / "hoerbarer_ton.toml").read_text(encoding="utf-8")) == {
        "tool": "audible_sound", "threshold": "-60dB", "min_dynamics": "6dB",
        "min_active": "10%", "window": "2s",
    }
    assert tomllib.loads((config_folder / "ton_qualitaet.toml").read_text(encoding="utf-8")) == {
        "tool": "sound_quality", "threshold": "-60dB", "min_dynamics": "6dB",
        "window": "2s", "min_high": "-20dB", "max_loudness": "-22.5LUFS",
        "max_true_peak": "-1dBTP",
    }


def test_ready_clips_filter(tmp_path):
    state = _write_state(tmp_path / "marathon.json", [
        _clip("3"), _clip("1"), _clip("2", ready=False), _clip("4", active=False),
        _clip("5", proxy=False), _clip("6", ftp={"copied_at": "2026-11-24"}),
        _clip("7", ftp={"copied_at": None}),
    ])
    assert [clip["clip_id"] for clip in ftp_marathon.ready_clips(state)] == ["1", "3", "7"]


def test_ready_clips_errors(tmp_path):
    with pytest.raises(FileNotFoundError, match="marathon.json nicht gefunden"):
        ftp_marathon.ready_clips(tmp_path / "missing.json")
    state = _write_state(tmp_path / "marathon.json", [], schema_version=7)
    with pytest.raises(ValueError, match="keine Marathon-JSON Version 8"):
        ftp_marathon.ready_clips(state)


def test_clip_source(tmp_path, monkeypatch):
    monkeypatch.setattr(ftp_marathon, "root_path", tmp_path)
    assert ftp_marathon.clip_source({"files": {"proxy": {"folder": "DEFA", "name": "x.wav"}}}) == tmp_path / "DEFA" / "x.wav"
    assert ftp_marathon.clip_source({"files": {"proxy": {"folder": "a/b", "name": "x.wav"}}}) == tmp_path / "a" / "b" / "x.wav"
    assert ftp_marathon.clip_source({"files": {"proxy": {"folder": str(tmp_path / "x"), "name": "x.wav"}}}) == tmp_path / "x" / "x.wav"


def test_run_vorab(tmp_path, monkeypatch):
    monkeypatch.setattr(ftp_marathon, "root_path", tmp_path)
    source_folder = tmp_path / "DEFA"
    source_folder.mkdir()
    rate = 48000
    write_wav(source_folder / "1.wav", speech_like(10, rate, db=-30), rate)
    write_wav(source_folder / "2.wav", pink(10, rate, db=-10), rate)
    state = _write_state(tmp_path / "marathon.json", [_clip("1"), _clip("2"), _clip("3")])

    output = ftp_marathon.run_vorab(state, tmp_path / "out")

    with output.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter=";"))
        handle.seek(0)
        header = next(csv.reader(handle, delimiter=";"))
    assert header == list(ftp_marathon.csv_columns)
    assert [row["status"] for row in rows] == ["ok", "rejected", "error"]
    assert rows[1]["audible_sound"].startswith("NICHT BESTANDEN")
    assert "Lautheit=" in rows[0]["sound_quality_werte"]
    assert len(list((tmp_path / "out" / "berichte").glob("*.aqc.json"))) == 2


def test_vorab_ctrl_c(tmp_path, monkeypatch):
    state = _write_state(tmp_path / "marathon.json", [_clip("1"), _clip("2")])
    real_process = ftp_marathon.qc_worker.process
    calls = 0

    def process(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt
        return real_process(*args, **kwargs)

    monkeypatch.setattr(ftp_marathon.qc_worker, "process", process)
    source_folder = tmp_path / "DEFA"
    source_folder.mkdir()
    rate = 48000
    write_wav(source_folder / "1.wav", speech_like(10, rate, db=-30), rate)
    monkeypatch.setattr(ftp_marathon, "root_path", tmp_path)

    output = ftp_marathon.run_vorab(state, tmp_path / "out")

    with output.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle, delimiter=";"))
    assert len(rows) == 2
    assert rows[0] == list(ftp_marathon.csv_columns)


def test_ask_mode(monkeypatch):
    answers = iter(["x", "2"])
    assert ftp_marathon._ask_mode(lambda _: next(answers)) == "vorab"
    assert ftp_marathon._ask_mode(lambda _: (_ for _ in ()).throw(EOFError())) == "produktion"


def test_ask_state_file(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{}", encoding="utf-8")
    answers = iter(['"/nicht/da.json"', f'"{path}"'])
    assert ftp_marathon._ask_state_file(lambda _: next(answers)) == path
    with pytest.raises(RuntimeError, match="Keine Eingabe für den Pfad"):
        ftp_marathon._ask_state_file(lambda _: (_ for _ in ()).throw(EOFError()))


def test_vorab_ctrl_c_before_first_clip_returns_partial_list(tmp_path, monkeypatch):
    state = _write_state(tmp_path / "marathon.json", [_clip("1")])
    monkeypatch.setattr(ftp_marathon, "root_path", tmp_path)
    monkeypatch.setattr(ftp_marathon, "_say", lambda message: None)
    monkeypatch.setattr(
        ftp_marathon, "enumerate", lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()), raising=False
    )

    output = ftp_marathon.run_vorab(state, tmp_path / "out")

    assert output.is_file()
    with output.open(encoding="utf-8-sig", newline="") as handle:
        assert next(csv.reader(handle, delimiter=";")) == list(ftp_marathon.csv_columns)
