# --------- IMPORTS ---------
from datetime import datetime
from pathlib import Path
from typing import Callable
import csv
import json
import os
import sys
import tomllib
import traceback
import unicodedata
from toolbox import tb_write_log
from marathon import marathon_adapter as adapter
import qc_worker


# --------- CONFIG ---------
app_name = "FTP Marathon"
app_version = "1.1.0"

root_path = Path(r"\\10.0.77.11\Ablage KI Proxy_1\Proxy 10 Mbit")   # like root_path in Marathon's res/config.ini
work_dir = ".marathon"
poll_seconds = 30
heartbeat_seconds = 30
stage = "FTP"
plan_name = "ftp_ton"
plan = [
    {"tool": "audible_sound", "config": "hoerbarer_ton"},
    {"tool": "sound_quality", "config": "ton_qualitaet"},
]
state_version = 8
copy_chunk = 8 * 1024 * 1024
job_status = {"ok": "ok", "rejected": "rejected", "error": "failed"}
csv_columns = (
    "clip_id", "kollektion", "identifier", "titel", "datei", "status", "kurztext",
    *(column for entry in plan for column in (entry["tool"], f"{entry['tool']}_werte")), "bericht",
)


# --------- STATIC ---------
def _bundle_folder() -> Path:
    return Path(sys._MEIPASS) if hasattr(sys, "_MEIPASS") else Path(__file__).resolve().parents[1]


def _run_folder() -> Path:
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


config_folder = _bundle_folder() / "marathon" / "configs"
main_log = _run_folder() / "ftp_marathon.log"


# --------- FUNC PLAN ---------
def _tools() -> list[dict]:
    return [{**entry, "config": config_folder / f"{entry['config']}.toml"} for entry in plan]


def _check_plan() -> None:
    available = qc_worker.methods()
    problems = []
    for entry in _tools():
        name, config = entry["tool"], entry["config"]
        known = name in available
        if not known:
            problems.append(f"unbekanntes Prüftool {name!r}")
        if not config.is_file():
            problems.append(f"Config fehlt: {config}")
            continue
        try:
            config.read_text(encoding="utf-8")
            if known:
                available[name](config)
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            problems.append(f"Config {config} nicht lesbar: {exc}")
        except ValueError as exc:
            problems.append(f"Config {config} ungültig: {exc}")
    if problems:
        raise ValueError(f"Prüfplan ungültig: {'; '.join(problems)}")
    _say(f"Prüfplan: {plan_name} mit {len(plan)} Tools.")


# --------- FUNC COPY ---------
def copy_to_ftp(source: Path, target: Path, size: int, progress: Callable[[int], None]) -> None:
    try:
        if target.stat().st_size == size:
            return
    except FileNotFoundError:
        pass

    part = target.with_name(f".{target.name}.part")
    try:
        bytes_done = 0
        with source.open("rb") as source_handle, part.open("wb") as target_handle:
            while block := source_handle.read(copy_chunk):
                target_handle.write(block)
                bytes_done += len(block)
                progress(bytes_done)
            target_handle.flush()
            os.fsync(target_handle.fileno())
        os.replace(part, target)
    finally:
        part.unlink(missing_ok=True)

    actual = target.stat().st_size
    if actual != size:
        raise RuntimeError(f"Kopie hat {actual} Bytes statt {size}")


# --------- FUNC JOB ---------
def _short_path(path: str | Path) -> Path:
    parts = Path(path).parts
    return Path(*parts[parts.index(work_dir):]) if work_dir in parts else Path(path)


def _short_report(message: str) -> str:
    head, sep, report = message.partition(" | Prüfbericht: ")
    return f"{head}{sep}{_short_path(report)}" if sep else message


def _console(message: str, done: bool) -> None:
    if done:
        adapter.log(_short_report(message))
    else:
        adapter.status(message.split(" | ", 1)[-1])


def _size_text(path: Path) -> str:
    try:
        size = path.stat().st_size
    except OSError as exc:
        return f"nicht lesbar ({exc})"
    return f"{size / 1e9:.2f} GB" if size >= 1e9 else f"{size / 1e6:.1f} MB"


