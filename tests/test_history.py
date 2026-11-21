"""Paper versions, the timeline and the text store over a few days of a small world."""
import gzip
import hashlib
import json
import re

import pytest

from conftest import new_home, new_standin, totals

KEPT = "2401.90001"         # analysed, replaced, analysed again
GONE = "2401.90002"         # analysed, then replaced by a withdrawal
FAILING = "2401.90003"      # fails three times, is retried by hand, then analysed
WITHDRAWN = "2401.90004"    # withdrawn when it is first stored

DAY_ONE = {"cs.LG": [[KEPT, 1, "new"], [GONE, 1, "new"], [FAILING, 1, "new"]], "stat.ML": []}
LATER = {"cs.LG": [[KEPT, 2, "replace"], [GONE, 2, "replace"], [FAILING, 1, "new"]],
         "stat.ML": [[WITHDRAWN, 1, "new"]]}


def stamp(value) -> str:
    return str(value)[:19].replace("T", " ")


def _done(results):
    for result in results:
        assert result.status == 0, result
    return results


class Story:
    def __init__(self, home, standin, runs, after_failures):
        self.home = home
        self.standin = standin
        self.runs = runs                      # the `run` line of each cycle
        self.after_failures = after_failures  # stand-in counters after the third failed try


@pytest.fixture(scope="module")
def story(tmp_path_factory):
    root = tmp_path_factory.mktemp("history")
    world = new_standin(root / "standin", scenario={"feeds": DAY_ONE, "bad": [FAILING]})
    try:
        home = new_home(root / "home").join(world).seeds("cs.LG", "stat.ML")
        runs = [_done(home.batch(["clock", "set", "2026-08-10T08:00Z"], ["cycle"]))[1].lines[0]]
        world.scenario(feeds=LATER, bad=[FAILING], withdrawn=[GONE, WITHDRAWN])
        later = _done(home.batch(["clock", "advance", "1d"], ["cycle"], ["clock", "advance", "1d"], ["cycle"]))
        runs += [later[1].lines[0], later[3].lines[0]]
        after_failures = world.counts()
        world.scenario(feeds=LATER, withdrawn=[GONE, WITHDRAWN])
        last = _done(home.batch(["paper", "retry", FAILING], ["cycle"]))
        assert last[0].lines == [f"queued {FAILING}"]
        runs.append(last[1].lines[0])
        yield Story(home, world, runs, after_failures)
    finally:
        world.close()


def _entries(home, arxiv_id) -> list[tuple]:
    return home.sql("SELECT e.kind, e.version, e.feed, e.at FROM paper_events e JOIN papers p ON p.id = e.paper_id "
                    "WHERE p.arxiv_id = ? ORDER BY e.id", (arxiv_id,))


def _in_order(entries, wanted) -> bool:
    """True when the wanted entries occur among the paper's entries in this order."""
    rest = iter(entries)
    return all(any(entry == want for entry in rest) for want in wanted)


def test_timeline_tells_what_happened_to_a_paper(story):
    counts = [{k: v for k, v in totals(line).items() if k != "run"} for line in story.runs]
    assert counts == [
        {"new": 3, "updated": 0, "skipped": 0, "processed": 2, "failed": 1},
        {"new": 1, "updated": 2, "skipped": 0, "processed": 1, "failed": 1},
        {"new": 0, "updated": 0, "skipped": 0, "processed": 0, "failed": 1},
        {"new": 0, "updated": 0, "skipped": 0, "processed": 1, "failed": 0},
    ]
    day1, day2, day3 = "2026-08-10 08:00:00", "2026-08-11 08:00:00", "2026-08-12 08:00:00"
    wanted = {
        KEPT: [("announced", 1, "cs.LG", day1), ("analysed", 1, None, day1),
               ("replaced", 2, "cs.LG", day2), ("analysed", 2, None, day2)],
        GONE: [("announced", 1, "cs.LG", day1), ("analysed", 1, None, day1),
               ("replaced", 2, "cs.LG", day2), ("withdrawn", 2, "cs.LG", day2)],
        FAILING: [("announced", 1, "cs.LG", day1), ("failed", 1, None, day1), ("failed", 1, None, day2),
                  ("failed", 1, None, day3), ("retried", 1, None, day3), ("analysed", 1, None, day3)],
        WITHDRAWN: [("announced", 1, "stat.ML", day2), ("withdrawn", 1, "stat.ML", day2)],
    }
    for arxiv_id, entries in wanted.items():
        got = [(kind, version, feed, stamp(at)) for kind, version, feed, at in _entries(story.home, arxiv_id)]
        assert got and got[0][0] == "announced", (arxiv_id, got)
        assert _in_order(got, entries), (arxiv_id, got)

    states = {row[0]: row[1:] for row in story.home.sql("SELECT arxiv_id, status, version, attempts FROM papers")}
    assert states[GONE] == ("pending", 2, 0) and states[WITHDRAWN] == ("pending", 1, 0), states
    assert states[KEPT][:2] == ("done", 2) and states[FAILING][:2] == ("done", 1), states
    withdrawn = dict(story.home.sql("SELECT arxiv_id, withdrawn_at FROM papers"))
    assert (stamp(withdrawn[GONE]), stamp(withdrawn[WITHDRAWN])) == (day2, day2)
    assert withdrawn[KEPT] is None and withdrawn[FAILING] is None
    # a withdrawn paper is never asked for: neither its PDF nor an analysis
    assert story.standin.count(f"pdf:{WITHDRAWN}") == story.standin.count(f"llm:{WITHDRAWN}") == 0


