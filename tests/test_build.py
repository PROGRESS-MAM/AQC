import importlib
import os
import sys
from types import ModuleType

import pytest


@pytest.fixture
def build_qc_module(monkeypatch, tmp_path):
    calls = []
    pyinstaller = ModuleType("PyInstaller")
    pyinstaller.__path__ = []
    pyinstaller_main = ModuleType("PyInstaller.__main__")
    pyinstaller_main.run = lambda args: calls.append(list(args))
    pyinstaller.__main__ = pyinstaller_main
    monkeypatch.setitem(sys.modules, "PyInstaller", pyinstaller)
    monkeypatch.setitem(sys.modules, "PyInstaller.__main__", pyinstaller_main)

    module = importlib.import_module("marathon.build_qc")
    module = importlib.reload(module)
    monkeypatch.setattr(module, "build_folder", tmp_path)
    return module, calls


def test_build_qc_args(build_qc_module, tmp_path):
    build_qc, calls = build_qc_module
    qc_marathon = importlib.import_module("marathon.qc_marathon")

    build_qc.build("qc_marathon")

    args = calls[0]
    assert args[0].endswith("qc_marathon.py")
    assert args[args.index("--name") + 1] == f"qc_marathon_{qc_marathon.app_version}"
    assert "--onefile" in args
    assert "--console" in args
    add_data = [args[index + 1] for index, value in enumerate(args[:-1]) if value == "--add-data"]
    assert len(add_data) == 2
    assert add_data[0].split(os.pathsep)[-1].endswith("marathon/configs")
    assert add_data[1].split(os.pathsep)[-1].endswith("res")
    assert (tmp_path / f"qc_marathon_{qc_marathon.app_version}_version.txt").is_file()
    assert args == [
        str(build_qc.project / "marathon" / "qc_marathon.py"), "--onefile", "--console", "--noconfirm",
        "--name", f"qc_marathon_{qc_marathon.app_version}", "--paths", str(build_qc.project),
        "--add-data", f"{build_qc.project / 'marathon/configs'}{os.pathsep}marathon/configs",
        "--add-data", f"{build_qc.project / 'res'}{os.pathsep}res",
        "--version-file", str(tmp_path / f"qc_marathon_{qc_marathon.app_version}_version.txt"),
        "--distpath", str(build_qc.project / "dist"), "--workpath", str(tmp_path), "--specpath", str(tmp_path),
    ]


def test_build_qc_main(build_qc_module, monkeypatch):
    build_qc, _ = build_qc_module
    calls = []
    monkeypatch.setattr(build_qc, "build", lambda module_name: calls.append(module_name))

    build_qc.main()

    assert calls == ["qc_marathon"]


def test_build_ftp_main(build_qc_module, monkeypatch):
    build_ftp = importlib.import_module("marathon.build_ftp")
    calls = []
    monkeypatch.setattr(build_ftp, "build", lambda module_name: calls.append(module_name))

    build_ftp.main()

    assert calls == ["ftp_marathon"]
