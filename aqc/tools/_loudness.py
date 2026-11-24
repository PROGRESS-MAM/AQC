# --------- IMPORTS ---------
from dataclasses import dataclass
from typing import Callable, Iterable, Iterator
import re
from ..aqc import Clip, ffmpeg_chunks


# --------- CONFIG ---------
filter_text = "ebur128=peak=true:metadata=1,ametadata=mode=print:file=-"
chunk_size = 65536
gate_floor = -70.0


# --------- STATIC ---------
i_pattern = re.compile(r"lavfi\.r128\.I=([-+]?\d+(?:\.\d+)?)")
peak_pattern = re.compile(r"lavfi\.r128\.true_peaks_ch(\d+)=([-+]?\d+(?:\.\d+)?)")
pts_pattern = re.compile(r"pts_time:([-+]?\d+(?:\.\d+)?)")


# --------- TYPES ---------
@dataclass(frozen=True)
class Loudness:
    """Integrated loudness and linear true peak per channel."""
    integrated: float | None
    true_peaks: tuple[float, ...]


# --------- FUNC ---------
def _lines(chunks: Iterable[bytes]) -> Iterator[str]:
    pending = b""
    for chunk in chunks:
        lines = (pending + chunk).split(b"\n")
        pending = lines.pop()
        for line in lines:
            yield line.decode("utf-8", "replace").removesuffix("\r")
    if pending:
        yield pending.decode("utf-8", "replace")


def measure_loudness(clip: Clip, stream: dict, report: Callable[[float], None]) -> Loudness:
    """Measure integrated LUFS and per-channel linear true peak for one audio stream."""
    index = stream["index"]
    try:
        channels = int(stream["channels"])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"Audiostream {index}: Kanalzahl nicht lesbar.") from exc
    if channels < 1:
        raise ValueError(f"Audiostream {index}: Kanalzahl nicht lesbar.")

    args = ["-i", str(clip.path), "-map", f"0:{index}", "-af", filter_text, "-f", "null", "-"]
    integrated = None
    peaks = {}
    for line in _lines(ffmpeg_chunks(args, chunk_size)):
        if match := i_pattern.search(line):
            integrated = float(match[1])
        if match := peak_pattern.search(line):
            channel = int(match[1])
            if channel < channels:
                peaks[channel] = max(peaks.get(channel, 0.0), float(match[2]))
        if match := pts_pattern.search(line):
            report(float(match[1]))

    if integrated is None:
        raise RuntimeError(f"Audiostream {index}: Lautheit nicht messbar.")
    return Loudness(None if integrated <= gate_floor else integrated,
                    tuple(peaks.get(channel, 0.0) for channel in range(channels)))