def _job_problem(job: dict) -> str | None:
    value = job.get("input")
    if not isinstance(value, str) or not value:
        return "input fehlt"
    size = job.get("size")
    if type(size) is not int or size < 1:
        return "size fehlt oder ist keine positive Ganzzahl"
    ftp_dir = job.get("ftp_dir")
    if not isinstance(ftp_dir, str) or not ftp_dir or not Path(ftp_dir).is_absolute():
        return "ftp_dir fehlt oder ist kein vollständiger Pfad"
    ftp_name = job.get("ftp_name")
    if (not isinstance(ftp_name, str) or not ftp_name
            or any(char in '<>:"/\\|?*' or unicodedata.category(char) == "Cc" for char in ftp_name)
            or ftp_name in {".", ".."} or ftp_name.endswith((".", " "))):
        return "ftp_name fehlt oder ist kein gültiger Dateiname"
    return None


def run_job(job: dict, output_folder: Path, log_path: Path) -> adapter.JobReport:
    if problem := _job_problem(job):
        return adapter.JobReport("failed", f"Job ungültig: {problem}", plan_name)

    source = adapter.share_path(job["input"])
    adapter.log(f"Clip {job.get('clip_id', '?')}, {job.get('identifier', '')}, Prüfplan {plan_name} ({len(plan)} Tools)")
    adapter.log(f"Eingang: {_short_path(source)}, {_size_text(source)}")
    try:
        actual = source.stat().st_size
    except OSError as exc:
        return adapter.JobReport("failed", f"Quelle nicht lesbar: {exc}", plan_name)
    if actual != job["size"]:
        return adapter.JobReport("failed", f"Quelle hat {actual} Bytes statt {job['size']} laut Job", plan_name)

    result = qc_worker.process([source], _tools(), output_folder, on_progress=_console)
    if result.state != "ok":
        return adapter.JobReport(job_status[result.state], result.summary, plan_name)

    target = Path(job["ftp_dir"]) / job["ftp_name"]
    adapter.log(f"Kopie nach {target} …")

    try:
        copy_to_ftp(
            source,
            target,
            job["size"],
            lambda done: adapter.status(f"Kopie {done / 1e6:.0f}/{job['size'] / 1e6:.0f} MB"),
        )
    except (OSError, RuntimeError) as exc:
        return adapter.JobReport("failed", f"Kopie auf den FTP fehlgeschlagen: {exc}", plan_name)

    adapter.log(f"Kopiert: {target}, {job['size']} Bytes")
    return adapter.JobReport("ok", result.summary, plan_name, ftp_file=job["ftp_name"], size=job["size"])


# --------- FUNC VORAB ---------
def ready_clips(state_file: Path) -> list[dict]:
    try:
        text = state_file.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"marathon.json nicht gefunden: {state_file}") from exc
    try:
        state = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError):
        state = None
    if not isinstance(state, dict) or state.get("schema_version") != state_version or not isinstance(state.get("clips"), dict):
        raise ValueError(f"{state_file}: keine Marathon-JSON Version {state_version}.")

    ready = []
    for clip in state["clips"].values():
        if not isinstance(clip, dict) or clip.get("ready") is not True or clip.get("active") is not True:
            continue
        files = clip.get("files")
        proxy = files.get("proxy") if isinstance(files, dict) else None
        ftp = clip.get("ftp") or {}
        if (not isinstance(proxy, dict) or not isinstance(proxy.get("folder"), str)
                or not isinstance(proxy.get("name"), str) or not isinstance(ftp, dict) or ftp.get("copied_at")):
            continue
        ready.append(clip)
    return sorted(ready, key=lambda clip: int(clip["clip_id"]))


def clip_source(clip: dict) -> Path:
    proxy = clip["files"]["proxy"]
    folder, name = Path(proxy["folder"]), proxy["name"]
    return folder / name if folder.is_absolute() else root_path.joinpath(*proxy["folder"].split("/"), name)


def _tool_values(report: Path | None, tool: str) -> tuple[str, str]:
    if report is None:
        return "", ""
    data = json.loads(report.read_text(encoding="utf-8"))
    entry = next((item for item in data.get("tools", []) if item.get("kind") == tool), None)
    if entry is None:
        return "", ""
    failures = []
    for failure in entry.get("failures", []):
        parts = [failure.get("criterion")]
        if failure.get("stream") is not None:
            parts.append(f"Stream {failure['stream']}")
        if failure.get("channel") is not None:
            parts.append(f"Kanal {failure['channel']}")
        description = " ".join(str(part) for part in parts if part is not None)
        if failure.get("actual") is not None:
            description += f": {failure['actual']}"
        failures.append(description)
    status = entry.get("status", "")
    if failures:
        status += ": " + "; ".join(failures)
    return status, " | ".join(entry.get("info", []))


