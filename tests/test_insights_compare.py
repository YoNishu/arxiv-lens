"""insights check and insights diff on result folders written by the test."""
import hashlib
import json
import shutil

STAGES = ("snapshot", "text", "vocab", "vectors", "similar", "citations", "topics", "trends", "ranking", "digests")
IDS = ("2608.90001", "2608.90002", "2608.90003")


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def jsonl(rows) -> bytes:
    return b"".join(canonical(row) + b"\n" for row in rows)


def sample_rows() -> dict[str, list[dict]]:
    """The row files of a small result, by path."""
    return {
        "papers.jsonl": [{"arxiv_id": i, "feeds": ["cs.LG"], "state": "analysed", "title": "Paper " + i} for i in IDS],
        "text/tokens.jsonl": [{"arxiv_id": i, "first_terms": {"graph": 1}, "terms": {"graph": 2}} for i in IDS],
        "text/refs.jsonl": [{"arxiv_id": i, "cites": [], "entries": 1, "other": 1, "text_sha256": None} for i in IDS[1:]],
        "vectors.jsonl": [{"arxiv_id": i, "vector": [["graph", 1.0]]} for i in IDS],
        "similar.jsonl": [{"arxiv_id": i, "similar": []} for i in IDS],
        "citations/edges.jsonl": [{"dst": "2608.90001", "src": "2608.90002"}, {"dst": "2608.90001", "src": "2608.90003"}],
        "citations/external.jsonl": [{"arxiv_id": "2203.91234", "cited_by": 2}],
        "citations/nodes.jsonl": [{"arxiv_id": i, "cited_by": 0, "cites": 0, "cocited": [], "coupled": [], "external": 0}
                                  for i in IDS],
        "citations/pagerank.jsonl": [{"arxiv_id": i, "component": IDS[0], "pagerank": 0.25} for i in IDS],
        "assignments.jsonl": [{"arxiv_id": i, "sim": 0.5, "topic": n % 2} for n, i in enumerate(IDS)],
        "ranking.jsonl": [{"feed": "cs.LG", "papers": [], "week": "2026-W35"},
                          {"feed": "cs.LG", "papers": [], "week": "2026-W36"}],
    }


def sample_topics() -> dict:
    return {"k": 2, "topics": [
        {"blurb": "", "exemplars": [], "name": "graph / node", "recent": 0, "size": 2, "source": "fallback",
         "terms": ["graph", "node"], "topic": 0},
        {"blurb": "", "exemplars": [], "name": "speech / unit", "recent": 0, "size": 1, "source": "fallback",
         "terms": ["speech", "unit"], "topic": 1},
    ]}


def sample_whole() -> dict[str, bytes]:
    """The files of a small result that are one unit each, by path."""
    trend = {"analysed": [1], "bursts": [], "papers": [1], "rising": [], "topics": [], "weeks": ["2026-W35"]}
    return {
        "vocab.json": canonical({"reference": {"month": "2026-09", "papers": 3, "whole_library": True},
                                 "terms": {"graph": {"df": 3, "idf": 1.0}}}) + b"\n",
        "centroids.json": canonical({"fitted": 3, "k": 2, "topics": []}) + b"\n",
        "trends/cs.LG.json": canonical(dict(trend, feed="cs.LG")) + b"\n",
        "trends/stat.ML.json": canonical(dict(trend, feed="stat.ML")) + b"\n",
        "authors.json": canonical({"active": [], "papers": [3], "weeks": ["2026-W35"], "with_new_author": [1]}) + b"\n",
        "digests/cs.LG/2026-W35.md": b"# cs.LG week 2026-W35\n",
        "digests/cs.LG/2026-W36.md": b"# cs.LG week 2026-W36\n",
    }


def contents(rows, topics, whole) -> dict[str, bytes]:
    files = {path: jsonl(r) for path, r in rows.items()}
    files["topics.json"] = canonical(topics) + b"\n"
    files.update(whole)
    return files


