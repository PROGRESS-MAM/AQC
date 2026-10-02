# --------- IMPORTS ---------
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
import inspect
import json
import os
import aqc
import aqc.tests
from aqc.aqc import status_texts
from toolbox import tb_write_log


# --------- CONFIG ---------
app_name = "QC Worker"
app_version = "1.0.0"

report_suffix = ".aqc.json"
system_files = frozenset({"thumbs.db", "desktop.ini", ".ds_store"})
test_keys = ("test", "config", "must_fail")
states = ("ok", "rejected", "error")       # also the order of severity


# --------- TYPES ---------
@dataclass(frozen=True)
class ClipResult:
    """Result of one clip: state ok | rejected | error, short summary and path of the JSON report."""
    source: Path
    state: str
    summary: str
    report: Path | None


@dataclass(frozen=True)
class QCResult:
    """Result of one process() call; state is the worst clip state, error if nothing could be checked."""
    state: str
    summary: str
    clips: list[ClipResult] = field(default_factory=list)
    versions: dict[str, str] = field(default_factory=dict)


# --------- FUNC INFO ---------
def versions() -> dict[str, str]:
    """Versions of QC worker and AQC core."""
    return {"qc_worker": app_version, "aqc": aqc.app_version}


def methods() -> dict[str, Callable]:
    """Test methods of aqc.tests by name; new methods in the core are found without changes here."""
    return {name: item for name, item in vars(aqc.tests).items()
            if inspect.isfunction(item) and not name.startswith("_")}


# --------- FUNC INPUT ---------
def _log(log_path: Path | None, message: str) -> None:
    if not log_path:
        return
    try:
        tb_write_log(log_path, message)
    except OSError as exc:
        print(f"Log nicht schreibbar: {exc}")


def _ignored(path: Path) -> bool:
    name = path.name.casefold()
    return name in system_files or name.startswith(".") or name.endswith(report_suffix)


def _collect(sources: list[str | Path], recursive: bool) -> list[Path]:
    if isinstance(sources, (str, Path)) or not sources:
        raise ValueError("sources muss eine nicht leere Liste von Dateien oder Ordnern sein.")
    files, seen = [], set()
    for source in map(Path, sources):
        if source.is_file():
            found = [source]
        elif source.is_dir():
            items = source.rglob("*") if recursive else source.iterdir()
            found = sorted((item for item in items if item.is_file() and not _ignored(item)),
                           key=lambda item: str(item).casefold())
        else:
            raise FileNotFoundError(f"Quelle nicht gefunden: {source}")
        for path in found:
            if (key := os.path.normcase(path.resolve())) not in seen:
                seen.add(key)
                files.append(path)
    if not files:
        raise ValueError("In den Quellen wurden keine Dateien gefunden.")
    return files


def _build_tests(tests: list[dict[str, Any]]) -> list[aqc.aqc.Test]:
    if not isinstance(tests, (list, tuple)) or not tests:
        raise ValueError("tests muss eine nicht leere Liste sein.")
    available, built = methods(), []
    for number, spec in enumerate(tests, 1):
        if not isinstance(spec, dict) or (unknown := set(spec) - set(test_keys)):
            raise ValueError(f"Test {number}: erwartet ein dict mit {', '.join(test_keys)}"
                             + (f", unbekannt: {', '.join(sorted(unknown))}" if isinstance(spec, dict) else "") + ".")
        name, config, must = spec.get("test"), spec.get("config"), spec.get("must_fail", False)
        if name not in available:
            raise ValueError(f"Test {number}: unbekannte Prüfmethode {name!r}, verfügbar: {', '.join(sorted(available))}.")
        if not isinstance(config, (str, Path)) or not str(config).strip():
            raise ValueError(f"Test {number}: config fehlt.")
        if not isinstance(must, bool):
            raise ValueError(f"Test {number}: must_fail muss true oder false sein.")
        built.append(available[name](config, aqc.must_fail if must else None))
    return built


