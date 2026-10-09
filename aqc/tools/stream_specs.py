# --------- IMPORTS ---------
from functools import partial
from fractions import Fraction
from pathlib import Path
from typing import Any
from ..aqc import Clip, Failure, Outcome, ProgressFn, Tool, ffprobe_lines, load_config, make_tool, parse_number, stream_fps


# --------- CONFIG ---------
stream_types = {"video": "v", "audio": "a", "subtitle": "s", "data": "d", "attachment": "t"}
bitrate_sources = ("file", "measured")
reserved_keys = ("stream", "bitrate_source")
interlaced_orders = ("tt", "bb", "tb", "bt")


# --------- FUNC VALUES ---------
def _scan(stream: dict) -> str | None:
    order = stream.get("field_order")
    return "progressive" if order == "progressive" else "interlaced" if order in interlaced_orders else None


fixed_values = {
    "width": lambda stream: stream.get("width"),
    "height": lambda stream: stream.get("height"),
    "scan": _scan,
    "fps": stream_fps,
    "codec": lambda stream: stream.get("codec_name"),
    "bitrate": lambda stream: parse_number(stream.get("bit_rate")),
    "channels": lambda stream: stream.get("channels"),
    "sample_rate": lambda stream: parse_number(stream.get("sample_rate")),
}


def _field(stream: dict, key: str) -> Any:
    if key in stream:
        return stream[key]
    return next((value for name, value in stream.get("tags", {}).items() if name.casefold() == key.casefold()), None)


def _stream_values(stream: dict, keys: tuple[str, ...], measured: dict[int, float] | None) -> dict[str, Any]:
    values = {key: getter(stream) for key, getter in fixed_values.items()}
    if measured is not None:
        values["bitrate"] = measured.get(stream["index"])
    values |= {key: _field(stream, key) for key in keys if key not in fixed_values}
    return {key: value for key, value in values.items() if value is not None or key in keys}


def _matches(actual: Any, expected: Any) -> bool:
    if actual is None:
        return False
    if isinstance(expected, list):
        return any(_matches(actual, item) for item in expected)
    number = parse_number(actual)
    if isinstance(expected, dict):
        return number is not None and all(
            compare(number, parse_number(expected[key])) for key, compare in
            (("min", float.__ge__), ("max", float.__le__)) if key in expected)
    wanted = parse_number(expected)
    if number is not None and wanted is not None:
        # 3 decimals: 23.976 matches 24000/1001, but 24 does not.
        return round(number, 3) == round(wanted, 3)
    return str(actual).casefold() == str(expected).casefold()


def _show(value: Any, key: str = "") -> str:
    if value is None:
        return "-"
    if isinstance(value, Fraction):
        return str(value.numerator) if value.denominator == 1 else f"{value} ({float(value):.3f})"
    number = parse_number(value) if key == "bitrate" else None
    if number is not None:
        return next((f"{number / factor:.4g}{suffix}" for suffix, factor in (("G", 1e9), ("M", 1e6), ("k", 1e3))
                     if abs(number) >= factor), f"{number:g}")
    return f"{value:g}" if isinstance(value, float) else str(value)


def _show_expected(expected: Any) -> str:
    if isinstance(expected, list):
        return " | ".join(map(str, expected))
    if isinstance(expected, dict):
        low, high = expected.get("min"), expected.get("max")
        return f"{low} bis {high}" if low is not None and high is not None else \
            f">= {low}" if low is not None else f"<= {high}"
    return str(expected)


