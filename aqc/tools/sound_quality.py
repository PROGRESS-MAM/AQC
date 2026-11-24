# --------- IMPORTS ---------
from functools import partial
from pathlib import Path
import math
import re
from ..aqc import Clip, Failure, Outcome, ProgressFn, Tool, load_config, make_tool, parse_number, parse_seconds
from .digital_silence import _config_number, db_pattern
from ._spectrum import analyse_stream, frame_seconds, high_bands
from ._loudness import measure_loudness


# --------- CONFIG ---------
config_keys = ("threshold", "min_dynamics", "window", "min_high", "max_loudness", "max_true_peak")
required_keys = ("threshold", "min_dynamics", "min_high", "max_loudness", "max_true_peak")
default_window = 2.0
sample_bytes = 4


# --------- STATIC ---------
lufs_pattern = re.compile(r"(-?\d+(?:\.\d+)?)\s*(?:lufs)?", re.IGNORECASE)
dbtp_pattern = re.compile(r"(-?\d+(?:\.\d+)?)\s*(?:dbtp|db)?", re.IGNORECASE)


# --------- FUNC RUN ---------
def _run(threshold: float, min_dynamics: float, window: float, min_high: float, max_loudness: float,
         max_true_peak: float, clip: Clip, progress: ProgressFn) -> Outcome:
    streams = clip.streams_of("audio")
    if not streams:
        raise ValueError("Clip hat keine Audiostreams.")
    level = 10 ** (threshold / 10)
    total, done_media, done_bytes, media_time = 2 * clip.duration * len(streams), 0.0, 0, 0.0
    failures, info = [], []

    for stream in streams:
        index, rate = stream["index"], int(parse_number(stream.get("sample_rate")) or 0)
        if rate < 1:
            raise ValueError(f"Audiostream {index}: Samplerate nicht lesbar.")
        analysis = analyse_stream(clip, stream, window, threshold - 20,
                                  lambda count, size: progress(done_media + count / rate, total,
                                                              (done_bytes + size) / 1e6, "MB"))
        samples = analysis.samples
        channels = analysis.mean_square.shape[1]
        ton = (analysis.mean_square >= level) & (analysis.dynamics >= min_dynamics)
        stream_duration = samples / rate
        loudness = measure_loudness(clip, stream,
                                    lambda seconds: progress(done_media + stream_duration + seconds, total,
                                                             done_bytes / 1e6, "MB"))
        peaks = loudness.true_peaks
        channel_values = []
        if loudness.integrated is not None and loudness.integrated > max_loudness:
            failures.append(Failure("loudness", f"<= {max_loudness:g} LUFS",
                                    f"{loudness.integrated:.1f} LUFS", stream=index))

        for channel in range(channels):
            content = analysis.content[ton[:, channel], channel]
            if content.sum() <= 0:
                high_share = None
            else:
                ratio = content[:, -high_bands:].sum() / content.sum()
                high_share = 10 * math.log10(ratio) if ratio > 0 else -math.inf
            peak = peaks[channel]
            peak_db = 20 * math.log10(peak) if peak > 0 else -math.inf
            if high_share is not None and high_share < min_high:
                failures.append(Failure("dull", f">= {min_high:g} dB Höhenanteil ab 2.8 kHz",
                                        f"{high_share:.1f} dB", stream=index, channel=channel + 1))
            if peak_db > max_true_peak:
                failures.append(Failure("true_peak", f"<= {max_true_peak:g} dBTP", f"{peak_db:.1f} dBTP",
                                        stream=index, channel=channel + 1))
            high_text = "kein Ton" if high_share is None else f"{high_share:.1f} dB"
            peak_text = f"{peak_db:.1f} dBTP"
            channel_values.append(f"Kanal {channel + 1} Höhen={high_text} Spitze={peak_text}")

        loudness_text = ("nicht messbar" if loudness.integrated is None
                         else f"{loudness.integrated:.1f} LUFS")
        info.append(f"Stream {index} Ist-Werte: Lautheit={loudness_text}, " + ", ".join(channel_values))
        done_media += 2 * stream_duration
        done_bytes += samples * channels * sample_bytes
        media_time += stream_duration

    return Outcome(not failures, failures, info, media_time)


# --------- FUNC CONFIG ---------
def sound_quality(config: str | Path, option: object = None) -> Tool:
    """Check dynamic-signal high-frequency content, integrated loudness and true peak for every audio stream.

    config: name of configs/<name>.toml or a path; TOML with tool = "sound_quality", threshold (e.g. "-60dB"),
    min_dynamics (e.g. "6dB"), min_high (e.g. "-20dB"), max_loudness (e.g. "-22.5LUFS"), max_true_peak
    (e.g. "-1dBTP") and optional window (default "2s").
    A stream fails when its loudness or a channel's true peak exceeds the maximum, or dynamic sound has too little
    high-frequency content above 2.8 kHz.
    option: None or must_fail.
    """
    path, data = load_config(config, "sound_quality", config_keys, required_keys)
    threshold = _config_number(path, data, "threshold", db_pattern, "-60dB")
    min_dynamics = _config_number(path, data, "min_dynamics", db_pattern, "6dB")
    min_high = _config_number(path, data, "min_high", db_pattern, "-20dB")
    max_loudness = _config_number(path, data, "max_loudness", lufs_pattern, "-22.5LUFS")
    max_true_peak = _config_number(path, data, "max_true_peak", dbtp_pattern, "-1dBTP")
    try:
        window = parse_seconds(data.get("window", default_window))
    except ValueError as exc:
        raise ValueError(f"Config {path}: window: {exc}") from exc
    if threshold >= 0:
        raise ValueError(f"Config {path}: threshold muss kleiner als 0 dB sein.")
    if min_dynamics <= 0:
        raise ValueError(f"Config {path}: min_dynamics muss größer als 0 dB sein.")
    if min_high >= 0:
        raise ValueError(f"Config {path}: min_high muss kleiner als 0 dB sein.")
    if max_loudness >= 0:
        raise ValueError(f"Config {path}: max_loudness muss kleiner als 0 LUFS sein.")
    if max_true_peak > 0:
        raise ValueError(f"Config {path}: max_true_peak darf höchstens 0 dBTP sein.")
    if window < 2 * frame_seconds:
        raise ValueError(f"Config {path}: window muss mindestens 0.2 s sein.")
    return make_tool("sound_quality", path, option,
                     partial(_run, threshold, min_dynamics, window, min_high, max_loudness, max_true_peak))
