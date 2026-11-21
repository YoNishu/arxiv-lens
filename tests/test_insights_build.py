"""Whole insights builds on played homes: equal bytes, stats, work counts, interrupted builds and reading back."""

import hashlib
import json
import re
import shutil

from conftest import SMALL, run_all

FIXED_FILES = [
    "papers.jsonl", "text/tokens.jsonl", "text/refs.jsonl", "vocab.json", "vectors.jsonl", "similar.jsonl",
    "citations/edges.jsonl", "citations/external.jsonl", "citations/nodes.jsonl", "citations/pagerank.jsonl",
    "centroids.json", "assignments.jsonl", "topics.json", "authors.json", "ranking.jsonl",
]
NAMES = [
    "snapshot.rows", "text.read", "text.parsed", "text.tokenized", "vocab.fits", "vectors.built",
    "similar.searched", "similar.scored", "citations.resolved", "citations.lists", "citations.graphs",
    "topics.fits", "topics.assigned", "labels.asked", "trends.feeds", "trends.authors",
    "ranking.buckets", "digests.pages",
]
STAGES = {
    "snapshot": ["papers"],
    "text": ["papers", "texts", "entries", "arxiv_refs", "other_refs"],
    "vocab": ["papers", "terms"],
    "vectors": ["papers", "weights"],
    "similar": ["lists", "entries"],
    "citations": ["sources", "edges", "external", "nodes", "components"],
    "topics": ["topics", "assigned", "unassigned"],
    "trends": ["feeds"],
    "ranking": ["buckets", "ranked"],
    "digests": ["pages"],
}
INSTANT = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d")


