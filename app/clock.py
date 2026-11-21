"""The one source of time.

now() answers with the instant in the home's clock file when there is one, else with
ARXIVLENS_CLOCK, else with the system time. A clock file holds simulated time: it stands still
until it is set or advanced, and every process that uses the home, or a copy of it, reads the
same instant. Leases, windows and stored stamps all come from here, so a test never has to wait.
"""
import os
import re
from datetime import date, datetime, timedelta, timezone

from app import home

_INSTANT = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})(?:T([0-9]{2}):([0-9]{2})(?::([0-9]{2}))?Z?)?")
_DURATION = re.compile(r"([0-9]+)([smhd])")
_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_EXPECTED = "expected an instant, YYYY-MM-DDTHH:MM:SSZ"


def now() -> datetime:
    """The current instant, aware and in UTC."""
    stored = _stored()
    if stored is not None:
        return stored
    value = os.environ.get("ARXIVLENS_CLOCK")
    if value:
        try:
            return parse_instant(value)
        except ValueError:
            raise ValueError("ARXIVLENS_CLOCK: " + _EXPECTED) from None
    return datetime.now(timezone.utc)


def today() -> date:
    return now().date()


def source() -> str:
    """Where now() takes its answer from: file, env or system."""
    if home.clock_file().exists():
        return "file"
    if os.environ.get("ARXIVLENS_CLOCK"):
        return "env"
    return "system"


def set_to(instant: datetime) -> None:
    """Write the clock file. Simulated time has whole seconds."""
    home.write_file(home.clock_file(), (format_instant(instant) + "\n").encode("ascii"))


def advance(seconds: int) -> datetime:
    """Move the clock file forward and return the new instant."""
    stored = _stored()
    if stored is None:
        # Advancing from the system time would write a clock file and so stop time without being asked.
        raise ValueError("clock: not set")
    moved = stored + timedelta(seconds=seconds)
    set_to(moved)
    return moved


def parse_instant(text: str) -> datetime:
    """2026-09-03, 2026-09-03T08:00 or 2026-09-03T08:00:00, the last two with or without Z."""
    match = _INSTANT.fullmatch(text)
    if match is None:
        raise ValueError("not an instant: %s" % text)
    year, month, day, hour, minute, second = (int(part or 0) for part in match.groups())
    try:
        return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    except ValueError:
        raise ValueError("not an instant: %s" % text) from None


def format_instant(dt: datetime) -> str:
    """YYYY-MM-DDTHH:MM:SSZ; fractions of a second are dropped."""
    # A time without a zone is taken as UTC: that is how SQLite hands stored times back.
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return "%04d-%02d-%02dT%02d:%02d:%02dZ" % (dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)


def parse_duration(text: str) -> int:
    """Seconds of 90s, 15m, 2h or 3d."""
    match = _DURATION.fullmatch(text)
    if match is None or int(match.group(1)) == 0:
        raise ValueError("not a duration: %s" % text)
    return int(match.group(1)) * _SECONDS[match.group(2)]


def _stored() -> datetime | None:
    """The instant in the clock file, or None when the home has none."""
    path = home.clock_file()
    try:
        with open(path, "rb") as f:
            text = f.read().decode("ascii", "replace").strip()
    except FileNotFoundError:
        return None
    try:
        return parse_instant(text)
    except ValueError:
        # Never answered from another source instead: a broken clock would go unnoticed.
        raise ValueError("%s: %s" % (path, _EXPECTED)) from None
