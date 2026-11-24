# --------- IMPORTS ---------
from dataclasses import dataclass
from typing import Callable
import math
import numpy as np
from ..aqc import Clip, ffmpeg_chunks, parse_number


# --------- CONFIG ---------
frame_seconds = 0.1
low_cut = 100.0
band_edges = tuple(500 * 2 ** (i - 0.5) for i in range(6))
high_bands = 2
chunk_seconds = 4
sample_bytes = 4


# --------- TYPES ---------
@dataclass(frozen=True)
class Analysis:
    window_samples: np.ndarray
    mean_square: np.ndarray
    dynamics: np.ndarray
    content: np.ndarray
    peak: np.ndarray

    @property
    def samples(self) -> int:
        """Total decoded samples per channel across all windows."""
        return int(self.window_samples.sum())


# --------- FUNC SAMPLES ---------
def _window_stats(frames: np.ndarray, rate: int, floor_db: float) -> tuple[np.ndarray, np.ndarray]:
    """Return per-window spectral dynamics and octave-band content."""
    frame = frames.shape[2]
    hann = np.hanning(frame)
    power = np.abs(np.fft.rfft(frames * hann[None, None, :, None], axis=2)) ** 2
    power *= 2 / (frame * np.sum(hann ** 2))
    frequencies = np.fft.rfftfreq(frame, 1 / rate)
    bands = [power[:, :, frequencies >= low_cut, :].sum(axis=2)]
    bands.extend(power[:, :, (frequencies >= lower) & (frequencies < upper), :].sum(axis=2)
                 for lower, upper in zip(band_edges, band_edges[1:]))
    band_power = np.stack(bands, axis=2)
    levels = 10 * np.log10(np.maximum(band_power, 10 ** (floor_db / 10)))
    dynamics = (np.percentile(levels, 90, axis=1) - np.percentile(levels, 10, axis=1)).max(axis=1)
    content = (np.percentile(band_power[:, :, 1:, :], 90, axis=1)
               - np.percentile(band_power[:, :, 1:, :], 10, axis=1)).transpose(0, 2, 1)
    return dynamics, content


def analyse_samples(values: np.ndarray, rate: int, window: float, floor_db: float) -> Analysis:
    """Measure per-window level dynamics, octave-band content, mean square and channel peaks."""
    values = np.asarray(values, dtype=np.float64)
    if values.ndim != 2:
        raise ValueError("values müssen die Form (Samples, Kanäle) haben.")
    channels = values.shape[1]
    if rate < 1:
        raise ValueError("rate muss größer als 0 sein.")

    frame = round(frame_seconds * rate)
    frames_per_window = max(1, round(window / frame_seconds))
    window_len = frame * frames_per_window
    sample_count = len(values)
    peak = np.max(np.abs(values), axis=0) if sample_count else np.zeros(channels, dtype=np.float64)
    if not sample_count:
        return Analysis(np.zeros(0, dtype=np.int64), np.zeros((0, channels), dtype=np.float64),
                        np.zeros((0, channels), dtype=np.float64), np.zeros((0, channels, 5), dtype=np.float64), peak)

    full_count, tail_len = divmod(sample_count, window_len)
    window_samples = np.full(full_count, window_len, dtype=np.int64)
    mean_square_parts, dynamics_parts, content_parts = [], [], []
    if full_count:
        full = values[:full_count * window_len].reshape(full_count, window_len, channels)
        mean_square_parts.append(np.square(full).mean(axis=1))
        frames = full.reshape(full_count, frames_per_window, frame, channels)
        dynamics, content = _window_stats(frames, rate, floor_db)
        dynamics_parts.append(dynamics)
        content_parts.append(content)

    if tail_len:
        tail = values[full_count * window_len:]
        window_samples = np.append(window_samples, tail_len)
        mean_square_parts.append(np.square(tail).mean(axis=0, keepdims=True))
        frame_count = len(tail) // frame
        if not frame_count:
            dynamics_parts.append(np.zeros((1, channels), dtype=np.float64))
            content_parts.append(np.zeros((1, channels, 5), dtype=np.float64))
        else:
            tail_frames = tail[:frame_count * frame].reshape(frame_count, frame, channels)
            dynamics, content = _window_stats(tail_frames[None], rate, floor_db)
            dynamics_parts.append(dynamics)
            content_parts.append(content)

    return Analysis(window_samples, np.concatenate(mean_square_parts), np.concatenate(dynamics_parts),
                    np.concatenate(content_parts), peak)


# --------- FUNC STREAM ---------
def analyse_stream(clip: Clip, stream: dict, window: float, floor_db: float,
                   report: Callable[[int, int], None]) -> Analysis:
    """Decode one audio stream and return the same per-window analysis as analyse_samples."""
    try:
        channels = int(stream.get("channels") or 0)
    except (TypeError, ValueError, OverflowError):
        channels = 0
    if channels < 1:
        raise ValueError(f"Audiostream {stream['index']}: Kanalzahl nicht lesbar.")
    try:
        rate = int(parse_number(stream.get("sample_rate")) or 0)
    except (TypeError, ValueError, OverflowError):
        rate = 0
    if rate < 1:
        raise ValueError(f"Audiostream {stream['index']}: Samplerate nicht lesbar.")

    frame = round(frame_seconds * rate)
    frames_per_window = max(1, round(window / frame_seconds))
    window_samples = frame * frames_per_window
    frame_bytes = channels * sample_bytes
    window_bytes = window_samples * frame_bytes
    chunk_size = window_bytes * max(1, math.ceil(chunk_seconds * rate / window_samples))
    args = ["-i", str(clip.path), "-map", f"0:{stream['index']}", "-c:a", "pcm_f32le", "-f", "f32le", "-"]
    analyses, rest, samples_done, bytes_done = [], b"", 0, 0

    for chunk in ffmpeg_chunks(args, chunk_size):
        data = rest + chunk
        cut = len(data) // window_bytes * window_bytes
        if cut:
            values = np.frombuffer(data[:cut], dtype="<f4").reshape(-1, channels)
            result = analyse_samples(values, rate, window, floor_db)
            analyses.append(result)
            samples_done += result.samples
        rest, bytes_done = data[cut:], bytes_done + len(chunk)
        report(samples_done, bytes_done)

    tail_bytes = len(rest) // frame_bytes * frame_bytes
    if tail_bytes:
        values = np.frombuffer(rest[:tail_bytes], dtype="<f4").reshape(-1, channels)
        result = analyse_samples(values, rate, window, floor_db)
        analyses.append(result)
        samples_done += result.samples
    if not samples_done:
        raise ValueError(f"Audiostream {stream['index']}: keine Samples dekodierbar.")

    return Analysis(np.concatenate([result.window_samples for result in analyses]),
                    np.concatenate([result.mean_square for result in analyses]),
                    np.concatenate([result.dynamics for result in analyses]),
                    np.concatenate([result.content for result in analyses]),
                    np.maximum.reduce([result.peak for result in analyses]))
