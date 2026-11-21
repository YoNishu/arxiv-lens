"""The read side of a result folder that the surfaces and two tools use (app/insights/store.py)."""
import json
import os

import pytest

from app.insights import store


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def write_json(path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical(value) + b"\n")


def write_jsonl(path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(canonical(row) + b"\n" for row in rows))


def paper_row(arxiv_id, title, authors, feeds, state="analysed", withdrawn=False):
    return {
        "abstract": "An abstract.", "announced": "2026-08-25", "arxiv_id": arxiv_id, "authors": authors,
        "categories": feeds, "feeds": feeds, "first_abstract": "An abstract.", "first_title": title,
        "keywords": None, "novelty": 7, "published": "2026-08-25T08:00:00Z", "state": state,
        "text_chars": None, "text_sha256": None, "title": title, "updated": None, "version": 1,
        "withdrawn": withdrawn,
    }


STATS = {
    "as_of": {"day": "2026-09-10", "month": "2026-09", "week": "2026-W37"},
    "files": {},
    "inputs": {"analysed": 3, "failed": 0, "feeds": ["cs.LG", "stat.ML"], "papers": 4, "pending": 1, "withdrawn": 0},
    "settings": "0" * 64,
    "stages": {},
}

PAPERS = [
    paper_row("2608.90001", "Sparse Routing for Graph Embeddings", ["K. Maren", "T. Soval"], ["cs.LG"]),
    paper_row("2608.90002", "Gated Mixtures at Scale", ["R. Ilden"], ["cs.LG", "stat.ML"]),
    paper_row("2608.90003", "A Note on Routing", ["A. Dorun"], ["cs.LG"], state="pending"),
    paper_row("2608.90004", "Speech Units from Silence", ["M. Tovar", "K. Maren"], ["cs.LG"]),
]

SIMILAR = [
    {"arxiv_id": "2608.90001", "similar": [{"id": "2608.90002", "score": 0.75}, {"id": "2608.90004", "score": 0.5}]},
    {"arxiv_id": "2608.90002", "similar": [{"id": "2608.90001", "score": 0.75}]},
    {"arxiv_id": "2608.90004", "similar": []},
]

EDGES = [
    {"src": "2608.90002", "dst": "2608.90001"},
    {"src": "2608.90004", "dst": "2608.90001"},
    {"src": "2608.90004", "dst": "2608.90002"},
]


def node(arxiv_id, cited_by, cites, external):
    return {"arxiv_id": arxiv_id, "cited_by": cited_by, "cites": cites, "cocited": [], "coupled": [], "external": external}


NODES = [node("2608.90001", 2, 0, 1), node("2608.90002", 1, 1, 0), node("2608.90003", 0, 0, 0), node("2608.90004", 0, 2, 3)]

ASSIGNMENTS = [
    {"arxiv_id": "2608.90001", "sim": 0.8, "topic": 0},
    {"arxiv_id": "2608.90002", "sim": 0.95, "topic": 0},
    {"arxiv_id": "2608.90004", "sim": 0.6, "topic": 1},
]

TOPICS = {"k": 2, "topics": [
    {"blurb": "Graphs.", "exemplars": [{"arxiv_id": "2608.90002", "title": "Gated Mixtures at Scale"}],
     "name": "Graph and Embed", "recent": 1, "size": 2, "source": "model", "terms": ["graph", "embed"], "topic": 0},
    {"blurb": "", "exemplars": [{"arxiv_id": "2608.90004", "title": "Speech Units from Silence"}],
     "name": "speech / unit", "recent": 0, "size": 1, "source": "fallback", "terms": ["speech", "unit"], "topic": 1},
]}

TRENDS = {
    "analysed": [2, 1], "bursts": [], "feed": "cs.LG", "papers": [2, 1],
    "rising": [{"baseline": 0, "recent": 2, "term": "graph embedding", "z": 1.25}],
    "topics": [{"counts": [0, 0, 0, 0, 2, 0], "slope": [3, 35], "topic": 0},
               {"counts": [0, 0, 0, 0, 0, 1], "slope": [5, 35], "topic": 1}],
    "weeks": ["2026-W35", "2026-W36"],
}


