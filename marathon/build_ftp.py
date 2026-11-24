# --------- IMPORTS ---------
from pathlib import Path
import sys

# Started as a script, the project root is not on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from marathon.build_qc import build


# --------- CONFIG ---------
app_name = "FTP Marathon Build"
app_version = "1.0.0"


# --------- MAIN ---------
def main() -> None:
    build("ftp_marathon")


# --------- EXEC ---------
if __name__ == "__main__":
    main()
