# --------- IMPORTS ---------
from functools import partial
from pathlib import Path
import numpy as np
from ..aqc import Clip, Failure, Outcome, ProgressFn, Tool, load_config, make_tool, parse_number, parse_seconds
from .digital_silence import _config_number, db_pattern, percent_pattern
from ._spectrum import analyse_stream, frame_seconds


# --------- CONFIG ---------
config_keys = ("threshold", "min_dynamics", "min_active", "window")
required_keys = ("threshold", "min_dynamics", "min_active")
default_window = 2.0
sample_bytes = 4


# --------- FUNC RUN ---------
def _run(threshold: float, min_dynamics: float, min_active: float, window: float,
         clip: Clip, progress: ProgressFn) -> Outcome:
    streams = clip.streams_of("audio")
    if not streams:
        raise ValueError("Clip hat keine Audiostreams.")
    level = 10 ** (threshold / 10)
    floor_db = threshold - 20
    total, done_media, done_bytes = clip.duration * len(streams), 0.0, 0
    failures, info = [], []

    for stream in streams:
        index, rate = stream["index"], int(parse_number(stream.get("sample_rate")) or 0)
        if rate < 1:
            raise ValueError(f"Audiostream {index}: Samplerate nicht lesbar.")
        analysis = analyse_stream(clip, stream, window, floor_db,
                                  lambda count, size: progress(done_media + count / rate, total,
                                                              (done_bytes + size) / 1e6, "MB"))
        samples = analysis.samples
        loud = analysis.mean_square >= level
        ton = loud & (analysis.dynamics >= min_dynamics)
        shares = (analysis.window_samples[:, None] * ton).sum(axis=0) / samples * 100
        failures += [Failure("min_active",
                             f">= {min_active:g}% hörbarer Ton (über {threshold:g} dBFS, Dynamik >= {min_dynamics:g} dB)",
                             f"{share:.2f}% Ton", stream=index, channel=channel)
                     for channel, share in enumerate(shares.tolist(), 1) if share < min_active]
        channel_info = []
        for channel, (share, is_loud) in enumerate(zip(shares, loud.T), 1):
            dynamics = analysis.dynamics[is_loud, channel - 1]
            dynamic_text = f"{np.median(dynamics):.1f} dB" if len(dynamics) else "-"
            channel_info.append(f"Kanal {channel} Ton={share:.2f}% Dynamik={dynamic_text}")
        info.append(f"Stream {index} Ist-Werte: " + ", ".join(channel_info))
        done_media += samples / rate
        done_bytes += samples * len(shares) * sample_bytes

    return Outcome(not failures, failures, info, done_media)


# --------- FUNC CONFIG ---------
def audible_sound(config: str | Path, option: object = None) -> Tool:
    """Check that every audio channel contains audible, dynamic sound for a minimum share of its length.

    config: name of configs/<name>.toml or a path; TOML with tool = "audible_sound", threshold (e.g. "-60dB",
    RMS per window in dBFS), min_dynamics (e.g. "6dB"), min_active (e.g. "10%") and optional window (default "2s").
    A channel fails if windows above threshold with at least min_dynamics cover less than min_active of the stream.
    option: None or must_fail.
    """
    path, data = load_config(config, "audible_sound", config_keys, required_keys)
    threshold = _config_number(path, data, "threshold", db_pattern, "-60dB")
    min_dynamics = _config_number(path, data, "min_dynamics", db_pattern, "6dB")
    min_active = _config_number(path, data, "min_active", percent_pattern, "10%")
    try:
        window = parse_seconds(data.get("window", default_window))
    except ValueError as exc:
        raise ValueError(f"Config {path}: window: {exc}") from exc
    if threshold >= 0:
        raise ValueError(f"Config {path}: threshold muss kleiner als 0 dB sein.")
    if min_dynamics <= 0:
        raise ValueError(f"Config {path}: min_dynamics muss größer als 0 dB sein.")
    if not 0 < min_active <= 100:
        raise ValueError(f"Config {path}: min_active muss größer als 0 und höchstens 100 % sein.")
    if window < 2 * frame_seconds:
        raise ValueError(f"Config {path}: window muss mindestens 0.2 s sein.")
    return make_tool("audible_sound", path, option, partial(_run, threshold, min_dynamics, min_active, window))
