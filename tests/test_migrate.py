"""Recorded schema steps: an empty database and the two earlier schemas end in the same place."""
import gzip
import hashlib
import json
import sqlite3
import subprocess
import sys
import urllib.parse
from contextlib import closing
from functools import partial

import pytest

from conftest import ROOT, new_home, run_all

STARTS = {"empty": None, "original": "original", "reworked": "reworked"}
TABLES = ["analyses", "categories", "conversations", "messages", "paper_categories", "paper_events",
          "paper_texts", "paper_versions", "papers", "run_feeds", "runs", "schema_version", "seeded_categories"]
IDS = [f"2406.9000{n}" for n in range(1, 7)]
PLACEHOLDER_START = "[Text extraction failed"


def write_sample(target, schema: str) -> None:
    args = [sys.executable, str(ROOT / "dev" / "sample_db.py"), str(target)]
    if schema != "original":
        args += ["--schema", schema]
    done = subprocess.run(args, cwd=ROOT, capture_output=True, encoding="utf-8", timeout=300)
    assert done.returncode == 0 and done.stdout.startswith("wrote "), (args, done.stdout, done.stderr)


def as_written(path, query: str) -> list[tuple]:
    """Rows of a sample database as dev/sample_db.py wrote it, before any upgrade."""
    uri = "file:" + urllib.parse.quote(str(path)) + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        return [tuple(row) for row in connection.execute(query).fetchall()]


def stamp(value) -> str:
    """A stored time to the second, whichever way the driver wrote it."""
    return str(value)[:19].replace("T", " ")


