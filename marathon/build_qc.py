# --------- IMPORTS ---------
from pathlib import Path
import os
import sys
import PyInstaller.__main__


# --------- CONFIG ---------
app_name = "QC Marathon Build"
app_version = "1.0.0"

project = Path(__file__).resolve().parents[1]
build_folder = project / "build"


# --------- FUNC ---------
def _version_file(name: str, version: str, exe_name: str) -> Path:
    numbers = (tuple(int(part) for part in version.split(".")) + (0, 0, 0, 0))[:4]
    text = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={numbers}, prodvers={numbers}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040704B0', [
          StringStruct('FileDescription', '{name}'), StringStruct('FileVersion', '{version}'),
          StringStruct('ProductName', '{name}'), StringStruct('ProductVersion', '{version}'),
          StringStruct('OriginalFilename', '{exe_name}.exe')])]),
        VarFileInfo([VarStruct('Translation', [1031, 1200])])])
"""
    path = build_folder / f"{exe_name}_version.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _data(source: str, target: str) -> str:
    return f"{project / source}{os.pathsep}{target}"


# --------- MAIN ---------
def main() -> None:
    sys.path.insert(0, str(project))
    from marathon import qc_marathon
    exe_name = f"qc_marathon_{qc_marathon.app_version}"
    PyInstaller.__main__.run([
        str(project / "marathon" / "qc_marathon.py"), "--onefile", "--console", "--noconfirm",
        "--name", exe_name, "--paths", str(project),
        "--add-data", _data("marathon/configs", "marathon/configs"), "--add-data", _data("res", "res"),
        "--version-file", str(_version_file(qc_marathon.app_name, qc_marathon.app_version, exe_name)),
        "--distpath", str(project / "dist"), "--workpath", str(build_folder), "--specpath", str(build_folder),
    ])


# --------- EXEC ---------
if __name__ == "__main__":
    main()
