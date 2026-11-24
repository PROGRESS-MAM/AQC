import numpy as np
import pytest

from aqc.aqc import Clip
from aqc.tools._spectrum import Analysis, analyse_samples, analyse_stream, band_edges, high_bands
from tests.audio_signals import hum, pink, read_wav, speech_like, tone, white, write_wav


@pytest.mark.parametrize("rate", (48000, 44100, 16000))
@pytest.mark.parametrize("signal", (white, pink, hum, tone))
def test_static_signals_have_no_dynamics(rate, signal):
    values = signal(10, rate)
    result = analyse_samples(values[:, None], rate, 2.0, -80.0)
    assert result.dynamics.max() < 5.0


def test_band_edges():
    assert len(band_edges) == 6
    assert band_edges[0] == pytest.approx(353.55, abs=0.01)
    assert band_edges[3] == pytest.approx(2828.43, abs=0.01)
    assert high_bands == 2


@pytest.mark.parametrize("rate", (48000, 44100, 16000))
def test_speech_like_is_dynamic(rate):
    voice = speech_like(10, rate)
    mixed = voice + white(10, rate, db=-30.0)
    assert analyse_samples(voice[:, None], rate, 2.0, -80.0).dynamics.min() > 20.0
    assert analyse_samples(mixed[:, None], rate, 2.0, -80.0).dynamics.min() > 12.0


def test_dull_has_no_highs():
    rate = 48000
    voice = speech_like(10, rate)
    dull = speech_like(10, rate, max_freq=2500)

    def high_share(values):
        content = analyse_samples(values[:, None], rate, 2.0, -80.0).content
        return 10 * np.log10(content[..., -2:].sum() / content.sum())

    assert high_share(voice) > -15.0
    assert high_share(dull) < -30.0


def test_windows_and_short_tail():
    rate = 48000
    values = white(10.05, rate)
    result = analyse_samples(values[:, None], rate, 2.0, -80.0)
    assert result.window_samples.tolist() == [96000] * 5 + [2400]
    assert result.samples == 482400

    short = analyse_samples(np.ones((2 * 96000 + 100, 1)), rate, 2.0, -80.0)
    assert short.dynamics[-1, 0] == 0.0
    assert np.array_equal(short.content[-1, 0], np.zeros(5))


def test_stream_matches_samples(tmp_path):
    rate = 48000
    values = np.column_stack((speech_like(10, rate), white(10, rate)))
    path = write_wav(tmp_path / "stereo.wav", values, rate)
    clip = Clip(path, 10.0, None, 0,
                ({"index": 0, "codec_type": "audio", "channels": 2, "sample_rate": "48000"},), {})
    reports = []
    actual = analyse_stream(clip, clip.streams[0], 2.0, -80.0, lambda samples, size: reports.append((samples, size)))
    expected = analyse_samples(read_wav(path), rate, 2.0, -80.0)
    assert np.allclose(actual.dynamics, expected.dynamics, atol=0.01)
    assert np.allclose(actual.mean_square, expected.mean_square, rtol=1e-4)
    assert np.array_equal(actual.window_samples, expected.window_samples)
    assert reports[-1][0] == 480000


def test_stream_errors(tmp_path):
    path = tmp_path / "unused.wav"
    clip = Clip(path, 0.0, None, 0, (), {})
    with pytest.raises(ValueError, match="Kanalzahl nicht lesbar"):
        analyse_stream(clip, {"index": 0, "channels": 0, "sample_rate": "48000"}, 2.0, -80.0, lambda *_: None)
    with pytest.raises(ValueError, match="Samplerate nicht lesbar"):
        analyse_stream(clip, {"index": 1, "channels": 1, "sample_rate": "x"}, 2.0, -80.0, lambda *_: None)
