# --------- IMPORTS ---------
from dataclasses import asdict, dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Iterator
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import traceback


# --------- CONFIG ---------
app_name = "AQC"
app_version = "0.2.0"

config_folder = "configs"
res_name = "res"            # folder with ffmpeg/ffprobe, searched before PATH
bar_width = 10
progress_interval = 0.1     # seconds between updates of the running line
status_texts = {True: "BESTANDEN", False: "NICHT BESTANDEN", None: "NICHT AUSFÜHRBAR"}
console_texts = {True: "OK", False: "FEHLER", None: "NICHT AUSFÜHRBAR"}


# --------- STATIC ---------
class _MustFail:
    def __repr__(self) -> str:
        return "must_fail"


must_fail = _MustFail()

bar_full, bar_empty = "\u2588", "\u2591"
si_factors = {"": 1, "k": 10**3, "m": 10**6, "g": 10**9}
number_pattern = re.compile(r"([-+]?\d+(?:\.\d+)?)\s*([kmg]?)", re.IGNORECASE)
seconds_pattern = re.compile(r"(\d+(?:\.\d+)?)s", re.IGNORECASE)
tc_pattern = r"(\d{2}):(\d{2}):(\d{2})[:;](\d{2})"
range_patterns = {
    "all": re.compile(r"all"),
    "start": re.compile(r"start\+(\d+(?:\.\d+)?)s"),
    "end": re.compile(r"end-(\d+(?:\.\d+)?)s"),
    "tc": re.compile(rf"{tc_pattern}-{tc_pattern}"),
}

ProgressFn = Callable[[float, float, float, str], None]     # media_done, media_total, units_done, "frames" | "MB"


# --------- TYPES ---------
@dataclass(frozen=True)
class Failure:
    """One failed criterion of a test."""
    criterion: str
    expected: str
    actual: str
    stream: int | None = None
    channel: int | None = None
    tc: str | None = None


@dataclass
class Outcome:
    """Raw test outcome before must_fail is applied."""
    passed: bool
    failures: list[Failure] = field(default_factory=list)
    info: list[str] = field(default_factory=list)
    media_time: float = 0.0


@dataclass(frozen=True)
class Test:
    """A configured test; runner(clip, progress) returns an Outcome."""
    kind: str
    config: Path
    must_fail: bool
    runner: Callable[["Clip", ProgressFn], Outcome]

    @property
    def name(self) -> str:
        return self.config.stem

    @property
    def label(self) -> str:
        return f"{self.kind}({self.name})"


@dataclass(frozen=True)
class Clip:
    """Probed clip, passed to every test run."""
    path: Path
    duration: float
    fps: Fraction | None
    start_frames: int
    streams: tuple[dict, ...]
    format: dict

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def video(self) -> dict | None:
        """First video stream; it defines frame rate and timecode."""
        return next(iter(self.streams_of("video")), None)

    def streams_of(self, codec_type: str) -> list[dict]:
        """All streams of one type, in file order."""
        # Cover images are reported as video streams by FFprobe.
        return [stream for stream in self.streams if stream.get("codec_type") == codec_type
                and not stream.get("disposition", {}).get("attached_pic")]

    def tc(self, seconds: float) -> str:
        """File timecode hh:mm:ss:ff of a clip position; hh:mm:ss.mmm if the clip has no video."""
        if self.fps is None:
            millis = round(seconds * 1000)
            return f"{millis // 3_600_000:02d}:{millis // 60_000 % 60:02d}:{millis // 1000 % 60:02d}.{millis % 1000:03d}"
        base = round(self.fps)
        frames = (self.start_frames + math.floor(seconds * self.fps + 1e-6)) % (24 * 3600 * base)
        return f"{frames // (3600 * base):02d}:{frames // (60 * base) % 60:02d}:{frames // base % 60:02d}:{frames % base:02d}"

    def tc_seconds(self, parts: tuple[str, ...]) -> float:
        """Clip position in seconds of a file timecode given as (hh, mm, ss, ff)."""
        if self.fps is None:
            raise ValueError("Timecode-Angabe nicht möglich: Clip hat keinen Videostream.")
        return float((_tc_frames(parts, round(self.fps)) - self.start_frames) / self.fps)


