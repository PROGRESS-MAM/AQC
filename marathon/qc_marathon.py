# --------- IMPORTS ---------
from pathlib import Path
import sys
import traceback
from toolbox import tb_write_log
from marathon import marathon_adapter as adapter
import qc_worker


# --------- CONFIG ---------
app_name = "QC Marathon"
app_version = "1.0.0"

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
    {"test": "reference_frame", "config": "FFE_tafel_start_ok"},
    {"test": "reference_frame", "config": "FFE_tafel_ende_not_ok", "must_fail": True},
]


# --------- STATIC ---------
job_status = {"ok": "ok", "rejected": "rejected", "error": "failed"}


def _bundle_folder() -> Path:
    # PyInstaller unpacks the exe content to sys._MEIPASS.
    return Path(sys._MEIPASS) if hasattr(sys, "_MEIPASS") else Path(__file__).resolve().parents[1]


def _run_folder() -> Path:
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


config_folder = _bundle_folder() / "marathon" / "configs"
main_log = _run_folder() / "qc_marathon.log"


# --------- FUNC QC ---------
def _plan_tests() -> list[dict]:
    return [{**entry, "config": config_folder / f"{entry['config']}.toml"} for entry in qc_plan]


def _check_plan() -> None:
    available = qc_worker.methods()
    problems = [f"unbekannte Prüfmethode {entry['test']!r}" for entry in qc_plan if entry["test"] not in available]
    problems += [f"Config fehlt: {entry['config']}" for entry in _plan_tests() if not entry["config"].is_file()]
    if problems:
        raise ValueError(f"Prüfplan {qc_plan_name} ungültig: {'; '.join(problems)}")


def run_job(job: dict, output_folder: Path, log_path: Path) -> adapter.JobReport:
    if not isinstance(job.get("input"), str) or not job["input"]:
        return adapter.JobReport("failed", "Job ohne input.", qc_plan_name)
    result = qc_worker.process([adapter.share_path(job["input"])], _plan_tests(), output_folder, log_path=log_path)
    return adapter.JobReport(job_status[result.state], result.summary, qc_plan_name)


# --------- MAIN ---------
def main() -> None:
    _check_plan()
    versions = {"qc_marathon": app_version, "marathon_adapter": adapter.app_version, **qc_worker.versions()}
    adapter.run(adapter.Settings(root_path, work_dir, stage, poll_seconds, heartbeat_seconds, main_log, versions), run_job)


# --------- EXEC ---------
if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        error_message = f"Unhandled error in main: {exc}"
        print(error_message)
        tb_write_log(main_log, error_message)
        tb_write_log(main_log, traceback.format_exc())