def shape(home) -> dict[str, set[str]]:
    names = [row[0] for row in home.sql("SELECT name FROM sqlite_master WHERE type = 'table' "
                                        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {name: {row[1] for row in home.sql(f"PRAGMA table_info({name})")} for name in names}


def dump(home, times: bool = False) -> dict:
    """Every row of every table, and the decompressed text files; the step times only when asked."""
    out = {}
    for table, columns in sorted(shape(home).items()):
        keep = sorted(c for c in columns if times or (table, c) != ("schema_version", "applied_at"))
        out[table] = sorted(home.sql(f"SELECT {', '.join(keep)} FROM {table}"), key=repr)
    texts = home.path / "texts"
    out["texts/"] = {p.name: gzip.decompress(p.read_bytes()) for p in sorted(texts.iterdir())} if texts.exists() else {}
    return out


class Upgraded:
    def __init__(self, homes, first, written):
        self.homes = homes          # name -> Home, upgraded once
        self.first = first          # name -> Result of that first `db upgrade`
        self.written = written      # name -> the same sample database, never upgraded


@pytest.fixture(scope="module")
def upgraded(tmp_path_factory):
    root = tmp_path_factory.mktemp("migrate")
    homes = {name: new_home(root / name) for name in STARTS}
    written, jobs = {}, []
    for name, schema in STARTS.items():
        if schema:
            homes[name].path.mkdir(parents=True)
            written[name] = root / f"{name}-as-written.db"
            jobs += [partial(write_sample, homes[name].path / "lens.db", schema), partial(write_sample, written[name], schema)]
    run_all(jobs)
    first = dict(zip(STARTS, run_all([partial(h.run, "db", "upgrade") for h in homes.values()])))
    return Upgraded(homes, first, written)


def _steps(result) -> list[list[str]]:
    return [line.split(" ") for line in result.lines[:-1]]


def _sample_home(path, schema: str):
    home = new_home(path)
    home.path.mkdir(parents=True)
    write_sample(home.path / "lens.db", schema)
    return home


def test_legacy_database_is_upgraded_in_place(upgraded):
    first = upgraded.first["original"]
    assert first.lines[-1].startswith("schema ") and _steps(first), first
    assert all(step[0] == "step" for step in _steps(first)), first
    home = upgraded.homes["original"]

    papers = {row[0]: row[1:] for row in home.sql(
        "SELECT arxiv_id, version, status, attempts, error_message, lease_token, category_id FROM papers")}
    # one row per paper under the id without its version; the highest version is kept; whatever
    # state the old rows were in, a paper is done only when its current version was analysed
    assert papers == {
        "2406.90001": (2, "done", 0, None, None, 1),
        "2406.90002": (3, "pending", 0, None, None, 1),
        "2406.90003": (1, "pending", 0, None, None, 2),
        "2406.90004": (2, "pending", 0, None, None, 2),
        "2406.90005": (1, "done", 0, None, None, 1),
        "2406.90006": (1, "done", 0, None, None, 2),
    }
    links = home.sql("SELECT p.arxiv_id, c.arxiv_code FROM paper_categories pc JOIN papers p ON p.id = pc.paper_id "
                     "JOIN categories c ON c.id = pc.category_id ORDER BY p.arxiv_id, c.arxiv_code")
    assert links == [("2406.90001", "cs.AI"), ("2406.90001", "cs.LG"), ("2406.90002", "cs.AI"),
                     ("2406.90002", "cs.LG"), ("2406.90003", "cs.LG"), ("2406.90004", "cs.LG"),
                     ("2406.90005", "cs.AI"), ("2406.90006", "cs.LG")]
    earliest = {}
    for arxiv_id, created in as_written(upgraded.written["original"], "SELECT arxiv_id, created_at FROM papers"):
        base = arxiv_id.split("v")[0]
        earliest[base] = min(earliest.get(base, stamp(created)), stamp(created))
    assert {a: stamp(c) for a, c in home.sql("SELECT arxiv_id, created_at FROM papers")} == earliest

    # in place: the home still holds one database file and no copy of it
    holders = sorted(p.name for p in home.path.rglob("*") if p.is_file() and p.read_bytes()[:16] == b"SQLite format 3\x00")
    assert holders == ["lens.db"]


def test_current_database_is_left_as_it_is_on_restart(upgraded, tmp_path):
    schemas = {name: result.lines[-1] for name, result in upgraded.first.items()}
    assert len(set(schemas.values())) == 1, schemas
    for name, home in upgraded.homes.items():
        twin = home.copy(tmp_path / name)
        before = dump(twin, times=True)
        again = twin.run("db", "upgrade")
        assert again.lines == [schemas[name]], (name, again)
        assert dump(twin, times=True) == before, name
    # a current database keeps the states of its papers: the failed one stays failed
    states = {row[0]: row[1:] for row in upgraded.homes["reworked"].sql("SELECT arxiv_id, status, attempts FROM papers")}
    assert states["2406.90003"] == ("failed", 3) and states["2406.90004"] == ("processing", 1), states


def test_three_starting_points_end_with_the_same_tables_and_columns(upgraded, tmp_path):
    shapes = {name: shape(home) for name, home in upgraded.homes.items()}
    assert set(TABLES) <= set(shapes["empty"]), sorted(shapes["empty"])
    assert shapes["original"] == shapes["empty"] and shapes["reworked"] == shapes["empty"]
    infos = {name: [line.split("=")[0] for line in home.run("db", "info").lines] for name, home in upgraded.homes.items()}
    assert infos["original"] == infos["empty"] == infos["reworked"], infos

    for schema in ("original", "reworked"):
        home = _sample_home(tmp_path / schema, schema)
        with closing(sqlite3.connect(home.path / "lens.db")) as connection:
            connection.execute("INSERT INTO conversations (id, title, research_interest, created_at, updated_at) "
                               "VALUES (1, 'Old chat', 'graphs', '2024-06-02 12:00:00.000000', '2024-06-02 12:00:00.000000')")
            connection.execute("INSERT INTO messages (id, conversation_id, role, content, created_at) "
                               "VALUES (1, 1, 'user', 'hello', '2024-06-02 12:00:00.000000')")
            connection.commit()
        home.run("db", "upgrade")
        assert home.sql("SELECT id, title, research_interest FROM conversations") == [(1, "Old chat", "graphs")], schema
        assert home.sql("SELECT conversation_id, role, content FROM messages") == [(1, "user", "hello")], schema


def test_older_rows_of_a_paper_are_kept_as_version_rows(upgraded):
    expected = {}
    for arxiv_id, title, abstract, authors, categories, created in as_written(
            upgraded.written["original"], "SELECT arxiv_id, title, abstract, authors, categories, created_at FROM papers"):
        base, version = arxiv_id.split("v")
        expected[(base, int(version))] = (title, abstract, json.loads(authors), json.loads(categories), None, None,
                                          stamp(created))
    query = ("SELECT p.arxiv_id, v.version, v.title, v.abstract, v.authors, v.categories, v.comment, v.updated_at, "
             "v.seen_at FROM paper_versions v JOIN papers p ON p.id = v.paper_id")
    got = {(row[0], row[1]): (row[2], row[3], json.loads(row[4]), json.loads(row[5]), row[6], row[7], stamp(row[8]))
           for row in upgraded.homes["original"].sql(query)}
    assert got == expected
    assert sorted(got) == [("2406.90001", 1), ("2406.90001", 2), ("2406.90002", 1), ("2406.90002", 3),
                           ("2406.90003", 1), ("2406.90004", 2), ("2406.90005", 1), ("2406.90006", 1)]

    # a database of the reworked schema had no version rows: each paper gets one for its version
    rework = upgraded.homes["reworked"]
    papers = {row[0]: row[1:] for row in rework.sql("SELECT arxiv_id, version, title, abstract, created_at FROM papers")}
    rows = rework.sql(query)
    assert sorted((row[0], row[1]) for row in rows) == sorted((a, p[0]) for a, p in papers.items())
    for row in rows:
        version, title, abstract, created = papers[row[0]]
        assert (row[2], row[3], stamp(row[8])) == (title, abstract, stamp(created)), row


def test_an_analysis_keeps_its_version_and_an_older_one_leaves_the_paper_pending(upgraded):
    query = ("SELECT p.arxiv_id, a.paper_version, a.summary, p.version, p.status FROM analyses a "
             "JOIN papers p ON p.id = a.paper_id")
    got = {row[0]: row[1:] for row in upgraded.homes["original"].sql(query)}
    assert sorted(got) == ["2406.90001", "2406.90002", "2406.90005", "2406.90006"]
    assert got["2406.90001"] == (2, "A2", 2, "done")
    # the only analysis of 2406.90002 describes version 1; the paper holds version 3 and waits
    assert got["2406.90002"] == (1, "B1", 3, "pending")
    for arxiv_id in ("2406.90005", "2406.90006"):
        assert (got[arxiv_id][0], got[arxiv_id][2:]) == (1, (1, "done")), arxiv_id
    written = {row[0] for row in as_written(upgraded.written["original"], "SELECT summary FROM analyses")}
    assert {row[1] for row in got.values()} == written - {"A1"}

    for arxiv_id, paper_version, _, version, status in upgraded.homes["reworked"].sql(query):
        assert (paper_version, status) == (version, "done"), arxiv_id


def test_texts_move_into_the_text_store_and_the_column_goes(upgraded):
    for name in ("original", "reworked"):
        home = upgraded.homes[name]
        assert "full_text" not in shape(home)["analyses"], name
        texts = dict(as_written(upgraded.written[name], "SELECT summary, full_text FROM analyses"))
        rows = home.sql("SELECT p.arxiv_id, a.summary, a.paper_version, t.version, t.sha256, t.chars, t.extraction "
                        "FROM analyses a JOIN papers p ON p.id = a.paper_id "
                        "LEFT JOIN paper_texts t ON t.paper_id = p.id ORDER BY p.arxiv_id")
        files = set()
        for arxiv_id, summary, paper_version, version, sha256, chars, extraction in rows:
            text = texts[summary]
            if text is None:
                continue
            data = text.encode("utf-8")
            kind = "abstract" if text.startswith(PLACEHOLDER_START) else "pdf"
            assert (version, sha256, chars, extraction) == (
                paper_version, hashlib.sha256(data).hexdigest(), len(text), kind), (name, arxiv_id)
            file = home.path / "texts" / f"{arxiv_id.replace('/', '_')}v{version}.txt.gz"
            assert gzip.decompress(file.read_bytes()) == data, (name, arxiv_id)
            files.add(file.name)
        assert files and sorted(p.name for p in (home.path / "texts").iterdir()) == sorted(files), name


def _announced_once(home) -> None:
    lowest = dict(home.sql("SELECT paper_id, min(version) FROM paper_versions GROUP BY paper_id"))
    events = home.sql("SELECT paper_id, kind, version, feed, at FROM paper_events ORDER BY id")
    for paper_id, arxiv_id, created in home.sql("SELECT id, arxiv_id, created_at FROM papers"):
        mine = [event for event in events if event[0] == paper_id]
        announced = [event for event in mine if event[1] == "announced"]
        assert len(announced) == 1 and mine[0][1] == "announced", (arxiv_id, mine)
        assert announced[0][2:4] == (lowest[paper_id], None), (arxiv_id, announced)
        assert stamp(announced[0][4]) == stamp(created), (arxiv_id, announced)


def test_every_paper_gets_one_announced_entry(upgraded, tmp_path):
    assert len(upgraded.homes["original"].sql("SELECT id FROM papers")) == len(IDS)
    for name, home in upgraded.homes.items():
        _announced_once(home)
        twin = home.copy(tmp_path / name)
        twin.run("db", "upgrade")
        _announced_once(twin)


def test_a_step_ended_at_its_commit_is_applied_by_the_next_start(upgraded, tmp_path):
    first = upgraded.first["original"]
    steps = _steps(first)
    names = [step[2] for step in steps]
    reference = dump(upgraded.homes["original"])
    for name in ("clock_texts", "timeline"):
        k = names.index(name)
        home = _sample_home(tmp_path / name, "original")
        # the first commit records the table of steps, then each step commits once
        ended = home.run("db", "upgrade", env={"ARXIVLENS_KILL_AT": f"commit:{k + 2}"}, check=False)
        assert ended.status == 137, (name, ended)
        recorded = [row[0] for row in home.sql("SELECT version FROM schema_version ORDER BY version")]
        assert recorded == [int(step[1]) for step in steps[:k]], (name, recorded)
        if name == "clock_texts":
            assert "full_text" in shape(home)["analyses"]
        again = home.run("db", "upgrade")
        assert again.lines == first.lines[k:], (name, again)
        assert dump(home) == reference, name


def test_two_processes_upgrading_at_once_apply_each_step_once(upgraded, tmp_path):
    first = upgraded.first["original"]
    home = _sample_home(tmp_path / "home", "original")
    one, two = run_all([partial(home.run, "db", "upgrade"), partial(home.run, "db", "upgrade")])
    assert one.lines[-1] == two.lines[-1] == first.lines[-1], (one, two)
    applied = one.lines[:-1] + two.lines[:-1]
    assert sorted(applied) == sorted(first.lines[:-1]), (one, two)
    assert dump(home) == dump(upgraded.homes["original"])


def test_a_newer_database_is_refused(upgraded, tmp_path):
    latest = int(upgraded.first["empty"].lines[-1].split(" ")[1])
    newer = latest + 100
    twin = upgraded.homes["empty"].copy(tmp_path / "newer")
    with closing(sqlite3.connect(twin.path / "lens.db")) as connection:
        connection.execute("INSERT INTO schema_version (version, name, applied_at) VALUES (?, ?, ?)",
                           (newer, "later", "2026-08-10 08:00:00.000000"))
        connection.commit()
    before = dump(twin, times=True)
    for args in (["db", "upgrade"], ["db", "info"]):
        result = twin.run(*args, check=False)
        assert (result.status, result.stdout) == (2, ""), (args, result)
        assert result.stderr.splitlines() == [
            f"error: database schema {newer} is newer than this version ({latest})"], (args, result)
    assert dump(twin, times=True) == before