def rows(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def by_id(items: list[dict]) -> dict[str, dict]:
    return {item["arxiv_id"]: item for item in items}


def files_of(folder) -> dict[str, bytes]:
    """Every file of a result folder but runs.jsonl, by relative path."""
    found = {}
    for path in sorted(folder.rglob("*")):
        rel = path.relative_to(folder).as_posix()
        if path.is_file() and rel != "runs.jsonl":
            found[rel] = path.read_bytes()
    return found


def differing(a: dict, b: dict) -> list[str]:
    return sorted(name for name in set(a) | set(b) if a.get(name) != b.get(name))


def built_line(folder) -> str:
    stats = load(folder / "stats.json")
    topics = load(folder / "topics.json")
    return (f"built {len(stats['files'])} files: {stats['inputs']['papers']} papers, {topics['k']} topics, "
            f"as of {stats['as_of']['day']}")


def settings_digest(home, monkeypatch) -> str:
    monkeypatch.setenv("ARXIVLENS_HOME", str(home.path))
    from app.insights import config

    return config.digest(config.load())


def joined_copy(tiny, labels, to):
    return tiny.copy(to).join(labels)


def test_two_builds_of_one_home_are_byte_equal(tiny, labels, tmp_path):
    home = joined_copy(tiny, labels, tmp_path / "h")
    before = files_of(home.insights)
    runs_before = (home.insights / "runs.jsonl").read_bytes().splitlines()
    result = home.run("insights", "build")

    assert differing(before, files_of(home.insights)) == []
    runs_after = (home.insights / "runs.jsonl").read_bytes().splitlines()
    assert len(runs_after) == len(runs_before) + 1 and runs_after[:-1] == runs_before
    assert result.lines == [built_line(home.insights)]


def test_a_build_does_not_depend_on_the_hash_seed(tiny, labels, tmp_path):
    home = joined_copy(tiny, labels, tmp_path / "h")
    seeds = ["0", "4242"]
    outs = [tmp_path / f"seed{seed}" for seed in seeds]
    run_all([lambda seed=seed, out=out: home.run("insights", "build", "--out", str(out),
                                                 env={"PYTHONHASHSEED": seed})
             for seed, out in zip(seeds, outs)])
    first, second = files_of(outs[0]), files_of(outs[1])
    assert "papers.jsonl" in first
    assert differing(first, second) == []
    assert differing(first, files_of(tiny.insights)) == []


def test_stats_name_every_result_file_with_size_and_hash(tiny, monkeypatch):
    folder = tiny.insights
    stats = load(folder / "stats.json")
    papers = rows(folder / "papers.jsonl")
    ranking = rows(folder / "ranking.jsonl")
    feeds = sorted({feed for paper in papers for feed in paper["feeds"]})
    pages = [f"digests/{bucket['feed']}/{bucket['week']}.md" for bucket in ranking]
    assert papers and pages

    assert sorted(stats) == ["as_of", "files", "inputs", "settings", "stages"]
    assert sorted(stats["files"]) == sorted(FIXED_FILES + [f"trends/{feed}.json" for feed in feeds] + pages)
    for name, entry in sorted(stats["files"].items()):
        data = (folder / name).read_bytes()
        assert entry == {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}, name

    states = [paper["state"] for paper in papers]
    assert stats["inputs"] == {
        "analysed": states.count("analysed"), "failed": states.count("failed"), "pending": states.count("pending"),
        "feeds": feeds, "papers": len(papers), "withdrawn": sum(1 for p in papers if p["withdrawn"]),  # ints
    }
    assert stats["settings"] == settings_digest(tiny, monkeypatch)
    assert rows(folder / "runs.jsonl")[-1]["settings"] == stats["settings"]
    assert sorted(stats["as_of"]) == ["day", "month", "week"]

    def walk(value, where):
        if isinstance(value, dict):
            for key, inner in value.items():
                assert key not in ("seconds", "started", "duration", "time", "total"), where + key
                walk(inner, where + key + ".")
        elif isinstance(value, list):
            for inner in value:
                walk(inner, where)
        elif isinstance(value, str):
            assert not INSTANT.match(value), where
    walk(stats, "")

    assert sorted(stats["stages"]) == sorted(STAGES)
    for name, keys in STAGES.items():
        assert sorted(stats["stages"][name]) == sorted(keys + ["version"]), name
        assert stats["stages"][name]["version"] == 1, name

    tokens = rows(folder / "text" / "tokens.jsonl")
    refs = rows(folder / "text" / "refs.jsonl")
    vocab = load(folder / "vocab.json")
    vectors = rows(folder / "vectors.jsonl")
    similar = rows(folder / "similar.jsonl")
    nodes = rows(folder / "citations" / "nodes.jsonl")
    assignments = rows(folder / "assignments.jsonl")
    recount = {
        "snapshot": {"papers": len(papers)},
        "text": {"papers": len(tokens), "texts": len(refs), "entries": sum(r["entries"] for r in refs),  # ints
                 "arxiv_refs": sum(len(r["cites"]) for r in refs), "other_refs": sum(r["other"] for r in refs)},  # ints
        "vocab": {"papers": vocab["reference"]["papers"], "terms": len(vocab["terms"])},
        "vectors": {"papers": len(vectors), "weights": sum(len(r["vector"]) for r in vectors)},  # ints
        "similar": {"lists": len(similar), "entries": sum(len(r["similar"]) for r in similar)},  # ints
        "citations": {
            "sources": sum(1 for p in papers if p["state"] == "analysed" and not p["withdrawn"]),  # ints
            "edges": len(rows(folder / "citations" / "edges.jsonl")),
            "external": len(rows(folder / "citations" / "external.jsonl")),
            "nodes": len(nodes),
            "components": len({r["component"] for r in rows(folder / "citations" / "pagerank.jsonl")}),
        },
        "topics": {"topics": load(folder / "topics.json")["k"],
                   "assigned": sum(1 for r in assignments if r["topic"] is not None),  # ints
                   "unassigned": sum(1 for r in assignments if r["topic"] is None)},  # ints
        "trends": {"feeds": len(feeds)},
        "ranking": {"buckets": len(ranking), "ranked": sum(len(b["papers"]) for b in ranking)},  # ints
        "digests": {"pages": len(pages)},
    }
    for name, counts in recount.items():
        stage = {key: value for key, value in stats["stages"][name].items() if key != "version"}
        assert stage == counts, name


def test_work_counts_equal_the_unit_counts(tiny, labels, tmp_path):
    home = joined_copy(tiny, labels, tmp_path / "h")
    asked_before = labels.count("label")
    clock, build, show = home.batch(["clock", "show"], ["insights", "build"], ["insights", "show", "work"])
    asked = labels.count("label") - asked_before
    assert build.status == 0 and show.status == 0, (build.stderr, show.stderr)

    stats = load(home.insights / "stats.json")
    last = rows(home.insights / "runs.jsonl")[-1]
    assert sorted(last) == ["as_of", "kind", "seconds", "settings", "started", "work"]
    assert last["kind"] == "build"
    assert last["started"] == clock.lines[0].split()[0]
    assert last["as_of"] == stats["as_of"]["day"] and last["settings"] == stats["settings"]
    assert sorted(last["seconds"]) == sorted(list(STAGES) + ["total"])
    assert all(isinstance(value, float) and value >= 0 for value in last["seconds"].values())

    work = last["work"]
    stage = stats["stages"]
    assert sorted(work) == sorted(NAMES)
    expected = {
        "snapshot.rows": stage["snapshot"]["papers"],
        "text.read": stage["text"]["texts"],
        "text.parsed": stage["text"]["texts"],
        "text.tokenized": stage["text"]["papers"],
        "vocab.fits": 1,
        "vectors.built": stage["vectors"]["papers"],
        "similar.searched": stage["similar"]["lists"],
        "citations.resolved": stage["citations"]["sources"],
        "citations.lists": stage["citations"]["nodes"],
        "citations.graphs": 1,
        "topics.fits": 1,
        "topics.assigned": stage["topics"]["assigned"] + stage["topics"]["unassigned"],
        "labels.asked": stage["topics"]["topics"],
        "trends.feeds": stage["trends"]["feeds"],
        "trends.authors": 1,
        "ranking.buckets": stage["ranking"]["buckets"],
        "digests.pages": stage["digests"]["pages"],
    }
    for name, value in expected.items():
        assert work[name] == value, name
    assert work["similar.scored"] > work["similar.searched"]
    assert stage["topics"]["topics"] > 0 and asked == stage["topics"]["topics"]

    printed = [line for line in show.lines if line.split("=", 1)[0] in NAMES]
    assert printed == [f"{name}={work[name]}" for name in NAMES]


def test_a_build_that_was_ended_is_made_whole_by_the_next(tiny, labels, tmp_path):
    feeds = load(tiny.insights / "stats.json")["inputs"]["feeds"]
    beyond_trends = 2 * (13 + len(feeds) + 1) + 3
    values = [1, 3, 12, 27, beyond_trends]
    homes = [joined_copy(tiny, labels, tmp_path / f"fs{n}") for n in values]

    def ended_then_whole(home, n):
        def job():
            ended = home.run("insights", "build", env={"ARXIVLENS_KILL_AT": f"fs:{n}"}, check=False)
            home.run("insights", "build")
            return ended
        return job

    ended = run_all([ended_then_whole(home, n) for home, n in zip(homes, values)])
    whole = files_of(tiny.insights)
    for n, home, result in zip(values, homes, ended):
        assert result.status == 137, (n, result.stderr)
        assert differing(whole, files_of(home.insights)) == [], n


def test_files_of_a_feed_that_left_are_removed(tiny, labels, tmp_path):
    feeds = load(tiny.insights / "stats.json")["inputs"]["feeds"]
    gone = feeds[-1]
    assert (tiny.insights / "trends" / f"{gone}.json").is_file()
    assert (tiny.insights / "digests" / gone).is_dir()
    home = joined_copy(tiny, labels, tmp_path / "h")
    fresh = tmp_path / "fresh"
    for result in home.batch(["category", "remove", gone], ["insights", "build"],
                             ["insights", "build", "--out", str(fresh)]):
        assert result.status == 0, result.stderr

    assert differing(files_of(fresh), files_of(home.insights)) == []
    assert not (home.insights / "trends" / f"{gone}.json").exists()
    assert not (home.insights / "digests" / gone).exists()
    stats = load(home.insights / "stats.json")
    assert gone not in stats["inputs"]["feeds"]
    assert not [name for name in stats["files"] if name.startswith(f"digests/{gone}/") or name == f"trends/{gone}.json"]


def test_an_empty_library_builds(home):
    clock, build = home.batch(["clock", "set", "2026-09-10T08:00:00Z"], ["insights", "build"])
    assert build.status == 0, build.stderr
    assert build.lines == ["built 15 files: 0 papers, 0 topics, as of 2026-09-10"]
    stats = load(home.insights / "stats.json")
    assert sorted(stats["files"]) == sorted(FIXED_FILES)
    for name, entry in stats["files"].items():
        data = (home.insights / name).read_bytes()
        assert entry == {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}, name
    work = rows(home.insights / "runs.jsonl")[-1]["work"]
    for name in ("vocab.fits", "topics.fits", "citations.graphs", "trends.authors"):
        assert work[name] == 1, name


def test_only_day_week_and_month_of_the_clock_enter_a_result(tiny, labels, tmp_path):
    home = joined_copy(tiny, labels, tmp_path / "h")
    later_hours, next_day = tmp_path / "hours", tmp_path / "day"
    results = home.batch(["clock", "advance", "2h"], ["insights", "build", "--out", str(later_hours)],
                         ["clock", "advance", "1d"], ["insights", "build", "--out", str(next_day)])
    for result in results:
        assert result.status == 0, result.stderr

    assert differing(files_of(tiny.insights), files_of(later_hours)) == []

    stats = load(next_day / "stats.json")
    assert sorted(load(later_hours / "stats.json")["files"]) == sorted(stats["files"])
    running = [name for name in stats["files"] if name.startswith("digests/") and name.endswith(f"/{stats['as_of']['week']}.md")]
    assert running, "no page of the running week"
    changed = sorted(name for name in stats["files"]
                     if (later_hours / name).read_bytes() != (next_day / name).read_bytes())
    assert changed == sorted(running)
    assert (later_hours / "stats.json").read_bytes() != (next_day / "stats.json").read_bytes()


def test_show_commands_read_the_result_back(tiny, tmp_path, monkeypatch):
    home = tiny.copy(tmp_path / "h")
    folder = home.insights
    topics = load(folder / "topics.json")["topics"]
    feeds = load(folder / "stats.json")["inputs"]["feeds"]
    papers = by_id(rows(folder / "papers.jsonl"))
    assignments = by_id(rows(folder / "assignments.jsonl"))
    nodes = by_id(rows(folder / "citations" / "nodes.jsonl"))
    similar = by_id(rows(folder / "similar.jsonl"))
    picked = next(i for i, row in sorted(similar.items()) if row["similar"] and assignments[i]["topic"] is not None)

    empty = tmp_path / "empty"
    empty.mkdir()
    other = tmp_path / "other"
    shutil.copytree(folder, other)
    stats = load(other / "stats.json")
    stats["stages"]["similar"]["version"] = 2
    (other / "stats.json").write_bytes(json.dumps(stats, sort_keys=True, separators=(",", ":")).encode() + b"\n")

    commands = [["insights", "show", "topics"]]
    commands += [["insights", "show", "trends", feed] for feed in feeds]
    commands += [
        ["insights", "show", "trends", "q-fin.ST"],
        ["insights", "show", "paper", picked],
        ["insights", "show", "paper", "2608.99999"],
        ["insights", "config"],
        ["insights", "show", "topics", "--out", str(empty)],
        ["insights", "show", "trends", feeds[0], "--out", str(empty)],
        ["insights", "show", "paper", picked, "--out", str(empty)],
        ["insights", "show", "work", "--out", str(empty)],
        ["insights", "show", "topics", "--out", str(other)],
    ]
    results = home.batch(*commands)
    shown_topics, results = results[0], results[1:]
    shown_trends, results = results[:len(feeds)], results[len(feeds):]
    unknown_feed, shown_paper, unknown_paper, config, *refused = results

    assert shown_topics.status == 0
    assert shown_topics.lines == [f"{t['topic']}\t{t['size']}\t{t['recent']}\t{t['source']}\t{t['name']}" for t in topics]
    for feed, result in zip(feeds, shown_trends):
        data = load(folder / "trends" / f"{feed}.json")
        assert result.status == 0, feed
        assert result.lines == (
            [f"rising\t{r['term']}\t{r['recent']}\t{r['baseline']}\t{format(r['z'], '.2f')}" for r in data["rising"]]
            + [f"burst\t{b['term']}\t{b['count']}\t{b['before']}" for b in data["bursts"]]), feed
    assert (unknown_feed.status, unknown_feed.stdout) == (1, "")
    assert unknown_feed.stderr.splitlines() == ["error: no trends for q-fin.ST"]

    topic = assignments[picked]["topic"]
    paper = papers[picked]
    expected = [
        "arxiv_id".ljust(12) + picked,
        "state".ljust(12) + paper["state"],
        "feeds".ljust(12) + (" ".join(paper["feeds"]) or "-"),
        "topic".ljust(12) + f"{topic} {topics[topic]['name']}",
        "cited_by".ljust(12) + str(nodes[picked]["cited_by"]),
        "cites".ljust(12) + str(nodes[picked]["cites"]),
    ] + ["similar".ljust(12) + f"{entry['id']} {format(entry['score'], '.2f')}" for entry in similar[picked]["similar"]]
    assert shown_paper.status == 0 and shown_paper.lines == expected
    assert (unknown_paper.status, unknown_paper.stdout) == (1, "")
    assert unknown_paper.stderr.splitlines() == ["error: no such paper: 2608.99999"]

    monkeypatch.setenv("ARXIVLENS_HOME", str(home.path))
    from app.insights import config as settings

    assert config.status == 0 and config.stdout == settings.dump(settings.load())
    assert "  extra_stopwords: []" in config.lines and "  card_terms: 10" in config.lines

    messages = [f"error: no result in {empty}"] * 3 + [f"error: no run recorded in {empty}",
                                                       f"error: {other} was written by another version of stage similar"]
    for result, message in zip(refused, messages):
        assert (result.status, result.stdout) == (1, ""), message
        assert result.stderr.splitlines() == [message]


def test_several_builds_in_one_process(tiny, tmp_path):
    home = tiny.copy(tmp_path / "h")
    days = ["2026-08-15", "2026-08-16", "2026-08-17"]
    runs_before = len(rows(home.insights / "runs.jsonl"))
    result = home.run("play", "--corpus", str(SMALL), "--fast", "--from", days[0], "--until", days[-1],
                      "--then", "insights build")

    assert len(result.lines) == 2 * len(days)
    built = result.lines[1::2]
    for day, line in zip(days, result.lines[0::2]):
        assert line.startswith(f"{day} run "), line
    runs = rows(home.insights / "runs.jsonl")
    assert len(runs) == runs_before + len(days)
    for day, line, run in zip(days, built, runs[-len(days):]):
        assert run["as_of"] == day
        assert line.startswith("built ") and line.endswith(f", as of {day}"), line
        assert run["work"]["vocab.fits"] == 1 and run["work"]["topics.fits"] == 1, day
        assert f": {run['work']['snapshot.rows']} papers, " in line, day
    assert built[-1] == built_line(home.insights)


def test_the_snapshot_is_one_view_of_the_database(tiny, tmp_path):
    home = tiny.copy(tmp_path / "h")
    seen = json.loads(home.probe("snapshot_view").stdout)
    assert seen["inserted"], "the read ran no query on its own transaction"
    assert seen["same_as_before"] and not seen["seen_during"]
    assert seen["papers_after"] == seen["papers_before"] + 1
    assert seen["new_feeds"] and seen["new_analysis"] and seen["new_first"]
