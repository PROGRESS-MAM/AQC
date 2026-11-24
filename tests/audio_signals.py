import wave
from pathlib import Path
import numpy as np


def _norm(values, db):
    return values / np.sqrt(np.mean(values ** 2)) * 10 ** (db / 20)


def _colored(n, rate, alpha, seed):
    rng = np.random.default_rng(seed)
    spectrum, freqs = np.fft.rfft(rng.standard_normal(n)), np.fft.rfftfreq(n, 1 / rate)
    freqs[0] = freqs[1]
    return np.fft.irfft(spectrum / freqs ** (alpha / 2), n)


def white(seconds, rate, db=-20.0, seed=1):
    return _norm(_colored(round(seconds * rate), rate, 0, seed), db)


def pink(seconds, rate, db=-20.0, seed=1):
    return _norm(_colored(round(seconds * rate), rate, 1, seed), db)


def hum(seconds, rate, db=-20.0, base=49.7):
    t = np.arange(round(seconds * rate)) / rate
    return _norm(sum(np.sin(2 * np.pi * base * k * t + k) / k for k in range(1, 12)), db)


def tone(seconds, rate, db=-20.0, freq=1000.0):
    return _norm(np.sin(2 * np.pi * freq * np.arange(round(seconds * rate)) / rate), db)


def speech_like(seconds, rate, db=-20.0, max_freq=8000.0, seed=1):
    n, rng = round(seconds * rate), np.random.default_rng(seed)
    t = np.arange(n) / rate
    phase = 2 * np.pi * np.cumsum(150 + 30 * np.sin(2 * np.pi * 0.7 * t)) / rate
    envelope, pos = np.zeros(n), 0
    while pos < n:
        length, gap = int(rate * rng.uniform(0.12, 0.3)), int(rate * rng.uniform(0.03, 0.25))
        part = np.hanning(length)[:n - pos]
        envelope[pos:pos + len(part)] = part
        pos += length + gap
    return _norm(sum(np.sin(k * phase) / k for k in range(1, 80) if k * 150 < max_freq) * envelope, db)


def write_wav(path, values, rate) -> Path:
    """16 bit PCM; values (n,) or (n, channels) in -1..1."""
    data = np.round(np.clip(np.asarray(values).reshape(len(values), -1), -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(data.shape[1]); handle.setsampwidth(2); handle.setframerate(rate)
        handle.writeframes(data.tobytes())
    return Path(path)


def read_wav(path) -> np.ndarray:
    """(n, channels) float64 like ffmpeg decodes 16 bit PCM (value / 32768)."""
    with wave.open(str(path), "rb") as handle:
        data = np.frombuffer(handle.readframes(handle.getnframes()), "<i2")
        return data.reshape(-1, handle.getnchannels()) / 32768