def ranked(arxiv_id, rank, score):
    return {"arxiv_id": arxiv_id, "grounding": 1, "heat": 2, "novelty": 7, "percentile": 100 - 50 * (rank - 1),
            "rank": rank, "score": score}


RANKING = [
    {"feed": "cs.LG", "papers": [ranked("2608.90002", 1, 0.6), ranked("2608.90001", 2, 0.5)], "week": "2026-W35"},
    {"feed": "cs.LG", "papers": [ranked("2608.90004", 1, 0.4)], "week": "2026-W37"},
    {"feed": "stat.ML", "papers": [ranked("2608.90002", 1, 0.6)], "week": "2026-W35"},
]

PAGE = "# cs.LG week 2026-W35\n\n2026-08-24 to 2026-08-30, closed, 2 ranked\n"


def write_folder(folder) -> None:
    write_json(folder / "stats.json", STATS)
    write_jsonl(folder / "papers.jsonl", PAPERS)
    write_jsonl(folder / "similar.jsonl", SIMILAR)
    write_jsonl(folder / "citations" / "edges.jsonl", EDGES)
    write_jsonl(folder / "citations" / "nodes.jsonl", NODES)
    write_jsonl(folder / "assignments.jsonl", ASSIGNMENTS)
    write_json(folder / "topics.json", TOPICS)
    write_json(folder / "trends" / "cs.LG.json", TRENDS)
    write_jsonl(folder / "ranking.jsonl", RANKING)
    (folder / "digests" / "cs.LG").mkdir(parents=True)
    (folder / "digests" / "cs.LG" / "2026-W35.md").write_bytes(PAGE.encode("utf-8"))


def test_lookups_follow_the_files_on_disk(tmp_path):
    folder = tmp_path / "insights"
    write_folder(folder)
    title = {row["arxiv_id"]: row["title"] for row in PAPERS}

    assert store.stats(folder) == STATS
    assert store.paper("2608.90002", folder) == PAPERS[1]
    assert store.topics(folder) == TOPICS["topics"]

    want_topic = dict(TOPICS["topics"][0])
    want_topic["members"] = [
        {"arxiv_id": "2608.90002", "title": title["2608.90002"], "sim": 0.95},
        {"arxiv_id": "2608.90001", "title": title["2608.90001"], "sim": 0.8},
    ]
    assert store.topic(0, folder) == want_topic
    assert [m["arxiv_id"] for m in store.topic(1, folder)["members"]] == ["2608.90004"]

    trend = store.trends("cs.LG", folder)
    assert [t["name"] for t in trend["topics"]] == ["Graph and Embed", "speech / unit"]
    assert {k: v for k, v in trend.items() if k != "topics"} == {k: v for k, v in TRENDS.items() if k != "topics"}
    assert [{k: v for k, v in t.items() if k != "name"} for t in trend["topics"]] == TRENDS["topics"]

    assert store.weeks("cs.LG", folder) == ["2026-W35", "2026-W37"]
    assert store.weeks("stat.ML", folder) == ["2026-W35"]
    closed = store.digest("cs.LG", "2026-W35", folder)
    assert closed == {
        "feed": "cs.LG", "week": "2026-W35", "closed": True, "ranked": 2, "page": PAGE,
        "papers": [
            dict(RANKING[0]["papers"][0], title=title["2608.90002"], authors=["R. Ilden"], topic="Graph and Embed"),
            dict(RANKING[0]["papers"][1], title=title["2608.90001"], authors=["K. Maren", "T. Soval"],
                 topic="Graph and Embed"),
        ],
    }
    running = store.digest("cs.LG", "2026-W37", folder)
    assert (running["closed"], running["ranked"], running["page"]) == (False, 1, "")
    assert running["papers"][0]["topic"] == "speech / unit"

    assert store.related("2608.90001", folder) == {
        "arxiv_id": "2608.90001", "title": title["2608.90001"], "state": "analysed", "withdrawn": False,
        "feeds": ["cs.LG"], "topic": {"topic": 0, "name": "Graph and Embed"},
        "similar": [{"arxiv_id": "2608.90002", "title": title["2608.90002"], "score": 0.75},
                    {"arxiv_id": "2608.90004", "title": title["2608.90004"], "score": 0.5}],
        "cites": [],
        "cited_by": [{"arxiv_id": "2608.90002", "title": title["2608.90002"]},
                     {"arxiv_id": "2608.90004", "title": title["2608.90004"]}],
        "cited_by_count": 2, "external": 1,
    }
    pending = store.related("2608.90003", folder)
    assert (pending["state"], pending["topic"], pending["similar"], pending["cites"], pending["cited_by"]) == (
        "pending", None, [], [], [])
    assert (pending["cited_by_count"], pending["external"]) == (0, 0)
    assert [c["arxiv_id"] for c in store.related("2608.90004", folder)["cites"]] == ["2608.90001", "2608.90002"]

    # A rewritten file is read again: a longer title, and one more edge.
    renamed = [dict(row) for row in PAPERS]
    renamed[0]["title"] = "Sparse Routing for Graph Embeddings, Revised"
    write_jsonl(folder / "papers.jsonl", renamed)
    write_jsonl(folder / "citations" / "edges.jsonl", EDGES + [{"src": "2608.90003", "dst": "2608.90001"}])
    assert store.paper("2608.90001", folder)["title"] == renamed[0]["title"]
    seen = store.related("2608.90001", folder)
    assert seen["title"] == renamed[0]["title"]
    assert [c["arxiv_id"] for c in seen["cited_by"]] == ["2608.90002", "2608.90003", "2608.90004"]
    assert store.related("2608.90002", folder)["similar"][0]["title"] == renamed[0]["title"]

    # The same size and time as before, rows in another order: the stale index is made again.
    path = folder / "papers.jsonl"
    before = os.stat(path)
    write_jsonl(path, list(reversed(renamed)))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert os.stat(path).st_size == before.st_size
    for row in renamed:
        assert store.paper(row["arxiv_id"], folder) == row, row["arxiv_id"]


