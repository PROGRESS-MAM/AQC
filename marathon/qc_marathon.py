# --------- IMPORTS ---------
from pathlib import Path
import json
import sys
import tempfile
import tomllib
import traceback
from toolbox import tb_write_log
from marathon import marathon_adapter as adapter
import qc_worker


# --------- CONFIG ---------
app_name = "QC Marathon"
app_version = "2.0.0"

root_path = Path(r"\\server\share")        # like root_path in Marathon's res/config.ini
work_dir = ".marathon"                      # like work_dir in Marathon's res/config.ini
poll_seconds = 30
heartbeat_seconds = 30
stage = "QC"

qc_plan_name = "proxy_standard"
qc_plan = [
    {"tool": "stream_specs", "config": "video_1080p"},
    {"tool": "stream_specs", "config": "audio_aac_stereo"},
    {"tool": "digital_silence", "config": "keine_stille"},
]

ffe_plan_name = "ffe_tafel"
ffe_plan = [
    {"tool": "reference_frame", "config": "FFE_tafel_start_ok"},
    {"tool": "reference_frame", "config": "FFE_tafel_ende_not_ok", "must_fail": True},
]


# --------- STATIC ---------
job_status = {"ok": "ok", "rejected": "rejected", "error": "failed"}
toml_scalars = (str, int, float, bool)


def _bundle_folder() -> Path:
    # PyInstaller unpacks the exe content to sys._MEIPASS.
    return Path(sys._MEIPASS) if hasattr(sys, "_MEIPASS") else Path(__file__).resolve().parents[1]


def _run_folder() -> Path:
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


config_folder = _bundle_folder() / "marathon" / "configs"
main_log = _run_folder() / "qc_marathon.log"


# --------- FUNC PLAN ---------
def _tools(plan: list[dict]) -> list[dict]:
    return [{**entry, "config": config_folder / f"{entry['config']}.toml"} for entry in plan]


def _read_config(config: Path) -> dict:
    return tomllib.loads(config.read_text(encoding="utf-8"))


def _config_problems(config: Path, check_frames: bool) -> list[str]:
    if not config.is_file():
        return [f"Config fehlt: {config}"]
    try:
        frames = _read_config(config).get("frames", [])
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        return [f"Config {config} nicht lesbar: {exc}"]
    if not check_frames:
        return []
    frames = [frames] if isinstance(frames, str) else frames if isinstance(frames, list) else []
    return [f"Referenzbild fehlt: {config.parent / name}" for name in frames
            if isinstance(name, str) and not (config.parent / name).is_file()]


def _check_plans() -> None:
    """Validate both plans; raises ValueError on problems. Images of ffe_plan come from the job."""
    available = qc_worker.methods()
    problems = [f"unbekanntes Prüftool {entry['tool']!r}" for entry in qc_plan + ffe_plan if entry["tool"] not in available]
    problems += [problem for plan, check_frames in ((qc_plan, True), (ffe_plan, False))
                 for entry in _tools(plan) for problem in _config_problems(entry["config"], check_frames)]
    if problems:
        raise ValueError(f"Prüfplan ungültig: {'; '.join(problems)}")
    message = f"Prüfpläne: {qc_plan_name} mit {len(qc_plan)} Tools, {ffe_plan_name} mit {len(ffe_plan)} Tools."
    print(message)
    tb_write_log(main_log, message)


# --------- FUNC FFE ---------
def _toml_value(value) -> str:
    # JSON strings, numbers, booleans and flat arrays are valid TOML values.
    if isinstance(value, toml_scalars) or (isinstance(value, list) and all(isinstance(item, toml_scalars) for item in value)):
        return json.dumps(value)
    raise ValueError(f"Config-Wert {value!r} kann nicht übertragen werden.")


def _ffe_tools(image: Path, folder: Path) -> list[dict]:
    """ffe_plan with frames replaced by the job's reference image; changed configs keep their name in folder."""
    tools = []
    for entry in _tools(ffe_plan):
        data = _read_config(entry["config"])
        if "frames" in data:
            data["frames"] = [str(image)]
            entry["config"] = folder / entry["config"].name
            entry["config"].write_text("".join(f"{key} = {_toml_value(value)}\n" for key, value in data.items()),
                                       encoding="utf-8")
        tools.append(entry)
    return tools


# --------- FUNC QC ---------
def _job_problem(job: dict) -> str | None:
    if not isinstance(job.get("input"), str) or not job["input"]:
        return "input fehlt"
    if not isinstance(job.get("ffe_tafel"), bool):
        return "ffe_tafel fehlt oder ist nicht true/false"
    if job["ffe_tafel"] and (not isinstance(job.get("ffe_reference_image"), str) or not job["ffe_reference_image"]):
        return "ffe_reference_image fehlt bei ffe_tafel true"
    return None


def run_job(job: dict, output_folder: Path, log_path: Path) -> adapter.JobReport:
    if problem := _job_problem(job):
        return adapter.JobReport("failed", f"Job ungültig: {problem}", qc_plan_name)
    source = adapter.share_path(job["input"])
    if not job["ffe_tafel"]:
        result = qc_worker.process([source], _tools(qc_plan), output_folder, log_path=log_path)
        return adapter.JobReport(job_status[result.state], result.summary, qc_plan_name)
    image = adapter.share_path(job["ffe_reference_image"])
    with tempfile.TemporaryDirectory(prefix="qc_marathon_") as folder:
        tools = _tools(qc_plan) + _ffe_tools(image, Path(folder))
        result = qc_worker.process([source], tools, output_folder, log_path=log_path)
    return adapter.JobReport(job_status[result.state], result.summary, f"{qc_plan_name}+{ffe_plan_name}")


# --------- MAIN ---------
def main() -> None:
    _check_plans()
    versions = {"qc_marathon": app_version, "marathon_adapter": adapter.app_version, **qc_worker.versions()}
    settings = adapter.Settings(root_path, work_dir, stage, poll_seconds, heartbeat_seconds, main_log, versions)
    adapter.run(settings, run_job)


# --------- EXEC ---------
if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        error_message = f"Unhandled error in main: {exc}"
        print(error_message)
        tb_write_log(main_log, error_message)
        tb_write_log(main_log, traceback.format_exc())
