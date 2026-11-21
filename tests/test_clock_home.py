"""The clock and the home folder: where time comes from, and how a home is made and written."""
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

INSTANT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
PAPER = "2401.90001"

ACCEPTED = [
    ("2026-08-10", "2026-08-10T00:00:00Z"),
    ("2026-08-10T08:15", "2026-08-10T08:15:00Z"),
    ("2026-08-10T08:15Z", "2026-08-10T08:15:00Z"),
    ("2026-08-10T08:15:30", "2026-08-10T08:15:30Z"),
    ("2026-08-10T08:15:30Z", "2026-08-10T08:15:30Z"),
    ("2024-02-29T23:59:59Z", "2024-02-29T23:59:59Z"),
]
REFUSED = [
    "", "now", "2026-8-10", "20260810", "2026-08-10Z", "2026-08-10T08", "2026-08-10T08Z",
    "2026-08-10 08:15", "2026-08-10T08:15:30.250Z", "2026-08-10T08:15:30+00:00",
    "2026-02-30", "2025-02-29", "2026-13-01", "2026-08-10T24:00", "2026-08-10T08:60",
]
DURATIONS = [("90s", 90), ("15m", 900), ("2h", 7200), ("3d", 259200)]
REFUSED_DURATIONS = ["", "0s", "-5m", "90", "1.5h", "2w", "h", "2 h", "2H"]
BASE = "2026-08-10T08:00:00Z"


def _show(home, env=None) -> tuple[str, str]:
    shown = home.run("clock", "show", env=env)
    assert len(shown.lines) == 1, shown
    instant, source = shown.lines[0].split(" ")
    return instant, source


