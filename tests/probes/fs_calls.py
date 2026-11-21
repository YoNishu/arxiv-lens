"""Performs the file operations named on its command line, each in its own prepared folder."""
import atexit
import gzip
import io
import os
import shutil
import sys
from pathlib import Path

import app  # noqa: F401  importing the package installs the failure switch when one is set


def _os_open(name: str, flags: int):
    def call(d: Path) -> None:
        os.close(os.open(d / name, flags))
    return call


def _with_read_descriptor(action):
    def call(d: Path) -> None:
        fd = os.open(d / "f", os.O_RDONLY)
        try:
            action(fd)
        finally:
            os.close(fd)
    return call


def _close(make):
    return lambda d: make(d).close()


OPERATIONS = {
    "os.replace": lambda d: os.replace(d / "f", d / "g"),
    "os.rename": lambda d: os.rename(d / "f", d / "g"),
    "os.unlink": lambda d: os.unlink(d / "f"),
    "os.remove": lambda d: os.remove(d / "f"),
    "os.rmdir": lambda d: os.rmdir(d / "sub"),
    "os.mkdir": lambda d: os.mkdir(d / "new"),
    "os.symlink": lambda d: os.symlink(d / "f", d / "link"),
    "os.link": lambda d: os.link(d / "f", d / "hard"),
    "os.truncate": lambda d: os.truncate(d / "f", 0),
    # ftruncate on a read-only descriptor is refused by the system, which is fine: it is the
    # call that counts, not whether it succeeds
    "os.ftruncate": _with_read_descriptor(lambda fd: os.ftruncate(fd, 0)),
    "os.open-wronly": _os_open("f", os.O_WRONLY),
    "os.open-rdwr": _os_open("f", os.O_RDWR),
    "os.open-creat": _os_open("new", os.O_RDONLY | os.O_CREAT),
    "os.open-trunc": _os_open("f", os.O_RDONLY | os.O_TRUNC),
    "os.open-append": _os_open("f", os.O_RDONLY | os.O_APPEND),
    "open-w": _close(lambda d: open(d / "f", "w")),
    "open-wb": _close(lambda d: open(d / "f", "wb")),
    "open-a": _close(lambda d: open(d / "f", "a")),
    "open-x": _close(lambda d: open(d / "new", "x")),
    "open-r+": _close(lambda d: open(d / "f", "r+")),
    "io.open-w": _close(lambda d: io.open(d / "f", "w")),
    "gzip.open-wb": _close(lambda d: gzip.open(d / "f.gz", "wb")),
    "pathlib.write_text": lambda d: (d / "f").write_text("y"),
    "pathlib.write_bytes": lambda d: (d / "f").write_bytes(b"y"),
    "pathlib.open-a": _close(lambda d: (d / "f").open("a")),
    "pathlib.mkdir": lambda d: (d / "new").mkdir(),
    "pathlib.unlink": lambda d: (d / "f").unlink(),
    "pathlib.rmdir": lambda d: (d / "sub").rmdir(),
    "pathlib.rename": lambda d: (d / "f").rename(d / "g"),
    "pathlib.replace": lambda d: (d / "f").replace(d / "g"),
    "pathlib.symlink_to": lambda d: (d / "link").symlink_to(d / "f"),
    "os.makedirs-two-levels": lambda d: os.makedirs(d / "a" / "b"),
    "os.makedirs-existing": lambda d: os.makedirs(d / "existing", exist_ok=True),
    "shutil.rmtree": lambda d: shutil.rmtree(d / "tree"),
    "open-r": lambda d: (d / "f").read_text(),
    "open-rb": _close(lambda d: open(d / "f", "rb")),
    "os.open-rdonly": _os_open("f", os.O_RDONLY),
    "open-descriptor": _with_read_descriptor(lambda fd: open(fd, "w", closefd=False).close()),
    "open-opener": _close(lambda d: open(d / "f", "w", opener=lambda path, flags: os.open(path, os.O_RDONLY))),
    "os.utime": lambda d: os.utime(d / "f"),
    "os.chmod": lambda d: os.chmod(d / "f", 0o644),
    "os.fsync": _with_read_descriptor(os.fsync),
    "os.stat": lambda d: os.stat(d / "f"),
    "os.listdir": lambda d: os.listdir(d),
}


def main(argv: list[str]) -> int:
    root = Path(argv[0])
    out = io.TextIOWrapper(io.BufferedWriter(io.FileIO(1, "w", closefd=False)), encoding="utf-8")
    # Kept in the buffer on purpose: a process ended by the switch must not get to print it.
    out.write("started\n")
    atexit.register(sys.stderr.write, "exit handlers ran\n")
    for name in argv[1:]:
        try:
            OPERATIONS[name](root / name)
        except OSError:
            pass
    out.write("done\n")
    out.write(f"faults={'yes' if 'app.faults' in sys.modules else 'no'}\n")
    out.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
