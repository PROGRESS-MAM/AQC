import numpy as np
import pytest

from aqc import aqc
import qc_worker
from aqc.aqc import Clip
from aqc.tools.audible_sound import audible_sound
from tests.audio_signals import hum, pink, speech_like, tone, white, write_wav


RATE = 48000
SECONDS = 10


def _tool(tmp_path, **values):
    config = tmp_path / "audible_sound.toml"
    settings = {"tool": '"audible_sound"', "threshold": '"-60dB"', "min_dynamics": '"6dB"',
                "min_active": '"10%"', "window": '"2s"'}
    settings.update({key: f'"{value}"' for key, value in values.items()})
    config.write_text("\n".join(f"{key} = {value}" for key, value in settings.items()) + "\n", encoding="utf-8")
    return audible_sound(config)


def _clip(path, channels):
    return Clip(path, float(SECONDS), None, 0,
                ({"index": 0, "codec_type": "audio", "channels": channels, "sample_rate": str(RATE)},), {})


def _run(tmp_path, values):
    path = write_wav(tmp_path / "test.wav", values, RATE)
    outcome = _tool(tmp_path).runner(_clip(path, values.shape[1] if values.ndim == 2 else 1), lambda *a: None)
    return outcome


def test_noise_channel_fails(tmp_path):
    values = np.column_stack((speech_like(SECONDS, RATE), pink(SECONDS, RATE, db=-10)))
    outcome = _run(tmp_path, values)
    assert not outcome.passed
    assert [(f.criterion, f.stream, f.channel, f.actual) for f in outcome.failures] == [
        ("min_active", 0, 2, "0.00% Ton")]
    assert outcome.info[0].startswith("Stream 0 Ist-Werte: Kanal 1 Ton=100.00% Dynamik=")


@pytest.mark.parametrize("signal", (hum, tone, white))
def test_static_signals_fail(tmp_path, signal):
    outcome = _run(tmp_path, signal(SECONDS, RATE, db=-10)[:, None])
    assert not outcome.passed
    assert outcome.failures[0].actual == "0.00% Ton"


def test_digital_silence_fails(tmp_path):
    outcome = _run(tmp_path, np.zeros((SECONDS * RATE, 1)))
    assert not outcome.passed
    assert "Dynamik=-" in outcome.info[0]


def test_speech_in_noise_passes(tmp_path):
    values = speech_like(SECONDS, RATE) + white(SECONDS, RATE, db=-30)
    outcome = _run(tmp_path, values[:, None])
    assert outcome.passed
    assert outcome.failures == []


@pytest.mark.parametrize("overrides", ({"min_dynamics": "0dB"}, {"window": "0.1s"},
                                        {"threshold": "0dB"}, {"min_dynamics": None}))
def test_config_errors(tmp_path, overrides):
    path = tmp_path / "audible_sound.toml"
    settings = {"tool": '"audible_sound"', "threshold": '"-60dB"', "min_dynamics": '"6dB"',
                "min_active": '"10%"', "window": '"2s"'}
    for key, value in overrides.items():
        if value is None:
            settings.pop(key)
        else:
            settings[key] = f'"{value}"'
    path.write_text("\n".join(f"{key} = {value}" for key, value in settings.items()) + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        audible_sound(path)


def test_no_audio_stream(tmp_path):
    tool = _tool(tmp_path)
    clip = Clip(tmp_path / "unused.wav", float(SECONDS), None, 0, ({"index": 0, "codec_type": "video"},), {})
    with pytest.raises(ValueError, match="Clip hat keine Audiostreams\\."):
        tool.runner(clip, lambda *a: None)


def test_check_end_to_end(tmp_path):
    path = write_wav(tmp_path / "pink.wav", pink(SECONDS, RATE)[:, None], RATE)
    result, _, _ = aqc.check(path, [_tool(tmp_path)])
    assert result["tools"][0]["status"] == "NICHT BESTANDEN"


def test_registered():
    assert "audible_sound" in qc_worker.methods()
    assert aqc.app_version == "0.5.0"