# --------- FUNC RUN ---------
def _measure(clip: Clip, stream_type: str, streams: list[dict], progress: ProgressFn) -> dict[int, float]:
    """Average bitrate per stream from the packet sizes of the whole clip."""
    sizes = {stream["index"]: 0 for stream in streams}
    total_bytes, position = 0, 0.0
    args = ["-select_streams", stream_types[stream_type], "-show_entries", "packet=stream_index,size,pts_time",
            "-of", "compact=p=0", str(clip.path)]
    for line in ffprobe_lines(args):
        entry = dict(part.split("=", 1) for part in line.split("|") if "=" in part)
        index = int(parse_number(entry.get("stream_index")) or 0)
        if index not in sizes:
            continue
        size = int(parse_number(entry.get("size")) or 0)
        sizes[index] += size
        total_bytes += size
        position = max(position, parse_number(entry.get("pts_time")) or 0.0)
        progress(min(position, clip.duration), clip.duration, total_bytes / 1e6, "MB")
    durations = {stream["index"]: parse_number(stream.get("duration")) or clip.duration for stream in streams}
    return {index: size * 8 / durations[index] for index, size in sizes.items()}


def _run(stream_type: str, source: str, criteria: tuple[tuple[str, Any], ...], clip: Clip,
         progress: ProgressFn) -> Outcome:
    streams = clip.streams_of(stream_type)
    if not streams:
        return Outcome(False, [Failure("stream", f"mindestens ein {stream_type}-Stream", "keiner")])
    keys = tuple(key for key, _ in criteria)
    measure = source == "measured" and "bitrate" in keys
    measured = _measure(clip, stream_type, streams, progress) if measure else None
    failures, info = [], [f"{len(streams)} {stream_type}-Stream(s) geprüft"
                          + (f", Bitrate aus {'Messung' if measure else 'Datei'}" if "bitrate" in keys else "")]
    for stream in streams:
        values = _stream_values(stream, keys, measured)
        missed = [Failure(key, _show_expected(expected), _show(values.get(key), key), stream=stream["index"])
                  for key, expected in criteria if not _matches(values.get(key), expected)]
        failures += missed
        if missed:
            info.append(f"Stream {stream['index']} Ist-Werte: "
                        + ", ".join(f"{key}={_show(value, key)}" for key, value in values.items()))
    return Outcome(not failures, failures, info, clip.duration if measure else 0.0)


# --------- FUNC CONFIG ---------
def _check_expected(path: Path, key: str, value: Any) -> None:
    scalar = (str, int, float, bool)
    if isinstance(value, dict):
        if not value or set(value) - {"min", "max"} or any(parse_number(bound) is None for bound in value.values()):
            raise ValueError(f"Config {path}: {key} als Bereich braucht min und/oder max als Zahlen.")
    elif isinstance(value, list):
        if not value or not all(isinstance(item, scalar) for item in value):
            raise ValueError(f"Config {path}: {key} als Liste braucht mindestens einen einfachen Wert.")
    elif not isinstance(value, scalar):
        raise ValueError(f"Config {path}: {key} muss Wert, Liste oder {{min, max}} sein.")


def stream_specs(config: str | Path, option: object = None) -> Tool:
    """Check properties of all streams of one type against the config.

    config: name of configs/<name>.toml or a path; TOML with tool = "stream_specs", stream ("video", "audio", ...), optional bitrate_source ("file"/"measured")
    and criteria (fixed names or any FFprobe stream field) as value, list or {min, max}.
    option: None or must_fail.
    """
    path, data = load_config(config, "stream_specs", required=("stream",))
    stream_type, source = data["stream"], data.get("bitrate_source", "file")
    if stream_type not in stream_types:
        raise ValueError(f"Config {path}: stream muss einer von {', '.join(stream_types)} sein.")
    if source not in bitrate_sources:
        raise ValueError(f"Config {path}: bitrate_source muss {' oder '.join(bitrate_sources)} sein.")
    criteria = tuple((key, value) for key, value in data.items() if key not in reserved_keys)
    if not criteria:
        raise ValueError(f"Config {path}: keine Kriterien angegeben.")
    for key, value in criteria:
        _check_expected(path, key, value)
    return make_tool("stream_specs", path, option, partial(_run, stream_type, source, criteria))
