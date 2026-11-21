"""Failure switches: which calls end the process, how it ends, and what the next cycle makes of it."""
import gzip
import hashlib
import json
from functools import partial

from conftest import SMALL, new_home, run_all

# Calls the file-system switch counts, with how many counted calls each makes.
COUNTED = {
    "os.replace": 1, "os.rename": 1, "os.unlink": 1, "os.remove": 1, "os.rmdir": 1, "os.mkdir": 1,
    "os.symlink": 1, "os.link": 1, "os.truncate": 1, "os.ftruncate": 1,
    "os.open-wronly": 1, "os.open-rdwr": 1, "os.open-creat": 1, "os.open-trunc": 1, "os.open-append": 1,
    "open-w": 1, "open-wb": 1, "open-a": 1, "open-x": 1, "open-r+": 1, "io.open-w": 1, "gzip.open-wb": 1,
    "pathlib.write_text": 1, "pathlib.write_bytes": 1, "pathlib.open-a": 1, "pathlib.mkdir": 1,
    "pathlib.unlink": 1, "pathlib.rmdir": 1, "pathlib.rename": 1, "pathlib.replace": 1,
    "pathlib.symlink_to": 1,
    "os.makedirs-two-levels": 2, "os.makedirs-existing": 1, "shutil.rmtree": 2,
}
NOT_COUNTED = ["open-r", "open-rb", "os.open-rdonly", "open-descriptor", "open-opener", "os.utime",
               "os.chmod", "os.fsync", "os.stat", "os.listdir"]
MALFORMED = ["fs:0", "fs:", "fs:-1", "fs:one", "fs:1.5", "commit:0", "disk:2", "fs", "2", "fs:1:2"]
KILL_MESSAGE = "ARXIVLENS_KILL_AT: expected fs:N or commit:N"

# Points inside the first day's play; the marker and the clock come first, then the text files
# of the papers, so the higher values end the play while it works on its second or third paper.
FS_POINTS = [1, 3, 5, 8, 11]
COMMIT_POINTS = [1, 3, 9, 20, 28]


def _scratch(root, names):
    """A folder per operation, holding what the operation works on."""
    for name in names:
        folder = root / name
        (folder / "sub").mkdir(parents=True)
        (folder / "existing").mkdir()
        (folder / "tree" / "empty").mkdir(parents=True)
        (folder / "f").write_text("x\n")
    return root


def _kill(value):
    return {"ARXIVLENS_KILL_AT": value}


def _start_day():
    return json.loads((SMALL / "corpus.json").read_text(encoding="utf-8"))["start"]


def test_each_file_system_change_is_counted_once(home, tmp_path):
    names = list(COUNTED) + NOT_COUNTED
    roots = [_scratch(tmp_path / f"one-{i}", [name]) for i, name in enumerate(names)]
    alone = run_all([partial(home.probe, "fs_calls", root, name, env=_kill("fs:1"), check=False)
                     for root, name in zip(roots, names)])
    for name, result in zip(names, alone):
        expected = 137 if name in COUNTED else 0
        assert result.status == expected, (name, result)
        if name in COUNTED:
            assert (result.stdout, result.stderr) == ("", ""), (name, result)
        else:
            assert result.lines == ["started", "done", "faults=yes"], (name, result)

    # all counted operations in one process: the total is the sum of the counts above, so
    # each operation made exactly the calls listed, no more
    total = sum(COUNTED.values())  # ints
    runs = [(_scratch(tmp_path / "all-last", COUNTED), total), (_scratch(tmp_path / "all-past", COUNTED), total + 1)]
    last, past = run_all([partial(home.probe, "fs_calls", root, *COUNTED, env=_kill(f"fs:{n}"), check=False)
                          for root, n in runs])
    assert last.status == 137, last
    assert (past.status, past.lines) == (0, ["started", "done", "faults=yes"]), past


def test_the_process_ends_silently_with_137_and_a_malformed_value_ends_with_2(home, tmp_path):
    root = _scratch(tmp_path / "ops", ["os.mkdir"])
    ended = home.probe("fs_calls", root, "os.mkdir", env=_kill("fs:1"), check=False)
    # the buffered line, the exit handlers: nothing of the process reaches its outputs
    assert (ended.status, ended.stdout, ended.stderr) == (137, "", ""), ended
    assert not (root / "os.mkdir" / "new").exists()

    for env in (None, _kill("")):
        free = home.probe("fs_calls", root, "os.mkdir", env=env)
        assert free.lines == ["started", "done", "faults=no"], (env, free)
        assert (root / "os.mkdir" / "new").is_dir()
        (root / "os.mkdir" / "new").rmdir()

    refused = run_all([partial(home.run, "clock", "show", env=_kill(value), check=False) for value in MALFORMED])
    for value, result in zip(MALFORMED, refused):
        lines = result.stderr.splitlines()
        assert (result.status, result.stdout) == (2, ""), (value, result)
        assert len(lines) == 1 and lines[0].endswith(KILL_MESSAGE), (value, result)


