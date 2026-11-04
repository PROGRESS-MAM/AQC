import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from marathon import qc_marathon


MATCHING_JOB = {"collection": "LYNXarchive", "identifier": " LA000674 "}


@pytest.mark.parametrize(
    "job",
    [
        MATCHING_JOB,
        {"collection": "LYNXarchive", "identifier": "LA000674"},
    ],
)
def test_expected_silence_matches_lynx_la_job(job):
    assert qc_marathon.expected_silence(job) is True


@pytest.mark.parametrize(
    "job",
    [
        {"collection": "LYNXarchive", "identifier": "la000674"},
        {"collection": "lynxarchive", "identifier": "LA000674"},
        {"collection": " LYNXarchive", "identifier": "LA000674"},
        {"identifier": "LA000674"},
        {"collection": "LYNXarchive"},
        {"collection": None, "identifier": "LA000674"},
        {"collection": "LYNXarchive", "identifier": 674},
    ],
)
def test_expected_silence_rejects_nonmatching_or_incomplete_job(job):
    assert qc_marathon.expected_silence(job) is False


def test_silent_matching_job_skips_silence_and_audio_bitrate_checks(tmp_path):
    tools = qc_marathon._tools_for_job(MATCHING_JOB, is_silent=True, folder=tmp_path)

    assert [entry["tool"] for entry in tools] == ["stream_specs", "stream_specs"]
    audio_tool = next(entry for entry in tools if entry["config"].stem == "audio_aac_stereo")
    config = tomllib.loads(audio_tool["config"].read_text(encoding="utf-8"))
    assert "bitrate" not in config

    original_config = tomllib.loads(
        (qc_marathon.config_folder / "audio_aac_stereo.toml").read_text(encoding="utf-8")
    )
    assert "bitrate" in original_config


def test_matching_job_with_tone_keeps_audio_bitrate_check():
    tools = qc_marathon._tools_for_job(MATCHING_JOB, is_silent=False)

    assert [entry["tool"] for entry in tools] == ["stream_specs", "stream_specs", "digital_silence"]
    audio_tool = next(entry for entry in tools if entry["config"].stem == "audio_aac_stereo")
    config = tomllib.loads(audio_tool["config"].read_text(encoding="utf-8"))
    assert config["bitrate"] == {"min": "160k", "max": "400k"}


def test_nonmatching_job_keeps_standard_plan_unchanged(tmp_path):
    job = {"collection": "lynxarchive", "identifier": "LA000674"}
    tools = qc_marathon._tools_for_job(job, is_silent=True, folder=tmp_path)

    assert tools == qc_marathon._tools(qc_marathon.qc_plan)
    assert all(entry["config"].parent == qc_marathon.config_folder for entry in tools)
    assert list(tmp_path.iterdir()) == []


def _make_clip(ffmpeg, destination, audio_source):
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=1920x1080:r=25:d=1.2",
        "-f",
        "lavfi",
        "-i",
        audio_source,
        "-t",
        "1.2",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-b:v",
        "12M",
        "-minrate",
        "12M",
        "-maxrate",
        "12M",
        "-bufsize",
        "24M",
        "-x264-params",
        "nal-hrd=cbr",
        "-c:a",
        "aac",
        "-b:a",
        "64k",
        "-ac",
        "2",
        str(destination),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        pytest.skip(f"ffmpeg cannot create test clips: {exc.stderr}")
    return destination


@pytest.fixture(scope="module")
def media_clips(tmp_path_factory):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is unavailable")
    folder = tmp_path_factory.mktemp("expected-silence-media")
    return {
        "silent": _make_clip(ffmpeg, folder / "silent.mp4", "anullsrc=r=48000:cl=stereo"),
        "tone": _make_clip(ffmpeg, folder / "tone.mp4", "sine=frequency=440:sample_rate=48000"),
    }


def _run_media_job(monkeypatch, clip, output_folder):
    monkeypatch.setattr(qc_marathon.adapter, "share_path", lambda path: Path(path))
    monkeypatch.setattr(qc_marathon.adapter, "log", lambda message: None)
    monkeypatch.setattr(qc_marathon.adapter, "status", lambda message: None)
    job = {**MATCHING_JOB, "input": str(clip), "ffe_tafel": False}
    return qc_marathon.run_job(job, output_folder, output_folder / "qc.log")


def test_matching_silent_media_job_passes_without_bitrate_check(media_clips, monkeypatch, tmp_path):
    report = _run_media_job(monkeypatch, media_clips["silent"], tmp_path / "silent-report")

    assert report.status == "ok"
    assert report.preset == qc_marathon.qc_plan_name


def test_matching_tone_media_job_keeps_bitrate_check_active(media_clips, monkeypatch, tmp_path):
    report = _run_media_job(monkeypatch, media_clips["tone"], tmp_path / "tone-report")

    assert report.status == "rejected"
    assert "audio_aac_stereo" in report.result
