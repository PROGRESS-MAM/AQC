# --------- IMPORTS ---------
from functools import partial
from pathlib import Path
from typing import Callable
import math
import numpy as np
from ..aqc import Clip, Failure, Outcome, ProgressFn, Range, Tool, ffmpeg_chunks, load_config, make_tool, \
    parse_number, parse_range, parse_seconds


# --------- CONFIG ---------
config_keys = ("min_duration", "expected")
chunk_seconds = 4
sample_bytes = 4            # decoded as 32 bit float, so only real zeros are 0


# --------- FUNC RUNS ---------
def _zero_runs(is_zero: np.ndarray, offset: int, open_start: int | None) -> tuple[list[tuple[int, int]], int | None]:
    """Closed zero runs (start, end) of one chunk and the start of a run still open at its end."""
    edges = np.flatnonzero(np.diff(np.concatenate(([0], is_zero.astype(np.int8), [0]))))
    starts, ends = (edges[0::2] + offset).tolist(), (edges[1::2] + offset).tolist()
    if open_start is not None:
        if starts and starts[0] == offset:
            starts[0] = open_start
        else:
            starts.insert(0, open_start)
            ends.insert(0, offset)
    if starts and ends[-1] == offset + len(is_zero):
        return list(zip(starts[:-1], ends[:-1])), starts[-1]
    return list(zip(starts, ends)), None


def _outside(start: int, end: int, covered: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Parts of start..end that are not in the sorted covered intervals."""
    parts, cursor = [], start
    for low, high in covered:
        if high <= cursor or low >= end:
            continue
        if low > cursor:
            parts.append((cursor, low))
        cursor = max(cursor, high)
    return parts + ([(cursor, end)] if cursor < end else [])


# --------- FUNC RUN ---------
def _scan_stream(clip: Clip, stream: dict, rate: int, min_samples: int,
                 report: Callable[[int, int], None]) -> tuple[list[tuple[int, int, int]], int]:
    """Zero runs (channel, start, end) of at least min_samples and the number of samples per channel."""
    channels = int(stream.get("channels") or 0)
    if channels < 1:
        raise ValueError(f"Audiostream {stream['index']}: Kanalzahl nicht lesbar.")
    args = ["-i", str(clip.path), "-map", f"0:{stream['index']}", "-c:a", "pcm_f32le", "-f", "f32le", "-"]
    chunk_size = rate * channels * sample_bytes * chunk_seconds
    runs, open_starts, offset, done_bytes = [], [None] * channels, 0, 0
    for chunk in ffmpeg_chunks(args, chunk_size):
        usable = len(chunk) // (channels * sample_bytes) * channels
        zero = np.frombuffer(chunk, "<f4", usable).reshape(-1, channels) == 0
        for channel in range(channels):
            closed, open_starts[channel] = _zero_runs(zero[:, channel], offset, open_starts[channel])
            runs += [(channel, start, end) for start, end in closed if end - start >= min_samples]
        offset, done_bytes = offset + len(zero), done_bytes + len(chunk)
        report(offset, done_bytes)
    runs += [(channel, start, offset) for channel, start in enumerate(open_starts)
             if start is not None and offset - start >= min_samples]
    return sorted(runs, key=lambda run: (run[1], run[0])), offset


def _run(min_duration: float, expected: tuple[Range, ...], clip: Clip, progress: ProgressFn) -> Outcome:
    streams = clip.streams_of("audio")
    if not streams:
        raise ValueError("Clip hat keine Audiostreams.")
    covered_seconds = sorted(item.resolve(clip) for item in expected)
    total, done_media, done_bytes = clip.duration * len(streams), 0.0, 0
    failures, info = [], []
    for stream in streams:
        rate = int(parse_number(stream.get("sample_rate")) or 0)
        if rate < 1:
            raise ValueError(f"Audiostream {stream['index']}: Samplerate nicht lesbar.")
        min_samples = max(1, math.ceil(min_duration * rate - 1e-9))
        covered = [(round(start * rate), round(end * rate)) for start, end in covered_seconds]
        runs, samples = _scan_stream(clip, stream, rate, min_samples, lambda count, size: progress(
            done_media + count / rate, total, (done_bytes + size) / 1e6, "MB"))
        found = [(channel, low, high) for channel, start, end in runs
                 for low, high in _outside(start, end, covered) if high - low >= min_samples]
        failures += [Failure("digital_silence", f"keine Stille >= {min_duration:g}s außerhalb von expected",
                             f"{(high - low) / rate:.3f}s Stille", stream=stream["index"], channel=channel + 1,
                             tc=f"{clip.tc(low / rate)} bis {clip.tc(high / rate)}") for channel, low, high in found]
        info.append(f"Stream {stream['index']}: {int(stream['channels'])} Kanäle, "
                    f"{len(found)} Stille-Abschnitte außerhalb von expected")
        done_media += samples / rate
        done_bytes += samples * int(stream["channels"]) * sample_bytes
    return Outcome(not failures, failures, info, done_media)


# --------- FUNC CONFIG ---------
def digital_silence(config: str | Path, option: object = None) -> Tool:
    """Check all channels of all audio streams for digital silence (samples exactly 0).

    config: name of configs/<name>.toml or a path; TOML with tool = "digital_silence", min_duration (e.g. "0.1s") and optional expected ranges where silence is allowed.
    option: None or must_fail.
    """
    path, data = load_config(config, "digital_silence", config_keys, ("min_duration",))
    try:
        min_duration = parse_seconds(data["min_duration"])
    except ValueError as exc:
        raise ValueError(f"Config {path}: min_duration: {exc}") from exc
    if min_duration <= 0:
        raise ValueError(f"Config {path}: min_duration muss größer als 0 sein.")
    expected = data.get("expected", [])
    if not isinstance(expected, list) or not all(isinstance(item, str) for item in expected):
        raise ValueError(f"Config {path}: expected muss eine Liste von Bereichsangaben sein.")
    return make_tool("digital_silence", path, option, partial(_run, min_duration, tuple(map(parse_range, expected))))