def _later(instant: str, seconds: int) -> str:
    moment = datetime.strptime(instant, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (moment + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _refused(result) -> bool:
    return result.status == 2 and result.stdout == "" and result.stderr.strip() != ""


def test_clock_file_wins_over_the_variable_and_the_variable_over_system_time(home):
    instant, source = _show(home)
    assert source == "system" and INSTANT.match(instant), (instant, source)
    assert _show(home, {"ARXIVLENS_CLOCK": "2026-08-10T08:00:00Z"}) == ("2026-08-10T08:00:00Z", "env")
    assert _show(home, {"ARXIVLENS_CLOCK": "2026-08-10"}) == ("2026-08-10T00:00:00Z", "env")

    assert home.run("clock", "set", "2026-09-01T10:30Z").lines == ["2026-09-01T10:30:00Z"]
    assert (home.path / "clock").read_bytes() == b"2026-09-01T10:30:00Z\n"
    assert _show(home) == ("2026-09-01T10:30:00Z", "file")
    assert _show(home, {"ARXIVLENS_CLOCK": "2026-08-10T08:00:00Z"}) == ("2026-09-01T10:30:00Z", "file")


def test_instants_and_durations_are_accepted_or_refused(home):
    commands = [["clock", "set", text] for text, _ in ACCEPTED]
    commands += [["clock", "set", text] for text in REFUSED]
    for text, _ in DURATIONS:
        commands += [["clock", "set", BASE], ["clock", "advance", text]]
    commands += [["clock", "set", BASE]] + [["clock", "advance", text] for text in REFUSED_DURATIONS]
    commands += [["clock", "show"]]
    results = iter(home.batch(*commands))

    for text, expected in ACCEPTED:
        result = next(results)
        assert (result.status, result.lines) == (0, [expected]), (text, result)
    for text in REFUSED:
        result = next(results)
        assert _refused(result), (text, result)
    for text, seconds in DURATIONS:
        next(results)
        result = next(results)
        assert (result.status, result.lines) == (0, [_later(BASE, seconds)]), (text, result)
    next(results)
    for text in REFUSED_DURATIONS:
        result = next(results)
        assert _refused(result), (text, result)
    # a refused duration leaves the clock where it was
    assert next(results).lines == [f"{BASE} file"]


def test_advance_needs_a_clock_file_and_a_malformed_clock_is_an_error(home):
    result = home.run("clock", "advance", "2h", check=False)
    assert _refused(result) and "not set" in result.stderr, result
    # the variable does not stand in for the file: advancing never freezes another source
    result = home.run("clock", "advance", "2h", env={"ARXIVLENS_CLOCK": BASE}, check=False)
    assert _refused(result) and "not set" in result.stderr, result
    assert not (home.path / "clock").exists()

    result = home.run("clock", "show", env={"ARXIVLENS_CLOCK": "next tuesday"}, check=False)
    assert _refused(result) and "ARXIVLENS_CLOCK" in result.stderr, result

    home.path.mkdir(parents=True, exist_ok=True)
    (home.path / "clock").write_bytes(b"2026-08-10 08:00\n")
    for args, env in ((["clock", "show"], None), (["clock", "advance", "1h"], None),
                      (["clock", "show"], {"ARXIVLENS_CLOCK": BASE})):
        result = home.run(*args, env=env, check=False)
        assert _refused(result) and "clock" in result.stderr, (args, env, result)
    assert (home.path / "clock").read_bytes() == b"2026-08-10 08:00\n"


def test_two_processes_and_a_copied_home_share_simulated_time(home, standin, tmp_path):
    world = standin(scenario={"feeds": {"cs.LG": [[PAPER, 1, "new"]]}})
    home.join(world).seeds("cs.LG")
    assert home.run("clock", "set", "2026-08-10T08:00Z").lines == ["2026-08-10T08:00:00Z"]
    home.run("cycle")
    # the cycle ran in a process of its own and stamped the time another process had set
    assert "stored      2026-08-10T08:00:00Z" in home.run("paper", "show", PAPER).lines
    assert home.run("clock", "advance", "2h").lines == ["2026-08-10T10:00:00Z"]
    assert _show(home) == ("2026-08-10T10:00:00Z", "file")

    # every command has given its database back, so a plain copy is a complete home
    assert not (home.path / "lens.db-wal").exists()
    twin = home.copy(tmp_path / "twin")
    for args in (["db", "info"], ["clock", "show"], ["paper", "show", PAPER]):
        mine, theirs = home.run(*args), twin.run(*args)
        assert mine.stdout == theirs.stdout, (args, mine, theirs)

    # from here on the two homes keep their own time and their own record
    assert twin.run("clock", "advance", "1d").lines == ["2026-08-11T10:00:00Z"]
    assert _show(home) == ("2026-08-10T10:00:00Z", "file")
    assert re.match(r"^run \d+: new=0 updated=0 skipped=0 processed=0 failed=0$", twin.run("cycle").lines[0])
    assert home.run("db", "info").stdout != twin.run("db", "info").stdout


def test_home_is_created_on_demand_and_files_are_written_whole(home, make_home, tmp_path, monkeypatch):
    from app import home as home_module

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ARXIVLENS_HOME", raising=False)
    assert Path(home_module.path()) == tmp_path / "data"
    monkeypatch.setenv("ARXIVLENS_HOME", "")
    assert Path(home_module.path()) == tmp_path / "data"
    monkeypatch.setenv("ARXIVLENS_HOME", "elsewhere")
    assert Path(home_module.path()) == tmp_path / "elsewhere"
    assert not (tmp_path / "data").exists() and not (tmp_path / "elsewhere").exists()

    assert not home.path.exists()
    assert home.run("clock", "set", "2026-08-10T08:00Z").lines == ["2026-08-10T08:00:00Z"]
    names = sorted(p.name for p in home.path.iterdir())
    assert "clock" in names and not [n for n in names if n.endswith(".tmp")], names

    other = make_home("h2")
    other.run("db", "info")
    assert (other.path / "lens.db").is_file()

    # with the folder present a write is two counted calls: the temporary file, then the switch
    killed = home.run("clock", "set", "2026-08-11T08:00Z", env={"ARXIVLENS_KILL_AT": "fs:2"}, check=False)
    assert (killed.status, killed.stdout) == (137, ""), killed
    assert _show(home) == ("2026-08-10T08:00:00Z", "file")
    done = home.run("clock", "set", "2026-08-11T08:00Z", env={"ARXIVLENS_KILL_AT": "fs:3"})
    assert done.lines == ["2026-08-11T08:00:00Z"]
    assert (home.path / "clock").read_bytes() == b"2026-08-11T08:00:00Z\n"
    assert not [p.name for p in home.path.iterdir() if p.name.endswith(".tmp")]