@dataclass(frozen=True)
class Range:
    """Parsed range: all, start+Ns, end-Ns or hh:mm:ss:ff-hh:mm:ss:ff (end frame included)."""
    text: str
    kind: str
    values: tuple[str, ...]

    def resolve(self, clip: Clip) -> tuple[float, float]:
        """Return (start, end) in clip seconds, end exclusive, limited to the clip length."""
        if self.kind == "all":
            start, end = 0.0, clip.duration
        elif self.kind == "start":
            start, end = 0.0, float(self.values[0])
        elif self.kind == "end":
            start, end = clip.duration - float(self.values[0]), clip.duration
        else:
            start, end = clip.tc_seconds(self.values[:4]), clip.tc_seconds(self.values[4:]) + float(1 / clip.fps)
        start, end = max(0.0, start), min(clip.duration, end)
        if end <= start:
            raise ValueError(f"Bereich '{self.text}' ist leer oder liegt außerhalb des Clips "
                             f"({clip.tc(0)} bis {clip.tc(clip.duration)}).")
        return start, end


# --------- FUNC VALUES ---------
def _tc_frames(parts: tuple[str, ...], base: int) -> int:
    hours, minutes, seconds, frames = map(int, parts)
    if minutes >= 60 or seconds >= 60 or frames >= base:
        raise ValueError(f"Ungültiger Timecode {':'.join(parts)} bei {base} Bildern pro Sekunde.")
    return ((hours * 60 + minutes) * 60 + seconds) * base + frames


def parse_number(value: Any) -> float | None:
    """Return value as float or None if it is no number.

    Accepts numbers, SI suffixes like '320k', '9M', '1.5G' and fractions like '24000/1001'.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, Fraction)):
        return float(value)
    text = str(value).strip()
    if "/" in text:
        try:
            return float(Fraction(text))
        except (ValueError, ZeroDivisionError):
            return None
    match = number_pattern.fullmatch(text)
    return float(match[1]) * si_factors[match[2].casefold()] if match else None


def parse_seconds(value: Any) -> float:
    """Return a duration like '0.1s' (or a plain number) in seconds; raise ValueError otherwise."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return float(value)
    match = seconds_pattern.fullmatch(str(value).strip())
    if not match:
        raise ValueError(f"Ungültige Dauer '{value}', erwartet z. B. '0.1s'.")
    return float(match[1])


def parse_range(spec: Any) -> Range:
    """Parse 'all', 'start+5s', 'end-50s' or 'hh:mm:ss:ff-hh:mm:ss:ff'; raise ValueError otherwise."""
    text = "".join(str(spec).split()).casefold() if isinstance(spec, str) else ""
    for kind, pattern in range_patterns.items():
        if match := pattern.fullmatch(text):
            return Range(text, kind, match.groups())
    raise ValueError(f"Ungültige Bereichsangabe '{spec}', erlaubt: all, start+5s, end-50s, hh:mm:ss:ff-hh:mm:ss:ff.")


def stream_fps(stream: dict) -> Fraction | None:
    """Exact frame rate of a stream (avg_frame_rate, else r_frame_rate) or None."""
    for key in ("avg_frame_rate", "r_frame_rate"):
        try:
            fps = Fraction(str(stream.get(key, "")))
        except (ValueError, ZeroDivisionError):
            continue
        if fps > 0:
            return fps
    return None


def format_clock(seconds: float) -> str:
    """Format seconds as hh:mm:ss."""
    total = max(0, round(seconds))
    return f"{total // 3600:02d}:{total // 60 % 60:02d}:{total % 60:02d}"


def _format_span(seconds: float) -> str:
    return f"{seconds:.1f}s" if seconds < 60 else format_clock(seconds)


def _format_short(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 60:02d}:{total % 60:02d}" if total < 3600 else format_clock(total)


