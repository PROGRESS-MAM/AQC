# --------- IMPORTS ---------
from functools import partial
from pathlib import Path
from typing import Callable
import math
import re
import numpy as np
from ..aqc import Clip, Failure, Outcome, ProgressFn, Tool, ffmpeg_chunks, load_config, make_tool, parse_number, \
    parse_seconds


# --------- CONFIG ---------
config_keys = ("threshold", "min_active", "window")
default_window = 1.0        # seconds
chunk_seconds = 4
sample_bytes = 4            # decoded as 32 bit float, full scale is 1.0


# --------- STATIC ---------
db_pattern = re.compile(r"(-?\d+(?:\.\d+)?)\s*(?:dbfs|db)?", re.IGNORECASE)
percent_pattern = re.compile(r"(\d+(?:\.\d+)?)\s*%?")


# --------- FUNC SCAN ---------
def _scan_stream(clip: Clip, stream: dict, rate: int, window_samples: int, level: float,
                 report: Callable[[int, int], None]) -> tuple[np.ndarray, np.ndarray, int]:
    """Per channel: samples in windows with RMS >= level and peak (linear); plus the samples per channel."""
    channels = int(stream.get("channels") or 0)
    if channels < 1:
        raise ValueError(f"Audiostream {stream['index']}: Kanalzahl nicht lesbar.")
    args = ["-i", str(clip.path), "-map", f"0:{stream['index']}", "-c:a", "pcm_f32le", "-f", "f32le", "-"]
    frame_bytes = channels * sample_bytes
    block_bytes = window_samples * frame_bytes
    chunk_size = block_bytes * max(1, math.ceil(chunk_seconds * rate / window_samples))
    active, peak, samples, rest, done_bytes = np.zeros(channels, np.int64), np.zeros(channels), 0, b"", 0

    def add(data: bytes, frames: int, length: int) -> None:
        nonlocal active, peak, samples
        values = np.frombuffer(data, "<f4", frames * channels).reshape(frames, channels)
        mean_square = np.square(values.reshape(-1, length, channels).astype(np.float64)).mean(axis=1)
        active = active + (mean_square >= level ** 2).sum(axis=0) * length
        peak = np.maximum(peak, np.abs(values).max(axis=0))
        samples += frames

    for chunk in ffmpeg_chunks(args, chunk_size):
        data = rest + chunk if rest else chunk
        if cut := len(data) // block_bytes * block_bytes:
            add(data, cut // frame_bytes, window_samples)
        rest, done_bytes = data[cut:], done_bytes + len(chunk)
        report(samples, done_bytes)
    # The last window is usually shorter; it counts with its real length.
    if tail := len(rest) // frame_bytes:
        add(rest, tail, tail)
    return active, peak, samples


# --------- FUNC RUN ---------
def _db_text(value: float) -> str:
    return f"{20 * math.log10(value):.1f} dBFS" if value > 0 else "-inf dBFS"


def _run(threshold: float, min_active: float, window: float, clip: Clip, progress: ProgressFn) -> Outcome:
    streams = clip.streams_of("audio")
    if not streams:
        raise ValueError("Clip hat keine Audiostreams.")
    level = 10 ** (threshold / 20)
    total, done_media, done_bytes = clip.duration * len(streams), 0.0, 0
    failures, info = [], []
    for stream in streams:
        index, rate = stream["index"], int(parse_number(stream.get("sample_rate")) or 0)
        if rate < 1:
            raise ValueError(f"Audiostream {index}: Samplerate nicht lesbar.")
        active, peak, samples = _scan_stream(clip, stream, rate, max(1, round(window * rate)), level,
                                             lambda count, size: progress(done_media + count / rate, total,
                                                                          (done_bytes + size) / 1e6, "MB"))
        if not samples:
            raise ValueError(f"Audiostream {index}: keine Samples dekodierbar.")
        shares = (active / samples * 100).tolist()
        failures += [Failure("min_active", f">= {min_active:g}% Ton über {threshold:g} dBFS", f"{share:.2f}% Ton",
                             stream=index, channel=channel) for channel, share in enumerate(shares, 1)
                     if share < min_active]
        info.append(f"Stream {index} Ist-Werte: " + ", ".join(
            f"Kanal {channel} Ton={share:.2f}% Spitze={_db_text(value)}"
            for channel, (share, value) in enumerate(zip(shares, peak.tolist()), 1)))
        done_media += samples / rate
        done_bytes += samples * len(shares) * sample_bytes
    return Outcome(not failures, failures, info, done_media)


# --------- FUNC CONFIG ---------
def _config_number(path: Path, data: dict, key: str, pattern: re.Pattern, example: str) -> float:
    value = data[key]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str) and (match := pattern.fullmatch(value.strip())):
        return float(match[1])
    raise ValueError(f"Config {path}: {key}: ungültiger Wert {value!r}, erwartet z. B. '{example}'.")


def digital_silence(config: str | Path, option: object = None) -> Tool:
    """Check that every channel of every audio stream carries sound for a minimum share of its length.

    config: name of configs/<name>.toml or a path; TOML with tool = "digital_silence", threshold (e.g. "-60dB",
    RMS per window in dBFS), min_active (e.g. "1%") and optional window (default "1s").
    A channel fails if its windows with RMS >= threshold cover less than min_active of the stream.
    option: None or must_fail.
    """
    path, data = load_config(config, "digital_silence", config_keys, ("threshold", "min_active"))
    threshold = _config_number(path, data, "threshold", db_pattern, "-60dB")
    min_active = _config_number(path, data, "min_active", percent_pattern, "1%")
    try:
        window = parse_seconds(data.get("window", default_window))
    except ValueError as exc:
        raise ValueError(f"Config {path}: window: {exc}") from exc
    if threshold >= 0:
        raise ValueError(f"Config {path}: threshold muss kleiner als 0 dB sein.")
    if not 0 < min_active <= 100:
        raise ValueError(f"Config {path}: min_active muss größer als 0 und höchstens 100 % sein.")
    if window <= 0:
        raise ValueError(f"Config {path}: window muss größer als 0 sein.")
    return make_tool("digital_silence", path, option, partial(_run, threshold, min_active, window))
