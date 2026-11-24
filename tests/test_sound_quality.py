import re

import numpy as np
import pytest

import qc_worker
from aqc.aqc import Clip
from aqc.tools.sound_quality import sound_quality
from tests.audio_signals import speech_like, tone, write_wav


RATE = 48000
SECONDS = 10


def _tool(tmp_path, **values):
    config = tmp_path / "sound_quality.toml"
    settings = {"tool": '"sound_quality"', "threshold": '"-60dB"', "min_dynamics": '"6dB"',
                "window": '"2s"', "min_high": '"-20dB"', "max_loudness": '"-22.5LUFS"',
                "max_true_peak": '"-1dBTP"'}
    for key, value in values.items():
        if value is None:
            settings.pop(key)
        else:
            settings[key] = f'"{value}"'
    config.write_text("\n".join(f"{key} = {value}" for key, value in settings.items()) + "\n", encoding="utf-8")
    return sound_quality(config)


def _run(tmp_path, values):
    path = write_wav(tmp_path / "test.wav", values, RATE)
    channels = values.shape[1] if values.ndim == 2 else 1
    clip = Clip(path, float(SECONDS), None, 0,
                ({"index": 0, "codec_type": "audio", "channels": channels, "sample_rate": str(RATE)},), {})
    return _tool(tmp_path).runner(clip, lambda *a: None)


def test_dull_channel(tmp_path):
    values = np.column_stack((speech_like(SECONDS, RATE, db=-30),
                              speech_like(SECONDS, RATE, db=-30, max_freq=2500)))
    outcome = _run(tmp_path, values)
    assert [(failure.criterion, failure.channel) for failure in outcome.failures] == [("dull", 2)]
    loudness = re.search(r"Lautheit=(-?\d+\.\d+) LUFS", outcome.info[0])
    assert loudness and float(loudness[1]) == pytest.approx(-27.8, abs=0.5)


def test_loud_stream(tmp_path):
    values = np.column_stack((speech_like(SECONDS, RATE, db=-20), speech_like(SECONDS, RATE, db=-20, seed=2)))
    outcome = _run(tmp_path, values)
    assert [failure.criterion for failure in outcome.failures] == ["loudness"]


def test_tone_over_true_peak(tmp_path):
    outcome = _run(tmp_path, tone(SECONDS, RATE, db=-3.1)[:, None])
    assert {failure.criterion for failure in outcome.failures} == {"loudness", "true_peak"}
    assert "Höhen=kein Ton" in outcome.info[0]


def test_quiet_speech_passes(tmp_path):
    outcome = _run(tmp_path, speech_like(SECONDS, RATE, db=-30)[:, None])
    assert outcome.passed
    assert outcome.failures == []


def test_silence_passes(tmp_path):
    outcome = _run(tmp_path, np.zeros((SECONDS * RATE, 1)))
    assert outcome.passed
    assert "Lautheit=nicht messbar" in outcome.info[0]
    assert "Höhen=kein Ton" in outcome.info[0]


@pytest.mark.parametrize("overrides", ({"min_high": "0dB"}, {"max_loudness": "0LUFS"},
                                        {"max_true_peak": "1dBTP"}, {"window": "0.1s"},
                                        {"max_loudness": None}))
def test_config_errors(tmp_path, overrides):
    with pytest.raises(ValueError):
        _tool(tmp_path, **overrides)


def test_registered():
    assert {"audible_sound", "sound_quality", "digital_silence"} <= set(qc_worker.methods())