def test_commits_are_counted_and_reads_are_not(home):
    home.run("clock", "set", "2026-08-10T08:00Z")
    home.run("db", "upgrade")
    # a current database is only read: commands that read survive a switch set to the first commit
    for args, status in ((["db", "upgrade"], 0), (["category", "list"], 0), (["paper", "show", "2401.90001"], 1)):
        result = home.run(*args, env=_kill("commit:1"), check=False)
        assert result.status == status, (args, result)

    ended = home.run("category", "add", "cs.LG", env=_kill("commit:1"), check=False)
    assert (ended.status, ended.stdout, ended.stderr) == (137, "", ""), ended
    assert "cs.LG" not in home.run("category", "list").stdout
    assert home.run("category", "add", "cs.LG", env=_kill("commit:2")).lines == ["added cs.LG"]
    assert [line.split("\t")[1] for line in home.run("category", "list").lines] == ["cs.LG"]


def test_slow_keeps_an_analysis_answered_in_process_waiting(home, make_home):
    start = _start_day()
    day = json.loads((SMALL / "days" / f"{start}.json").read_text(encoding="utf-8"))
    ids = sorted({entry[0] for listing in day["feeds"].values() for entry in listing})
    assert ids, "the first day of the small corpus announces papers"

    play = home.start("play", "--corpus", SMALL, "--fast", "--day", start, env={"ARXIVLENS_SLOW": "120"})
    try:
        seen = None
        for _ in range(200):
            assert play.poll() is None, play.communicate()
            shown = home.batch(*[["paper", "show", arxiv_id] for arxiv_id in ids])
            seen = [r.lines[0] for r in shown if r.status == 0 and "status      processing" in r.lines]
            if seen:
                break
        assert seen, "no paper was seen processing"
        # the analysis is still being waited for: the play has not ended
        assert play.poll() is None
    finally:
        play.kill()
        play.communicate()

    for value in ("soon", "-1"):
        other = make_home(f"slow-{value}")
        result = other.run("play", "--corpus", SMALL, "--fast", "--day", start,
                           env={"ARXIVLENS_SLOW": value}, check=False)
        assert result.status == 2 and "ARXIVLENS_SLOW" in result.stderr, (value, result)


def _facts(home) -> dict:
    """What a finished day leaves behind, without times, attempts or run records."""
    events: dict[str, list[str]] = {}
    for arxiv_id, kind in home.sql("SELECT p.arxiv_id, e.kind FROM paper_events e JOIN papers p ON p.id = e.paper_id "
                                   "ORDER BY p.arxiv_id, e.id"):
        events.setdefault(arxiv_id, []).append(kind)
    texts = home.sql("SELECT p.arxiv_id, t.version, t.sha256, t.chars, t.extraction FROM paper_texts t "
                     "JOIN papers p ON p.id = t.paper_id ORDER BY p.arxiv_id")
    for arxiv_id, version, sha256, chars, _ in texts:
        stem = arxiv_id.replace("/", "_")
        text = gzip.decompress((home.path / "texts" / f"{stem}v{version}.txt.gz").read_bytes())
        assert hashlib.sha256(text).hexdigest() == sha256 and len(text.decode("utf-8")) == chars, arxiv_id
    return {
        "papers": home.sql("SELECT arxiv_id, status, version FROM papers ORDER BY arxiv_id"),
        "links": home.sql("SELECT p.arxiv_id, c.arxiv_code FROM paper_categories pc JOIN papers p ON p.id = pc.paper_id "
                          "JOIN categories c ON c.id = pc.category_id ORDER BY p.arxiv_id, c.arxiv_code"),
        "analyses": home.sql("SELECT p.arxiv_id, a.paper_version, a.summary, a.key_contributions, a.methodology, "
                             "a.limitations, a.keywords, a.novelty_score FROM analyses a "
                             "JOIN papers p ON p.id = a.paper_id ORDER BY p.arxiv_id"),
        "versions": home.sql("SELECT p.arxiv_id, v.version, v.title, v.abstract, v.authors, v.categories, v.comment, "
                             "v.updated_at FROM paper_versions v JOIN papers p ON p.id = v.paper_id "
                             "ORDER BY p.arxiv_id, v.version"),
        "texts": texts,
        "events": events,
    }


def test_a_cycle_ended_at_any_point_is_finished_by_the_next_one(tmp_path):
    start = _start_day()
    points = [None] + [f"fs:{n}" for n in FS_POINTS] + [f"commit:{n}" for n in COMMIT_POINTS]
    homes = [new_home(tmp_path / (point or "undisturbed").replace(":", "-")) for point in points]
    run_all([partial(h.run, "clock", "set", f"{start}T08:00:00Z") for h in homes])
    for h in homes:
        # The switch can end the play before it has recorded its corpus, and the cycle that
        # finishes the work has to meet the same world, so the marker is there from the start.
        marker = json.dumps({"fast": True, "path": str(SMALL)}, sort_keys=True, separators=(",", ":"))
        (h.path / "corpus").write_bytes(marker.encode("utf-8") + b"\n")

    plays = run_all([partial(h.run, "play", "--corpus", SMALL, "--fast", "--day", start,
                             env=_kill(point) if point else None, check=False)
                     for h, point in zip(homes, points)])
    for point, result in zip(points, plays):
        assert result.status == (137 if point else 0), (point, result)

    finished = run_all([partial(h.batch, ["clock", "advance", "601s"], ["cycle"]) for h in homes])
    for point, results in zip(points, finished):
        assert [r.status for r in results] == [0, 0], (point, results)

    expected = _facts(homes[0])
    assert [p for p in expected["papers"] if p[1] == "done"], expected["papers"]
    for point, h in zip(points[1:], homes[1:]):
        got = _facts(h)
        for key in expected:
            assert got[key] == expected[key], (point, key)
