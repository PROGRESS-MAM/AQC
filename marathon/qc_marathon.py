# --------- IMPORTS ---------
from dataclasses import dataclass
from functools import partial
from pathlib import Path
import json
import re
import sys
import tomllib
import traceback
import unicodedata
from toolbox import tb_write_log
from marathon import marathon_adapter as adapter
import qc_worker


# --------- CONFIG ---------
app_name = "QC Marathon"
app_version = "1.2.0"

root_path = Path(r"\\server\share")        # like root_path in Marathon's res/config.ini
work_dir = ".marathon"                      # like work_dir in Marathon's res/config.ini
poll_seconds = 30
heartbeat_seconds = 30
stage = "QC"

qc_plan_name = "proxy_standard"
qc_plan = [
    {"test": "stream_specs", "config": "video_1080p"},
    {"test": "stream_specs", "config": "audio_aac_stereo"},
    {"test": "digital_silence", "config": "keine_stille"},
]

ffe_plan_name = "ffe_tafel"
ffe_ids_file = "FFE-Filmerbe-DEFA-Titel_ids.json"
ffe_plan = [
    {"test": "reference_frame", "config": "FFE_tafel_start_ok"},
    {"test": "reference_frame", "config": "FFE_tafel_ende_not_ok", "must_fail": True},
]


# --------- STATIC ---------
job_status = {"ok": "ok", "rejected": "rejected", "error": "failed"}
defa_id_format = re.compile(r"DEFA\d{5}(?:_[A-Z])?")
defa_id_in_name = re.compile(r"(?<![A-Za-z0-9])(DEFA\d{5}(?:_[A-Z])?)(?![A-Za-z0-9])")
ffe_list_format = '{"titel": [{"file": "<Dateiname>"} | {"title": "...", "defa_id": "DEFA12345"}, ...]}'
shown_problems = 5


# --------- TYPES ---------
@dataclass(frozen=True)
class FfeList:
    """FFE titles: exact file names (NFC, casefold) and DEFA ids that may appear anywhere in a file name."""
    files: frozenset[str]
    defa_ids: frozenset[str]

    def matches(self, path: Path) -> bool:
        return _file_key(path.name) in self.files or not self.defa_ids.isdisjoint(_defa_ids_in(path.name))


def _bundle_folder() -> Path:
    # PyInstaller unpacks the exe content to sys._MEIPASS.
    return Path(sys._MEIPASS) if hasattr(sys, "_MEIPASS") else Path(__file__).resolve().parents[1]


def _run_folder() -> Path:
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


config_folder = _bundle_folder() / "marathon" / "configs"
main_log = _run_folder() / "qc_marathon.log"


# --------- FUNC PLAN ---------
def _tests(plan: list[dict]) -> list[dict]:
    return [{**entry, "config": config_folder / f"{entry['config']}.toml"} for entry in plan]


def _config_problems(config: Path) -> list[str]:
    if not config.is_file():
        return [f"Config fehlt: {config}"]
    try:
        frames = tomllib.loads(config.read_text(encoding="utf-8")).get("frames", [])
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        return [f"Config {config} nicht lesbar: {exc}"]
    frames = [frames] if isinstance(frames, str) else frames if isinstance(frames, list) else []
    return [f"Referenzbild fehlt: {config.parent / name}" for name in frames
            if isinstance(name, str) and not (config.parent / name).is_file()]


# --------- FUNC FFE LIST ---------
def _file_key(name: str) -> str:
    return unicodedata.normalize("NFC", name).strip().casefold()


def _defa_ids_in(name: str) -> set[str]:
    return set(defa_id_in_name.findall(unicodedata.normalize("NFC", name)))


def _valid_file(value) -> bool:
    return isinstance(value, str) and bool(value.strip()) and not any(char in value for char in "/\\")


def _valid_defa_id(value) -> bool:
    return isinstance(value, str) and defa_id_format.fullmatch(value.strip()) is not None


def _load_ffe_list() -> FfeList:
    path = config_folder / ffe_ids_file
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))["titel"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"ID-Liste {path} nicht lesbar ({type(exc).__name__}: {exc}). Erwartet: {ffe_list_format}.") from exc
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"ID-Liste {path}: 'titel' ist keine nicht leere Liste. Erwartet: {ffe_list_format}.")
    files, defa_ids, problems = set(), set(), []
    for number, entry in enumerate(entries, 1):
        fields = entry if isinstance(entry, dict) else {}
        file_name, defa_id = fields.get("file"), fields.get("defa_id")
        file_ok, defa_ok = _valid_file(file_name), _valid_defa_id(defa_id)
        if (file_name is not None and not file_ok) or (defa_id is not None and not defa_ok) or not (file_ok or defa_ok):
            problems.append(f"Eintrag {number}: {entry!r}")
            continue
        if file_ok:
            files.add(_file_key(file_name))
        if defa_ok:
            defa_ids.add(defa_id.strip())
    if problems:
        more = f" (+{len(problems) - shown_problems} weitere)" if len(problems) > shown_problems else ""
        raise ValueError(f"ID-Liste {path}: {len(problems)} ungültige Einträge: "
                         f"{'; '.join(problems[:shown_problems])}{more}. Erwartet: {ffe_list_format}.")
    return FfeList(frozenset(files), frozenset(defa_ids))


def _check_plans() -> FfeList:
    """Validate both plans and return the FFE list; raises ValueError on problems."""
    available, plans = qc_worker.methods(), qc_plan + ffe_plan
    problems = [f"unbekannte Prüfmethode {entry['test']!r}" for entry in plans if entry["test"] not in available]
    problems += [problem for entry in _tests(plans) for problem in _config_problems(entry["config"])]
    if problems:
        raise ValueError(f"Prüfplan ungültig: {'; '.join(problems)}")
    ffe_list = _load_ffe_list()
    message = (f"ID-Liste {ffe_ids_file}: {len(ffe_list.files)} Dateinamen und "
               f"{len(ffe_list.defa_ids)} DEFA-IDs mit {ffe_plan_name}.")
    print(message)
    tb_write_log(main_log, message)
    return ffe_list


# --------- FUNC QC ---------
def run_job(job: dict, output_folder: Path, log_path: Path, ffe_list: FfeList) -> adapter.JobReport:
    if not isinstance(job.get("input"), str) or not job["input"]:
        return adapter.JobReport("failed", "Job ohne input.", qc_plan_name)
    source = adapter.share_path(job["input"])
    is_ffe = ffe_list.matches(source)
    plan_name = f"{qc_plan_name}+{ffe_plan_name}" if is_ffe else qc_plan_name
    tests = _tests(qc_plan + ffe_plan if is_ffe else qc_plan)
    result = qc_worker.process([source], tests, output_folder, log_path=log_path)
    return adapter.JobReport(job_status[result.state], result.summary, plan_name)


# --------- MAIN ---------
def main() -> None:
    ffe_list = _check_plans()
    versions = {"qc_marathon": app_version, "marathon_adapter": adapter.app_version, **qc_worker.versions()}
    settings = adapter.Settings(root_path, work_dir, stage, poll_seconds, heartbeat_seconds, main_log, versions)
    adapter.run(settings, partial(run_job, ffe_list=ffe_list))


# --------- EXEC ---------
if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        error_message = f"Unhandled error in main: {exc}"
        print(error_message)
        tb_write_log(main_log, error_message)
        tb_write_log(main_log, traceback.format_exc())