# --------- FUNC CHECK ---------
def _clip_state(result: dict) -> str:
    if result["passed"]:
        return "ok"
    unrunnable = any(entry["status"] == status_texts[None] for entry in result["tests"])
    return "error" if result["error"] or unrunnable or not result["tests"] else "rejected"


def _clip_summary(result: dict, state: str) -> str:
    tests = result["tests"]
    if state == "ok":
        return f"{result['clip']}: bestanden ({len(tests)} Tests)"
    if state == "rejected":
        failed = [entry["label"] for entry in tests if entry["status"] == status_texts[False]]
        return f"{result['clip']}: nicht bestanden ({len(failed)} von {len(tests)} Tests): {', '.join(failed)}"
    reason = next((line for line in (result["error"] or "").splitlines() if line.strip()), "Test nicht ausführbar")
    return f"{result['clip']}: nicht ausführbar: {reason}"


def _report_path(folder: Path, clip: Path, used: set[str]) -> Path:
    path, number = folder / f"{clip.name}{report_suffix}", 0
    while os.path.normcase(path) in used or path.exists():
        number += 1
        path = folder / f"{clip.name}-{number}{report_suffix}"
    used.add(os.path.normcase(path))
    return path


def _check_clip(path: Path, tests: list, report_folder: Path, used: set[str], log_path: Path | None) -> ClipResult:
    result, progress, _ = aqc.check(path, tests)
    state = _clip_state(result)
    summary = _clip_summary(result, state)
    _log(log_path, progress)
    report = _report_path(report_folder, path, used)
    data = {"qc_worker": app_name, "qc_worker_version": app_version, "state": state, "summary": summary, **result}
    try:
        report.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    except OSError as exc:
        return ClipResult(path, "error", f"{path.name}: Bericht nicht schreibbar: {exc}", None)
    return ClipResult(path, state, summary, report)


def _summary(clips: list[ClipResult]) -> str:
    if len(clips) == 1:
        return clips[0].summary
    counts = {state: sum(clip.state == state for clip in clips) for state in states}
    head = f"{len(clips)} Clips: {counts['ok']} bestanden, {counts['rejected']} nicht bestanden, {counts['error']} nicht ausführbar"
    return "; ".join([head, *(clip.summary for clip in clips if clip.state != "ok")])


# --------- FUNC PUBLIC ---------
def process(sources: list[str | Path], tests: list[dict[str, Any]], report_folder: str | Path,
            recursive: bool = False, log_path: Path | None = None) -> QCResult:
    """Check all files of sources with the given tests and write one JSON report per clip.

    sources: files and/or folders; folders without subfolders unless recursive=True; system and dot files are skipped.
    tests: [{"test": <aqc.tests method>, "config": <name in configs/ or path to TOML>, "must_fail": bool}, ...].
    report_folder: target of <clip>.aqc.json, created if missing; existing reports are not overwritten.
    Returns QCResult; state error if sources or tests are invalid or a clip could not be checked.
    """
    _log(log_path, f"{app_name} {app_version} (AQC {aqc.app_version}) gestartet.")
    try:
        files, built = _collect(sources, recursive), _build_tests(tests)
        folder = Path(report_folder)
        folder.mkdir(parents=True, exist_ok=True)
    except (ValueError, OSError, RuntimeError) as exc:
        _log(log_path, f"Prüfung nicht möglich: {exc}")
        return QCResult("error", f"Prüfung nicht möglich: {exc}", [], versions())
    _log(log_path, f"{len(files)} Datei(en), {len(built)} Tests, Berichte nach {folder}")
    used: set[str] = set()
    clips = [_check_clip(path, built, folder, used, log_path) for path in files]
    state = max((clip.state for clip in clips), key=states.index)
    result = QCResult(state, _summary(clips), clips, versions())
    _log(log_path, f"Ergebnis {state}: {result.summary}")
    return result