def run_vorab(state_file: Path, folder: Path) -> Path:
    clips = ready_clips(state_file)
    reports = folder / "berichte"
    reports.mkdir(parents=True, exist_ok=True)
    csv_path = folder / "vorab.csv"
    counts = {"ok": 0, "rejected": 0, "error": 0}
    completed = 0
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(csv_columns)
        try:
            for i, clip in enumerate(clips, 1):
                source = clip_source(clip)
                _say(f"[{i}/{len(clips)}] Clip {clip['clip_id']} {clip.get('identifier', '')}: {source}")
                result = qc_worker.process([source], _tools(), reports)
                clip_result = result.clips[0] if result.clips else None
                report = clip_result.report if clip_result else None
                row = [clip["clip_id"], clip.get("collection", ""), clip.get("identifier", ""),
                       clip.get("title", ""), source.name, result.state, result.summary]
                for entry in plan:
                    row.extend(_tool_values(report, entry["tool"]))
                row.append(str(report) if report else "")
                writer.writerow(row)
                handle.flush()
                completed += 1
                if result.state in counts:
                    counts[result.state] += 1
                _say(f"[{i}/{len(clips)}] Clip {clip['clip_id']}: {result.state} - {result.summary}")
        except KeyboardInterrupt:
            _say(f"Vorab-Prüfung mit Strg+C abgebrochen nach {completed} von {len(clips)} Clips. Liste: {csv_path}")
            return csv_path
    _say(f"Vorab-Prüfung fertig: {len(clips)} Clips, {counts['ok']} ok, {counts['rejected']} rejected, "
         f"{counts['error']} error. Liste: {csv_path}")
    return csv_path


# --------- FUNC CONSOLE ---------
def _say(message: str) -> None:
    print(f"{datetime.now():%H:%M:%S} {message}", flush=True)
    try:
        tb_write_log(main_log, message)
    except OSError as exc:
        print(f"Log nicht schreibbar: {exc}", flush=True)


def _ask_mode(ask: Callable[[str], str] = input) -> str:
    prompt = "Modus wählen: [1] Produktion (Jobs von Marathon, Kopie auf den FTP)  [2] Vorab-Prüfung (bereite Clips aus marathon.json, keine Kopie)"
    while True:
        print(prompt, flush=True)
        try:
            answer = ask("").strip()
        except EOFError:
            return "produktion"
        if answer == "1":
            return "produktion"
        if answer == "2":
            return "vorab"


def _ask_state_file(ask: Callable[[str], str] = input) -> Path:
    while True:
        try:
            answer = ask("Pfad zu Marathons state/marathon.json: ")
        except EOFError as exc:
            raise RuntimeError("Keine Eingabe für den Pfad zur marathon.json.") from exc
        path = Path(answer.strip().strip('"').strip("'").strip())
        if path.is_file():
            return path
        _say(f"Datei nicht gefunden: {path}")


def _pause() -> None:
    try:
        input("Fertig - Enter schließt das Fenster.")
    except EOFError:
        pass


# --------- MAIN ---------
def _run_production() -> None:
    versions = {"ftp_marathon": app_version, "marathon_adapter": adapter.app_version, **qc_worker.versions()}
    adapter.run(adapter.Settings(root_path, work_dir, stage, poll_seconds, heartbeat_seconds, main_log, versions), run_job)


def main() -> None:
    _say(f"{app_name} {app_version} gestartet, Versionen {qc_worker.versions()}")
    _check_plan()
    mode = _ask_mode()
    if mode == "vorab":
        run_vorab(_ask_state_file(), _run_folder() / f"vorab_{datetime.now():%Y%m%d_%H%M%S}")
        _pause()
        return
    _run_production()


# --------- EXEC ---------
if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        error_message = f"Unhandled error in main: {exc}"
        print(error_message)
        tb_write_log(main_log, error_message)
        tb_write_log(main_log, traceback.format_exc())
        _pause()