def write_result(folder, files: dict[str, bytes]) -> dict:
    """Write the files and a stats.json naming each with its size and hash; return the stats."""
    named = {}
    for path, data in files.items():
        target = folder.joinpath(*path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        named[path] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    stats = {"as_of": {"day": "2026-09-10", "month": "2026-09", "week": "2026-W37"}, "files": named,
             "inputs": {"feeds": ["cs.LG", "stat.ML"]}}
    (folder / "stats.json").write_bytes(canonical(stats) + b"\n")
    return stats


STAGE_OF = {
    "papers.jsonl": "snapshot", "text/tokens.jsonl": "text", "text/refs.jsonl": "text", "vocab.json": "vocab",
    "vectors.jsonl": "vectors", "similar.jsonl": "similar", "citations/edges.jsonl": "citations",
    "citations/external.jsonl": "citations", "citations/nodes.jsonl": "citations",
    "citations/pagerank.jsonl": "citations", "centroids.json": "topics", "assignments.jsonl": "topics",
    "authors.json": "trends", "ranking.jsonl": "ranking",
}


def stage_units(rows, topics, whole) -> dict[str, set]:
    """Recount of the units of each stage, as (path, key) pairs."""
    units = {stage: set() for stage in STAGES}
    for path, file_rows in rows.items():
        for row in file_rows:
            if path == "citations/edges.jsonl":
                key = row["src"] + " " + row["dst"]
            elif path == "ranking.jsonl":
                key = row["feed"] + " " + row["week"]
            else:
                key = row["arxiv_id"]
            units[STAGE_OF[path]].add((path, key))
    units["topics"] |= {("topics.json", entry["topic"]) for entry in topics["topics"]}
    for path in whole:
        # trends/<feed>.json and digests/<feed>/<week>.md take the stage of their folder.
        units[STAGE_OF.get(path) or path.split("/")[0]].add((path, None))
    return units


def stage_lines(a_units, b_units, differ: dict[str, int]) -> list[str]:
    return ["stage %s: %d of %d units differ" % (s, differ.get(s, 0), len(a_units[s] | b_units[s])) for s in STAGES]


def test_check_names_changed_and_missing_files(home, tmp_path):
    rows, topics, whole = sample_rows(), sample_topics(), sample_whole()
    files = contents(rows, topics, whole)
    good = tmp_path / "good"
    stats = write_result(good, files)
    (good / "runs.jsonl").write_bytes(b'{"kind":"build"}\n')

    bad = tmp_path / "bad"
    shutil.copytree(good, bad)
    papers = (bad / "papers.jsonl").read_bytes()
    # The same size with one byte changed, a file that grew, and a file that is gone.
    (bad / "papers.jsonl").write_bytes(papers.replace(b"Paper 2608.90002", b"Paper 2608.90009"))
    assert len((bad / "papers.jsonl").read_bytes()) == len(papers)
    (bad / "authors.json").write_bytes(whole["authors.json"] + b"\n")
    (bad / "vocab.json").unlink()

    none = tmp_path / "none"
    shutil.copytree(good, none)
    (none / "stats.json").unlink()

    first, second, third = home.batch(
        ["insights", "check", "--out", str(good)],
        ["insights", "check", "--out", str(bad)],
        ["insights", "check", "--out", str(none)],
    )
    n = len(stats["files"])
    assert first.status == 0, first.stderr
    assert first.lines == ["checked %d files: 0 changed, 0 missing, 0 unknown" % n]
    assert second.status == 1, second.stderr
    assert second.lines == [
        "changed\tauthors.json",
        "changed\tpapers.jsonl",
        "missing\tvocab.json",
        "checked %d files: 2 changed, 1 missing, 0 unknown" % n,
    ]
    assert third.status == 1
    assert third.stdout == ""
    assert third.stderr.strip() == "error: no result in %s" % none


def test_check_lists_a_file_the_stats_do_not_name_and_still_passes(home, tmp_path):
    folder = tmp_path / "result"
    stats = write_result(folder, contents(sample_rows(), sample_topics(), sample_whole()))
    (folder / "runs.jsonl").write_bytes(b'{"kind":"build"}\n')
    (folder / "notes.txt").write_bytes(b"a note left by hand\n")
    (folder / "papers.jsonl.tmp").write_bytes(b'{"arxiv_id":"2608.90001"')
    (folder / "trends" / "runs.jsonl").write_bytes(b"")

    result = home.run("insights", "check", "--out", str(folder), check=False)
    assert result.status == 0, result.stderr
    assert result.lines == [
        "unknown\tnotes.txt",
        "unknown\tpapers.jsonl.tmp",
        "unknown\ttrends/runs.jsonl",
        "checked %d files: 0 changed, 0 missing, 3 unknown" % len(stats["files"]),
    ]


def test_diff_of_two_equal_folders_ignores_runs_jsonl(home, tmp_path):
    rows, topics, whole = sample_rows(), sample_topics(), sample_whole()
    a = tmp_path / "a"
    write_result(a, contents(rows, topics, whole))
    b = tmp_path / "b"
    shutil.copytree(a, b)
    (a / "runs.jsonl").write_bytes(b'{"kind":"build","seconds":{"total":1.5}}\n')
    (b / "runs.jsonl").write_bytes(b'{"kind":"build","seconds":{"total":2.5}}\n' * 2)

    result = home.run("insights", "diff", str(a), str(b), check=False)
    units = stage_units(rows, topics, whole)
    assert result.status == 0, result.stderr
    assert result.lines == stage_lines(units, units, {}) + ["0 files differ"]


def test_diff_names_files_first_units_and_stages(home, tmp_path):
    rows, topics, whole = sample_rows(), sample_topics(), sample_whole()
    a = tmp_path / "a"
    write_result(a, contents(rows, topics, whole))
    (a / "runs.jsonl").write_bytes(b'{"kind":"build"}\n')

    b_rows, b_topics, b_whole = sample_rows(), sample_topics(), sample_whole()
    b_rows["papers.jsonl"][1]["title"] = "Paper 2608.90002, revised"
    b_rows["papers.jsonl"][2]["state"] = "failed"
    b_rows["citations/edges.jsonl"].append({"dst": "2608.90002", "src": "2608.90003"})
    b_topics["topics"][1]["name"] = "Speech and Units"
    del b_whole["digests/cs.LG/2026-W36.md"]
    b = tmp_path / "b"
    write_result(b, contents(b_rows, b_topics, b_whole))
    (b / "notes.txt").write_bytes(b"a note\n")

    # More files differ than are listed one by one.
    c = tmp_path / "c"
    shutil.copytree(a, c)
    (c / "notes").mkdir()
    for n in range(23):
        (c / "notes" / ("n%02d.txt" % n)).write_bytes(b"x\n")

    against_b, against_c, not_a_folder = home.batch(
        ["insights", "diff", str(a), str(b)],
        ["insights", "diff", str(a), str(c)],
        ["insights", "diff", str(a), str(a / "papers.jsonl")],
    )

    a_units = stage_units(rows, topics, whole)
    b_units = stage_units(b_rows, b_topics, b_whole)
    assert against_b.status == 1, against_b.stderr
    assert against_b.lines == [
        "differ\tcitations/edges.jsonl\t2608.90003 2608.90002",
        "only-a\tdigests/cs.LG/2026-W36.md",
        "only-b\tnotes.txt",
        "differ\tpapers.jsonl\t2608.90002",
        "differ\tstats.json",
        "differ\ttopics.json\ttopic 1",
    ] + stage_lines(a_units, b_units, {"snapshot": 2, "citations": 1, "topics": 1, "digests": 1}) + ["6 files differ"]

    assert against_c.status == 1, against_c.stderr
    assert against_c.lines == (
        ["only-b\tnotes/n%02d.txt" % n for n in range(20)]
        + ["... 3 more"]
        + stage_lines(a_units, a_units, {})
        + ["23 files differ"]
    )

    assert not_a_folder.status == 2
    assert not_a_folder.stdout == ""
    assert not_a_folder.stderr.strip() == "error: not a folder: %s" % (a / "papers.jsonl")
