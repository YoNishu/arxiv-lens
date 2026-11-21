"""Switches that end or slow the process at a chosen point, for tests of what happens in the middle.

ARXIVLENS_KILL_AT=fs:N ends the process right before its N-th change to the file system,
commit:N right before its N-th database commit, in both cases the way kill -9 would: nothing is
flushed and no clean-up runs. ARXIVLENS_SLOW=SECONDS makes every analysis that is answered
inside the process wait first. Without these variables nothing here is active.
"""
import asyncio
import builtins
import functools
import io
import math
import os
import pathlib
import re

_KILL_AT = re.compile(r"(fs|commit):([0-9]+)")
_OS_CALLS = ("replace", "rename", "unlink", "remove", "rmdir", "mkdir", "symlink", "link", "truncate", "ftruncate")
_WRITING_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND


def install() -> None:
    """Arm the switch that ARXIVLENS_KILL_AT names."""
    match = _KILL_AT.fullmatch(os.environ.get("ARXIVLENS_KILL_AT", ""))
    if match is None or int(match.group(2)) < 1:
        _refuse("ARXIVLENS_KILL_AT: expected fs:N or commit:N")
    tick = _countdown(int(match.group(2)))
    if match.group(1) == "fs":
        _count_file_system_calls(tick)
    else:
        _count_commits(tick)


async def slow() -> None:
    """Wait ARXIVLENS_SLOW seconds; return at once when it is not set."""
    value = os.environ.get("ARXIVLENS_SLOW")
    if not value:
        return
    try:
        seconds = float(value)
    except ValueError:
        seconds = -1.0
    if not (seconds >= 0 and math.isfinite(seconds)):
        # Ended here and not raised: an error raised inside an analysis is recorded as that
        # paper's failure, and the mistyped variable would go unnoticed.
        _refuse("ARXIVLENS_SLOW: expected a number of seconds, 0 or more")
    await asyncio.sleep(seconds)


def _refuse(line: str) -> None:
    os.write(2, (line + "\n").encode("ascii"))
    os._exit(2)


def _countdown(limit: int):
    """A function that ends the process when it is called for the limit-th time."""
    calls = 0

    def tick() -> None:
        nonlocal calls
        calls += 1
        if calls == limit:
            os._exit(137)

    return tick


def _count_file_system_calls(tick) -> None:
    """Count every call that changes the file system, whether or not it would succeed.

    The functions are replaced as attributes of os, builtins and io, which is where os.makedirs,
    shutil, gzip, tempfile and pathlib look them up. What SQLite does to its own files does not
    pass through Python and is not seen.
    """
    for name in _OS_CALLS:
        setattr(os, name, _counted(getattr(os, name), tick))

    os_open = os.open

    @functools.wraps(os_open)
    def counted_os_open(path, flags, *args, **kwargs):
        if flags & _WRITING_FLAGS:
            tick()
        return os_open(path, flags, *args, **kwargs)

    os.open = counted_os_open

    plain_open = builtins.open

    @functools.wraps(plain_open)
    def counted_open(file, mode="r", *args, **kwargs):
        # A file descriptor is already open, and with an opener the opener's own os.open is the
        # call that reaches the file system; counting those here would count one change twice.
        opener = kwargs.get("opener", args[5] if len(args) > 5 else None)
        if not isinstance(file, int) and opener is None and isinstance(mode, str) and set(mode) & set("wax+"):
            tick()
        return plain_open(file, mode, *args, **kwargs)

    builtins.open = counted_open
    io.open = counted_open

    accessor = getattr(pathlib, "_NormalAccessor", None)
    if accessor is not None:
        # Python 3.10: pathlib took its own references to these functions when it was imported.
        # As staticmethod, because a Python function kept on a class is handed the instance first.
        accessor.open = staticmethod(counted_open)
        for name in ("mkdir", "unlink", "rmdir", "rename", "replace", "symlink", "link"):
            setattr(accessor, name, staticmethod(getattr(os, name)))


def _counted(call, tick):
    @functools.wraps(call)
    def counted(*args, **kwargs):
        tick()
        return call(*args, **kwargs)

    return counted


def _count_commits(tick) -> None:
    """Count the commits of every engine in the process.

    The event fires before the driver commits, so the N-th commit never reaches the database.
    """
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    @event.listens_for(Engine, "commit")
    def before_commit(connection):
        tick()
