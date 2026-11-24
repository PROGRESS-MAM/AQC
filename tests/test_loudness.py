import wave
from pathlib import Path

import numpy as np
import pytest

from aqc.aqc import Clip
from aqc.tools._loudness import _lines, measure_loudness


def _clip(tmp_path: Path, channels: int, amplitudes: tuple[float, ...] | None = None) -> Clip:
    sample_rate, duration = 48000, 5
    path = tmp_path / f"audio-{channels}.wav"
    samples = np.zeros((sample_rate * duration, channels), dtype=np.int16)
    if amplitudes is not None:
        times = np.arange(sample_rate * duration) / sample_rate
        for channel, amplitude in enumerate(amplitudes):
            samples[:, channel] = np.round(amplitude * 32767 * np.sin(2 * np.pi * 1000 * times)).astype(np.int16)
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(samples.tobytes())
    stream = {"index": 0, "codec_type": "audio", "channels": channels, "sample_rate": "48000"}
    return Clip(path, float(duration), None, 0, (stream,), {})


def test_lines_across_chunks():
    assert list(_lines([b"lavfi.r128.I=-2", b"3.5\nlavfi.r128.true_peaks_ch0=0.2\nlast"])) == [
        "lavfi.r128.I=-23.5", "lavfi.r128.true_peaks_ch0=0.2", "last"
    ]


@pytest.mark.parametrize(("amplitude", "expected"), [(0.1, -23.0), (0.5, -9.0)])
def test_sine_levels(tmp_path, amplitude, expected):
    clip = _clip(tmp_path, 1, (amplitude,))
    reports = []
    result = measure_loudness(clip, clip.streams[0], reports.append)
    assert result.integrated == pytest.approx(expected, abs=0.2)
    assert result.true_peaks[0] == pytest.approx(amplitude, abs=0.01)
    assert reports and reports[-1] >= 4.5


def test_stereo_true_peaks(tmp_path):
    clip = _clip(tmp_path, 2, (0.5, 0.1))
    result = measure_loudness(clip, clip.streams[0], lambda seconds: None)
    assert result.true_peaks == pytest.approx((0.5, 0.1), abs=0.01)
    assert len(result.true_peaks) == 2


def test_silence(tmp_path):
    clip = _clip(tmp_path, 1)
    result = measure_loudness(clip, clip.streams[0], lambda seconds: None)
    assert result.integrated is None
    assert result.true_peaks == (0.0,)


def test_missing_file(tmp_path):
    path = tmp_path / "missing.wav"
    stream = {"index": 0, "codec_type": "audio", "channels": 1, "sample_rate": "48000"}
    clip = Clip(path, 5.0, None, 0, (stream,), {})
    with pytest.raises(RuntimeError):
        measure_loudness(clip, stream, lambda seconds: None)


def test_bad_channels(tmp_path):
    path = tmp_path / "audio.wav"
    stream = {"index": 0, "codec_type": "audio", "channels": 0, "sample_rate": "48000"}
    clip = Clip(path, 5.0, None, 0, (stream,), {})
    with pytest.raises(ValueError, match="Kanalzahl nicht lesbar"):
        measure_loudness(clip, stream, lambda seconds: None)


def test_missing_integrated_loudness_value_error(tmp_path, monkeypatch):
    clip = _clip(tmp_path, 1)
    monkeypatch.setattr("aqc.tools._loudness.ffmpeg_chunks", lambda *_: iter([b"pts_time:0.5\n"]))

    with pytest.raises(RuntimeError) as error:
        measure_loudness(clip, clip.streams[0], lambda seconds: None)

    assert str(error.value) == "Audiostream 0: Lautheit nicht messbar."
