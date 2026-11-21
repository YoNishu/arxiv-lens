"""Playing a corpus into a home: day ranges, the clock, both kinds of sources, and what a played home holds."""

import gzip
import hashlib
import json
import os
import re
from datetime import date, timedelta

from conftest import SMALL, run_all

CYCLE = r"run (\d+): new=(\d+) updated=(\d+) skipped=(\d+) processed=(\d+) failed=(\d+)"
LINE = re.compile(r"(\d{4}-\d\d-\d\d) " + CYCLE)
IDLE = re.compile(CYCLE.replace(r"=(\d+)", "=0"))


def manifest() -> dict:
    return json.loads((SMALL / "corpus.json").read_text(encoding="utf-8"))


def day_range(first: str, last: str) -> list[str]:
    day, end = date.fromisoformat(first), date.fromisoformat(last)
    days = []
    while day <= end:
        days.append(day.isoformat())
        day += timedelta(days=1)
    return days


def shifted(day, days: int) -> str:
    return (date.fromisoformat(str(day)) + timedelta(days=days)).isoformat()


def play(home, *args, env=None, check=True):
    return home.run("play", "--corpus", str(SMALL), *args, env=env, check=check)


def tables(home, leave_out=()) -> dict[str, list[str]]:
    """Every table of the home's database with its rows, sorted, so that equal databases compare equal."""
    names = [row[0] for row in home.sql(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {name: sorted(repr(row) for row in home.sql(f'SELECT * FROM "{name}"'))
            for name in names if name not in leave_out}


def differing_tables(a: dict, b: dict) -> list[str]:
    return sorted(name for name in set(a) | set(b) if a.get(name) != b.get(name))


def texts(home) -> dict[str, bytes]:
    folder = home.path / "texts"
    if not folder.is_dir():
        return {}
    return {path.name: gzip.decompress(path.read_bytes()) for path in sorted(folder.iterdir())}


def home_files(home) -> dict[str, bytes]:
    """Every file under the home but the database and the clock, by relative path."""
    skip = {"lens.db", "lens.db-wal", "lens.db-shm", "clock"}
    found = {}
    for path in sorted(home.path.rglob("*")):
        rel = path.relative_to(home.path).as_posix()
        if path.is_file() and rel not in skip:
            found[rel] = path.read_bytes()
    return found


def test_play_refuses_days_outside_the_corpus(home, tmp_path):
    corpus = manifest()
    start, end = corpus["start"], corpus["end"]
    before, after = shifted(start, -1), shifted(end, 1)
    outside = f"({start} to {end})"
    empty = tmp_path / "no-corpus"
    empty.mkdir()
    cases = [
        (["--day", before], f"error: {before} is outside the corpus {outside}"),
        (["--until", after], f"error: {after} is outside the corpus {outside}"),
        (["--from", before, "--until", start], f"error: {before} is outside the corpus {outside}"),
        (["--from", shifted(start, 2), "--until", shifted(start, 1)], "error: nothing to play"),
    ]
    commands = [["play", "--corpus", str(SMALL), "--fast", *args] for args, _ in cases]
    commands.append(["play", "--corpus", str(empty), "--fast", "--day", start])
    commands.append(["play", "--corpus", str(SMALL), "--fast", "--day", "2026-02-30"])
    results = home.batch(*commands)

    for (args, message), result in zip(cases, results):
        assert result.status == 2, args
        assert result.stdout == "", args
        assert result.stderr.splitlines() == [message], args
    missing = results[len(cases)]
    assert missing.status == 2 and missing.stdout == ""
    assert missing.stderr.splitlines() == [f"error: corpus not found: {empty}"]
    malformed = results[len(cases) + 1]
    assert malformed.status == 2 and malformed.stdout == ""


def test_each_day_sets_the_clock_and_runs_one_cycle(home):
    days = day_range("2026-08-10", "2026-08-16")
    result = play(home, "--fast", "--until", days[-1])

    matches = [LINE.fullmatch(line) for line in result.lines]
    assert all(matches), result.lines
    assert [m.group(1) for m in matches] == days
    ids = [int(m.group(2)) for m in matches]
    assert ids == sorted(ids) and len(set(ids)) == len(days)
    for m in matches:
        weekend = date.fromisoformat(m.group(1)).weekday() >= 5
        assert (int(m.group(3)) == 0) == weekend, m.group(0)

    runs = home.sql("SELECT id, started_at, finished_at, error_message FROM runs ORDER BY id")
    assert [row[0] for row in runs] == ids
    for (run_id, started, finished, error), day in zip(runs, days):
        assert str(started)[:10] == day and str(started)[11:19] == "08:00:00", (run_id, started)
        assert finished is not None and error is None, run_id
    feeds = home.sql("SELECT run_id, COUNT(*) FROM run_feeds GROUP BY run_id ORDER BY run_id")
    assert feeds == [(run_id, len(manifest()["feeds"])) for run_id in ids]

    assert home.run("clock", "show").lines == [f"{days[-1]}T08:00:00Z file"]
    marker = (home.path / "corpus").read_bytes()
    value = {"fast": True, "path": os.path.abspath(str(SMALL))}
    assert marker == json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"


def test_http_and_fast_play_give_the_same_database(make_home, standin):
    last = day_range("2026-08-10", "2026-08-25")[-1]
    fast = make_home("fast")
    http = make_home("http")
    world = standin(corpus=SMALL, home=http.path)
    http = http.join(world)

    played = run_all([lambda: play(fast, "--fast", "--until", last), lambda: play(http, "--until", last)])
    assert played[0].lines == played[1].lines

    a, b = tables(fast), tables(http)
    assert sorted(a) == sorted(b)
    assert differing_tables(a, b) == []
    assert a["papers"], "nothing was stored"
    assert texts(fast) == texts(http)
    assert texts(fast), "no text was stored"
    assert world.count("pdf:") > 0 and world.count("llm:") > 0


def test_grouping_of_days_does_not_matter(played, tmp_path):
    start = played.home_at["B1"]
    days = [shifted(played.days["B1"], n) for n in range(1, 11)]
    weekend = [day for day in days if date.fromisoformat(day).weekday() >= 5]
    assert len(weekend) == 2
    whole = start.copy(tmp_path / "whole")
    daily = start.copy(tmp_path / "daily")
    gaps = start.copy(tmp_path / "gaps")

    play(whole, "--fast", "--from", days[0], "--until", days[-1])
    for result in daily.batch(*[["play", "--corpus", str(SMALL), "--fast", "--day", day] for day in days]):
        assert result.status == 0, result.stderr
    first_weekend = days.index(weekend[0])
    play(gaps, "--fast", "--from", days[0], "--until", days[first_weekend - 1])
    play(gaps, "--fast", "--from", days[first_weekend + 2], "--until", days[-1])

    assert differing_tables(tables(whole), tables(daily)) == []
    assert differing_tables(tables(whole, leave_out=("runs", "run_feeds")),
                            tables(gaps, leave_out=("runs", "run_feeds"))) == []
    assert texts(whole) == texts(daily) == texts(gaps)
    for home in (whole, daily, gaps):
        assert home.run("clock", "show").lines == [f"{days[-1]}T08:00:00Z file"]


def test_then_runs_after_every_day_and_its_failure_stops_the_play(make_home):
    days = day_range("2026-08-14", "2026-08-16")
    home = make_home("then")
    result = play(home, "--fast", "--from", days[0], "--until", days[-1], "--then", "clock show")
    assert len(result.lines) == 2 * len(days)
    for n, day in enumerate(days):
        match = LINE.fullmatch(result.lines[2 * n])
        assert match and match.group(1) == day, result.lines
        assert result.lines[2 * n + 1] == f"{day}T08:00:00Z file"

    stopped = make_home("stopped")
    failed = play(stopped, "--fast", "--from", days[1], "--until", days[-1], "--then", "paper show 2608.99999",
                  check=False)
    assert failed.status == 1
    assert len(failed.lines) == 1 and LINE.fullmatch(failed.lines[0]).group(1) == days[1]
    assert "error: no such paper: 2608.99999" in failed.stderr.splitlines()
    assert stopped.sql("SELECT COUNT(*) FROM runs") == [(1,)]
    assert stopped.run("clock", "show").lines == [f"{days[1]}T08:00:00Z file"]


def test_a_cycle_after_a_fast_play_is_idle_and_needs_no_stand_in(played, tmp_path):
    home = played.home_at["B1"].copy(tmp_path / "b1")
    weekend = shifted(played.days["B1"], 5)
    assert date.fromisoformat(weekend).weekday() == 5
    files = home_files(home)
    results = home.batch(["db", "info"], ["cycle"], ["db", "info"],
                         ["clock", "set", f"{weekend}T08:00:00Z"], ["cycle"], ["db", "info"])
    for result in results:
        assert result.status == 0, result.stderr
    infos = [dict(line.split("=", 1) for line in results[i].lines) for i in (0, 2, 5)]
    feeds = len(manifest()["feeds"])
    for result in (results[1], results[4]):
        assert len(result.lines) == 1 and IDLE.fullmatch(result.lines[0]), result.lines
    for earlier, later in zip(infos, infos[1:]):
        changed = sorted(name for name in earlier if earlier[name] != later.get(name))
        assert changed == ["run_feeds", "runs"]
        assert int(later["runs"]) == int(earlier["runs"]) + 1
        assert int(later["run_feeds"]) == int(earlier["run_feeds"]) + feeds
    assert home_files(home) == files


def test_a_played_corpus_keeps_the_invariants_of_the_schema(played):
    from app.ids import file_stem

    home = played.home_at["END"]
    papers = home.sql("SELECT id, arxiv_id, version, status, lease_token, category_id, attempts, withdrawn_at "
                      "FROM papers ORDER BY id")
    assert papers

    missing_version = home.sql(
        "SELECT p.arxiv_id FROM papers p LEFT JOIN paper_versions v ON v.paper_id = p.id AND v.version = p.version "
        "WHERE v.id IS NULL ORDER BY p.arxiv_id")
    assert missing_version == []

    links = {pid: (low, n) for pid, low, n in home.sql(
        "SELECT paper_id, MIN(category_id), COUNT(*) FROM paper_categories GROUP BY paper_id")}
    analysed = {pid: v for pid, v in home.sql("SELECT paper_id, paper_version FROM analyses")}
    for pid, arxiv_id, version, status, lease, category_id, attempts, withdrawn in papers:
        assert pid in links and links[pid][0] == category_id, arxiv_id
        assert (status == "done") == (analysed.get(pid) == version), arxiv_id
        assert analysed.get(pid, 0) <= version, arxiv_id
        assert lease is None or status == "processing", arxiv_id

    for arxiv_id, version, sha in home.sql(
            "SELECT p.arxiv_id, t.version, t.sha256 FROM paper_texts t JOIN papers p ON p.id = t.paper_id"):
        path = home.path / "texts" / f"{file_stem(arxiv_id)}v{version}.txt.gz"
        assert path.is_file(), path.name
        assert hashlib.sha256(gzip.decompress(path.read_bytes())).hexdigest() == sha, path.name

    first_kind = {}
    for pid, kind in home.sql("SELECT paper_id, kind FROM paper_events ORDER BY paper_id, id"):
        first_kind.setdefault(pid, kind)
    assert sorted(first_kind) == sorted(row[0] for row in papers)
    assert set(first_kind.values()) == {"announced"}

    final = {arxiv_id: (attempts, status) for _, arxiv_id, _, status, _, _, attempts, withdrawn in papers
             if withdrawn is not None}
    assert final, "no withdrawn paper in the played corpus"
    for name, earlier in sorted(played.home_at.items()):
        for arxiv_id, attempts in earlier.sql("SELECT arxiv_id, attempts FROM papers WHERE withdrawn_at IS NOT NULL"):
            assert final[arxiv_id][0] == attempts, (name, arxiv_id)
    assert all(status != "processing" for _, status in final.values())
