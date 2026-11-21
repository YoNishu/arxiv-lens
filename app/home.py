"""The home folder: where one installation keeps everything it writes.

ARXIVLENS_HOME names it (default: data under the working directory). It holds the SQLite
database, the clock file, the corpus marker, the paper texts, the insights results and the
report, and nothing in it names its own location, so a home can be copied or moved as a whole.
Every file written there goes through write_file().
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def path() -> Path:
    """Absolute path of the home. Read from the environment at every call; links are not resolved."""
    return Path(os.path.abspath(os.path.expanduser(os.environ.get("ARXIVLENS_HOME") or "data")))


def ensure() -> Path:
    """Create the home when it is missing and return its path."""
    home = path()
    if not home.is_dir():
        # Asked for only when the folder is missing; exist_ok covers another process making it first.
        os.makedirs(home, exist_ok=True)
    return home


def db_file() -> Path:
    return path() / "lens.db"


def sqlite_url() -> str:
    return "sqlite+aiosqlite:///" + db_file().as_posix()


def clock_file() -> Path:
    return path() / "clock"


def corpus_file() -> Path:
    return path() / "corpus"


def texts_dir() -> Path:
    return path() / "texts"


def insights_dir() -> Path:
    return path() / "insights"


def settings_file() -> Path:
    return path() / "insights.yaml"


def report_dir() -> Path:
    return path() / "report"


def write_file(path, data: bytes) -> None:
    """Write a file so that readers see the old content or the new one, never a part of it.

    The bytes go to <path>.tmp, which then takes the place of the file in one step. A process
    that dies in between leaves the .tmp behind; the next write of the same file overwrites it.
    """
    target = os.fspath(path)
    parent = os.path.dirname(target)
    if parent and not os.path.isdir(parent):
        # As in ensure(): only when missing, and without failing when another process was faster.
        os.makedirs(parent, exist_ok=True)
    with open(target + ".tmp", "wb") as f:
        f.write(data)
    os.replace(target + ".tmp", target)