# --------- FUNC PATHS ---------
def script_folder() -> Path:
    """Folder of the started script, or of the exe in a PyInstaller build."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(sys.argv[0]).resolve().parent if sys.argv and sys.argv[0] else Path.cwd()


def _bundle_folder() -> Path | None:
    # PyInstaller unpacks the exe content to sys._MEIPASS.
    return Path(sys._MEIPASS) if hasattr(sys, "_MEIPASS") else None


def res_folder() -> Path:
    """res/ inside the exe in a PyInstaller build, else next to the aqc package folder."""
    return (_bundle_folder() or Path(__file__).resolve().parents[1]) / res_name


# --------- FUNC CONFIG ---------
def config_path(config: str | Path) -> Path:
    """Resolve a config given as 'name', 'name.toml' or full path.

    Relative names are looked up in configs/ next to the started script, in a PyInstaller build inside the exe.
    """
    path = Path(config)
    if path.suffix.casefold() != ".toml":
        path = path.with_name(f"{path.name}.toml")
    if not path.is_absolute():
        path = (_bundle_folder() or script_folder()) / config_folder / path
    if not path.is_file():
        raise FileNotFoundError(f"Config nicht gefunden: {path}")
    return path


def load_config(config: str | Path, kind: str, allowed: tuple[str, ...] | None = None,
                required: tuple[str, ...] = ()) -> tuple[Path, dict]:
    """Read and check the TOML config of one tool; return (resolved path, data without 'tool').

    The config must contain tool = "<kind>". allowed=None permits any other key.
    Raises ValueError naming the file on any problem.
    """
    path = config_path(config)
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Config {path} ist kein gültiges TOML: {exc}") from exc
    if (tool := data.pop("tool", None)) != kind:
        raise ValueError(f"Config {path} ist für tool = \"{tool}\", nicht für {kind}." if tool else
                         f"Config {path}: Angabe tool = \"{kind}\" fehlt.")
    if missing := [key for key in required if key not in data]:
        raise ValueError(f"Config {path}: Angabe fehlt: {', '.join(missing)}")
    if allowed is not None and (unknown := [key for key in data if key not in allowed]):
        raise ValueError(f"Config {path}: unbekannte Angabe {', '.join(unknown)}, erlaubt: {', '.join(allowed)}")
    return path, data


def make_test(kind: str, config: Path, option: object, runner: Callable[[Clip, ProgressFn], Outcome]) -> Test:
    """Build a Test; option must be None or must_fail."""
    if option is not None and option is not must_fail:
        raise ValueError(f"{kind}({config.stem}): unbekannte Option {option!r}, erlaubt ist nur must_fail.")
    return Test(kind, config, option is must_fail, runner)


# --------- FUNC FFMPEG ---------
def tool_path(tool: str) -> str:
    """Path of ffmpeg or ffprobe: res_folder() first, then the system PATH."""
    executable = shutil.which(tool, path=str(res_folder())) or shutil.which(tool)
    if not executable:
        raise RuntimeError(f"{tool} weder in {res_folder()} noch im Systempfad gefunden.")
    return executable


def _run_tool(tool: str, args: list[str], chunk_size: int | None) -> Iterator:
    executable = tool_path(tool)
    command = [executable, "-v", "error", *(["-nostdin"] if tool == "ffmpeg" else []), *args]
    # stderr goes to a file, a full pipe would block the process.
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errors)
        try:
            if chunk_size is None:
                for line in process.stdout:
                    yield line.decode("utf-8", "replace").rstrip("\r\n")
            else:
                while chunk := process.stdout.read(chunk_size):
                    yield chunk
            process.wait()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()
        if process.returncode:
            errors.seek(0)
            lines = errors.read().decode("utf-8", "replace").splitlines()
            message = " / ".join(line.strip() for line in lines if line.strip())[-1000:]
            raise RuntimeError(f"{tool} fehlgeschlagen (Code {process.returncode}): {message or 'keine Meldung'}")


def ffmpeg_chunks(args: list[str], chunk_size: int) -> Iterator[bytes]:
    """Run ffmpeg (input is only read) and yield stdout in chunks of chunk_size bytes; the last may be shorter."""
    return _run_tool("ffmpeg", args, chunk_size)


def ffprobe_lines(args: list[str]) -> Iterator[str]:
    """Run ffprobe and yield its stdout line by line."""
    return _run_tool("ffprobe", args, None)


def _probe(path: Path) -> Clip:
    if not path.is_file():
        raise FileNotFoundError(f"Clip nicht gefunden: {path}")
    data = json.loads("\n".join(ffprobe_lines(["-print_format", "json", "-show_format", "-show_streams", str(path)])))
    info, streams = data.get("format", {}), tuple(data.get("streams", ()))
    duration = parse_number(info.get("duration"))
    if not duration:
        raise ValueError(f"Clip-Länge nicht lesbar: {path}")
    clip = Clip(path, duration, None, 0, streams, info)
    fps = stream_fps(clip.video) if clip.video else None
    start_tc = next((str(value).strip() for tags in (info.get("tags", {}), *(s.get("tags", {}) for s in streams))
                     for key, value in tags.items() if key.casefold() == "timecode" and str(value).strip()), None)
    if fps is None or start_tc is None:
        return Clip(path, duration, fps, 0, streams, info)
    if not (match := re.fullmatch(tc_pattern, start_tc)):
        raise ValueError(f"Start-TC '{start_tc}' der Datei ist ungültig.")
    return Clip(path, duration, fps, _tc_frames(match.groups(), round(fps)), streams, info)


# --------- FUNC PROGRESS ---------
class _Reporter:
    def __init__(self, clip_name: str, on_progress: Callable[[str, bool], None] | None) -> None:
        self.clip_name, self.callback, self.width = clip_name, on_progress or self._console, 0
        self.label, self.started, self.last = "", 0.0, 0.0

    def _console(self, message: str, done: bool) -> None:
        print(f"\r{message}{' ' * max(0, self.width - len(message))}", end="\n" if done else "", flush=True)
        self.width = 0 if done else len(message)

    def start(self, label: str) -> None:
        self.label, self.started, self.last = label, time.monotonic(), 0.0

    def update(self, media_done: float, media_total: float, units_done: float, unit: str) -> None:
        now = time.monotonic()
        if now - self.last < progress_interval:
            return
        self.last, elapsed = now, now - self.started
        fraction = min(1.0, media_done / media_total) if media_total > 0 else 0.0
        filled = round(fraction * bar_width)
        rate = units_done / elapsed if elapsed else 0.0
        rate_text = f"{rate:.0f} fps" if unit == "frames" else f"{rate:.1f} MB/s"
        speed = media_done / elapsed if elapsed else 0.0
        eta = elapsed / fraction - elapsed if fraction else 0.0
        self.callback(f"{self.clip_name} | {self.label} [{bar_full * filled}{bar_empty * (bar_width - filled)}] "
                      f"{fraction:.0%} {format_clock(media_done)}/{format_clock(media_total)} | "
                      f"{rate_text} {speed:.1f}x | {_format_short(elapsed)} ETA {_format_short(eta)}", False)

    def finish(self, message: str) -> None:
        self.callback(f"{self.clip_name} | {message}", True)


# --------- FUNC CHECK ---------
def _timing_text(entry: dict) -> str:
    if not entry["media_time"]:
        return _format_span(entry["runtime"])
    return (f"{_format_span(entry['media_time'])} Medienzeit in {_format_span(entry['runtime'])} "
            f"({entry['speed']:.1f}x)")


def _config_text(path: Path) -> str:
    bundle = _bundle_folder()
    if bundle and path.is_relative_to(bundle):
        return f"{path.relative_to(bundle).as_posix()} (in der EXE)"
    return str(path)


def _run_test(test: Test, clip: Clip | None, probe_error: str | None, reporter: _Reporter) -> tuple[dict, str | None]:
    entry = {"name": test.name, "kind": test.kind, "label": test.label, "config": _config_text(test.config),
             "must_fail": test.must_fail, "status": status_texts[None], "message": None, "failures": [],
             "info": [], "media_time": 0.0, "runtime": 0.0, "speed": 0.0}
    started, error, state = time.monotonic(), None, None
    reporter.start(test.label)
    try:
        if clip is None:
            raise RuntimeError(f"Clip nicht lesbar: {probe_error}")
        outcome = test.runner(clip, reporter.update)
        state = outcome.passed != test.must_fail
        failures = [Failure("must_fail", "nicht bestanden", "bestanden")] if test.must_fail and outcome.passed \
            else [] if test.must_fail else outcome.failures
        info = outcome.info + ([f"erwartet nicht bestanden: {failure_text(asdict(f))}" for f in outcome.failures]
                               if test.must_fail and not outcome.passed else [])
        entry.update(failures=[asdict(f) for f in failures], info=info, media_time=outcome.media_time)
    except Exception as exc:
        entry["message"] = str(exc) or type(exc).__name__
        detail = "" if isinstance(exc, (ValueError, RuntimeError, OSError)) else f"\n{traceback.format_exc()}"
        error = f"{test.label}: {entry['message']}{detail}"
    entry["status"], entry["runtime"] = status_texts[state], time.monotonic() - started
    entry["speed"] = entry["media_time"] / entry["runtime"] if entry["runtime"] else 0.0
    reporter.finish(f"{test.label}  {console_texts[state]}  {entry['message'] or _timing_text(entry)}")
    return entry, error


def check(clip_path: str | Path, tests: tuple[Test, ...] | list[Test],
          on_progress: Callable[[str, bool], None] | None = None) -> tuple[dict, str, str | None]:
    """Run all tests on one clip and return (result, progress, error).

    result: JSON-ready dict with clip status, per test status, failures, info and timings.
    progress: final summary line. error: text of all technical problems or None.
    on_progress(text, done) gets each running line (done=False) and finished line (done=True);
    without it lines are printed to the console. A test that cannot run fails the clip.
    """
    started = time.monotonic()
    path = Path(clip_path)
    reporter = _Reporter(path.name, on_progress)
    result = {"clip": path.name, "path": str(path), "aqc_version": app_version, "status": status_texts[False],
              "passed": False, "start_tc": None, "duration": 0.0, "processing": 0.0, "speed": 0.0,
              "tests": [], "error": None}
    errors = []
    try:
        tests = tuple(tests)
        if not tests or not all(isinstance(test, Test) for test in tests):
            raise ValueError("tests muss eine nicht leere Liste von AQC-Tests sein.")
        clip, probe_error = None, None
        try:
            clip = _probe(path)
            result.update(start_tc=clip.tc(0.0), duration=clip.duration)
        except (ValueError, RuntimeError, OSError) as exc:
            probe_error = str(exc)
            errors.append(f"{path.name}: {probe_error}")
        for test in tests:
            entry, error = _run_test(test, clip, probe_error, reporter)
            result["tests"].append(entry)
            errors += [error] if error and clip is not None else []
        result["passed"] = all(entry["status"] == status_texts[True] for entry in result["tests"])
        result["status"] = status_texts[result["passed"]]
    except Exception as exc:
        errors.append(f"Unhandled error in aqc: {exc}\n{traceback.format_exc()}")
    result["processing"] = time.monotonic() - started
    result["speed"] = result["duration"] / result["processing"] if result["processing"] else 0.0
    result["error"] = "\n".join(errors) or None
    progress = (f"{result['status']}  {_format_span(result['duration'])} Clip in "
                f"{_format_span(result['processing'])} ({result['speed']:.1f}x), {len(result['tests'])} Tests")
    reporter.finish(progress)
    return result, f"{path.name} | {progress}", result["error"]


# --------- FUNC REPORT ---------
def failure_text(failure: dict) -> str:
    """One line for a failure dict: criterion, expected, actual, stream, channel and TC."""
    parts = [f"Kriterium: {failure['criterion']}", f"Soll: {failure['expected']}", f"Ist: {failure['actual']}"]
    parts += [f"{label} {failure[key]}" for key, label in (("stream", "Stream"), ("channel", "Kanal"), ("tc", "TC"))
              if failure.get(key) is not None]
    return " | ".join(parts)


def report(result: dict) -> str:
    """Plain text report for one check() result."""
    lines = [f"{app_name} {result['aqc_version']} Prüfbericht", "",
             f"Clip:      {result['clip']}",
             f"Pfad:      {result['path']}",
             f"Ergebnis:  {result['status']}",
             f"Start-TC:  {result['start_tc'] or '-'}",
             f"Länge:     {format_clock(result['duration'])}",
             f"Prüfzeit:  {format_clock(result['processing'])} ({result['speed']:.1f}x)"]
    if result["error"]:
        lines += ["", "Technische Probleme:", *(f"  {line}" for line in result["error"].splitlines())]
    for entry in result["tests"]:
        lines += ["", f"[{entry['status']}] {entry['label']}{' mit must_fail' if entry['must_fail'] else ''}",
                  f"  Config:    {entry['config']}",
                  f"  Laufzeit:  {_timing_text(entry)}"]
        lines += [f"  Grund:     {entry['message']}"] if entry["message"] else []
        lines += [f"  Info:      {line}" for line in entry["info"]]
        lines += [f"  Fehler:    {failure_text(failure)}" for failure in entry["failures"]]
    return "\n".join(lines) + "\n"