def test_version_rows_keep_what_each_stored_version_said(story):
    rows = story.home.sql("SELECT p.arxiv_id, v.version, v.title, v.abstract, v.authors, v.categories, v.comment, "
                          "v.updated_at, v.seen_at FROM paper_versions v JOIN papers p ON p.id = v.paper_id "
                          "ORDER BY p.arxiv_id, v.version")
    got = {(row[0], row[1]): (row[2], row[3], json.loads(row[4]), json.loads(row[5]), row[6], stamp(row[7]),
                              stamp(row[8])) for row in rows}

    def said(arxiv_id, version, seen, comment=None):
        return (f"Paper {arxiv_id} (v{version})", f"Abstract of {arxiv_id}, version {version}.",
                ["A. Author", "B. Author"], ["cs.LG"], comment, f"2024-01-0{version} 10:00:00", seen)

    gone = "This paper has been withdrawn."
    assert got[(KEPT, 1)] == said(KEPT, 1, "2026-08-10 08:00:00")
    assert got[(KEPT, 2)] == said(KEPT, 2, "2026-08-11 08:00:00")
    assert got[(GONE, 1)] == said(GONE, 1, "2026-08-10 08:00:00")
    assert got[(GONE, 2)] == said(GONE, 2, "2026-08-11 08:00:00", gone)
    assert got[(WITHDRAWN, 1)][4] == gone and got[(WITHDRAWN, 1)][3] == ["stat.ML"]

    # the paper row holds its current version; its first submission and its first sighting stay
    paper = story.home.sql("SELECT title, abstract, comment, updated_at, published_at, created_at FROM papers "
                           "WHERE arxiv_id = ?", (GONE,))[0]
    assert paper[:3] == (f"Paper {GONE} (v2)", f"Abstract of {GONE}, version 2.", gone)
    assert [stamp(value) for value in paper[3:]] == ["2024-01-02 10:00:00", "2024-01-01 10:00:00",
                                                     "2026-08-10 08:00:00"]


def test_stored_text_follows_the_analysed_version(story):
    rows = story.home.sql("SELECT p.arxiv_id, t.version, t.sha256, t.chars, t.extraction FROM paper_texts t "
                          "JOIN papers p ON p.id = t.paper_id ORDER BY p.arxiv_id")
    texts = {row[0]: row[1:] for row in rows}
    assert texts[KEPT][0] == 2 and texts[FAILING][0] == 1, texts
    files = sorted(p.name for p in (story.home.path / "texts").iterdir())
    assert files == sorted(f"{arxiv_id}v{row[0]}.txt.gz" for arxiv_id, row in texts.items())
    for arxiv_id, (version, sha256, chars, extraction) in texts.items():
        data = gzip.decompress((story.home.path / "texts" / f"{arxiv_id}v{version}.txt.gz").read_bytes())
        text = data.decode("utf-8")
        assert (hashlib.sha256(data).hexdigest(), len(text), extraction) == (sha256, chars, "pdf"), arxiv_id
        assert f"Full text of paper {arxiv_id}v{version}." in text, (arxiv_id, text)
    analysed = dict(story.home.sql("SELECT p.arxiv_id, a.paper_version FROM analyses a JOIN papers p ON p.id = a.paper_id"))
    assert analysed[KEPT] == texts[KEPT][0] and analysed[FAILING] == texts[FAILING][0]

    # three tries that failed at the model and one that got an answer, all on the text fetched the
    # first time (the model client may ask more than once within one try)
    asked = story.after_failures.get(f"llm:{FAILING}", 0)
    assert story.after_failures.get(f"pdf:{FAILING}") == 1 and asked >= 3, story.after_failures
    assert story.standin.count(f"pdf:{FAILING}") == 1 and story.standin.count(f"llm:{FAILING}") > asked
    assert story.standin.count(f"pdf:{KEPT}") == 2
    error = story.home.sql("SELECT error_message FROM papers WHERE arxiv_id = ?", (FAILING,))
    assert error == [(None,)]


def _files(home) -> dict[str, tuple[int, bytes]]:
    out = {}
    for path in sorted(home.path.rglob("*")):
        if path.is_file() and not path.name.startswith("lens.db"):
            out[str(path.relative_to(home.path))] = (path.stat().st_mtime_ns, path.read_bytes())
    return out


def _rows_but_runs(home) -> dict[str, list]:
    tables = [row[0] for row in home.sql("SELECT name FROM sqlite_master WHERE type = 'table' "
                                         "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {t: sorted(home.sql(f"SELECT * FROM {t}"), key=repr) for t in tables if t not in ("runs", "run_feeds")}


def test_a_second_cycle_on_the_same_day_changes_only_the_run_tables(story, tmp_path):
    home = story.home.copy(tmp_path / "again")
    info, rows, files = home.run("db", "info").lines, _rows_but_runs(home), _files(home)
    line = home.run("cycle").lines
    assert len(line) == 1 and re.match(r"^run \d+: new=0 updated=0 skipped=0 processed=0 failed=0$", line[0]), line
    after = home.run("db", "info").lines
    changed = [(a, b) for a, b in zip(info, after) if a != b]
    assert len(info) == len(after) and [a.split("=")[0] for a, _ in changed] == ["run_feeds", "runs"], changed
    counts = {a.split("=")[0]: (int(a.split("=")[1]), int(b.split("=")[1])) for a, b in changed}
    assert counts["runs"][1] == counts["runs"][0] + 1 and counts["run_feeds"][1] == counts["run_feeds"][0] + 2
    assert _rows_but_runs(home) == rows
    assert _files(home) == files