def test_a_folder_without_stats_has_no_result(tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    calls = [
        ("stats", lambda: store.stats(empty)),
        ("paper", lambda: store.paper("2608.90001", empty)),
        ("topics", lambda: store.topics(empty)),
        ("topic", lambda: store.topic(0, empty)),
        ("trends", lambda: store.trends("cs.LG", empty)),
        ("related", lambda: store.related("2608.90001", empty)),
        ("weeks", lambda: store.weeks("cs.LG", empty)),
        ("digest", lambda: store.digest("cs.LG", "2026-W35", empty)),
    ]
    for name, call in calls:
        with pytest.raises(store.NoResult) as caught:
            call()
        assert str(caught.value) == "no result in %s" % empty, name

    # Without a folder the home's insights folder is read, at every call.
    monkeypatch.setenv("ARXIVLENS_HOME", str(tmp_path / "home"))
    folder = tmp_path / "home" / "insights"
    with pytest.raises(store.NoResult):
        store.stats()
    write_json(folder / "stats.json", STATS)
    assert store.stats() == STATS
    assert (store.paper("2608.90001"), store.topics(), store.topic(0), store.trends("cs.LG")) == (None, [], None, None)
    assert (store.related("2608.90001"), store.weeks("cs.LG"), store.digest("cs.LG", "2026-W35")) == (None, [], None)

    write_jsonl(folder / "papers.jsonl", PAPERS)
    write_jsonl(folder / "assignments.jsonl", ASSIGNMENTS)
    write_json(folder / "topics.json", TOPICS)
    write_jsonl(folder / "ranking.jsonl", RANKING)
    assert store.paper("2608.99999") is None
    assert store.related("2608.99999") is None
    assert store.topic(7) is None
    assert store.weeks("cs.CV") == []
    assert store.digest("cs.LG", "2026-W36") is None
    assert store.digest("cs.CV", "2026-W35") is None
    assert store.trends("cs.CV") is None

    # A trends file or a page the result does not name is never opened.
    write_json(folder / "trends" / "cs.CV.json", dict(TRENDS, feed="cs.CV"))
    assert store.trends("cs.CV") is None
    assert store.trends("../stats") is None
    (folder / "digests" / "cs.LG").mkdir(parents=True)
    (folder / "digests" / "cs.LG" / "2026-W30.md").write_bytes(PAGE.encode("utf-8"))
    assert store.digest("cs.LG", "2026-W30") is None
    assert store.digest("cs.LG", "2026-W35")["page"] == ""

    (folder / "stats.json").unlink()
    with pytest.raises(store.NoResult):
        store.paper("2608.90001")
