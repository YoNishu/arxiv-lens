"""Neighbour lists: the probe test of the list owner, the floor, the order and the cut."""
import json
import math
from dataclasses import replace
from datetime import date
from types import SimpleNamespace

from app.insights import similar, vectors, work

# Four listed papers: 2608.90002 holds only one of the two heaviest terms of 2608.90001, while
# 2608.90001 holds both of 2608.90002's; nobody else holds the one term of 2608.90004.
EXAMPLE = {
    "2608.90001": [("a", 0.8), ("b", 0.5), ("c", 0.33166247903554)],
    "2608.90002": [("a", 0.6), ("c", 0.8)],
    "2608.90003": [("a", 0.7071067811865476), ("b", 0.7071067811865476)],
    "2608.90004": [("d", 1.0)],
}


def section(top_k=2, probe_terms=2, min_shared=2, min_sim=0.1):
    return SimpleNamespace(top_k=top_k, probe_terms=probe_terms, min_shared=min_shared, min_sim=min_sim)


def library(pairs_by_id):
    """vectors, weights and postings the way run builds them, listed ids ascending."""
    ids = sorted(pairs_by_id)
    weights = {i: dict(pairs_by_id[i]) for i in ids}
    postings = {}
    for i in ids:
        for term, _ in pairs_by_id[i]:
            postings.setdefault(term, []).append(i)
    return dict(pairs_by_id), weights, postings


def lists_of(pairs_by_id, settings):
    vecs, weights, postings = library(pairs_by_id)
    return {i: similar.search(i, vecs, weights, postings, settings) for i in sorted(vecs)}


def rounded(found):
    return [(i, round(s, 6)) for i, s in found]


def context(tmp_path, monkeypatch, papers, pairs_by_id, **keys):
    from app.insights import asof, config, pipeline

    monkeypatch.setenv("ARXIVLENS_HOME", str(tmp_path / "home"))
    settings = config.load()
    settings = replace(settings, similar=replace(settings.similar, **keys))
    ctx = pipeline.Context(asof.of(date(2026, 9, 10)), settings, tmp_path / "out")
    ctx.papers = papers
    ctx.vectors = pairs_by_id
    return ctx


def rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_lists_of_the_example():
    work.reset()
    lists = lists_of(EXAMPLE, section())
    assert {i: rounded(found) for i, found in lists.items()} == {
        "2608.90001": [("2608.90003", 0.919239)],
        "2608.90002": [("2608.90001", 0.74533)],
        "2608.90003": [("2608.90001", 0.919239)],
        "2608.90004": [],
    }
    counts = work.counts()
    assert counts["similar.searched"] == 4
    assert counts["similar.scored"] == 3


def test_the_candidate_test_is_the_owners():
    lists = lists_of(EXAMPLE, section())
    assert "2608.90001" in [i for i, _ in lists["2608.90002"]]
    assert "2608.90002" not in [i for i, _ in lists["2608.90001"]]
    one, two = EXAMPLE["2608.90001"], EXAMPLE["2608.90002"]
    assert vectors.dot(one, dict(two)) == vectors.dot(two, dict(one))
    assert vectors.dot(one, dict(two)) >= 0.1


def test_a_short_vector_needs_all_its_terms():
    pairs = {
        "2608.90101": [("d", 1.0)],
        "2608.90102": [("d", 0.6), ("e", 0.8)],
        "2608.90103": [("a", 0.6), ("b", 0.8)],
        "2608.90104": [("a", 0.8), ("e", 0.6)],
        "2608.90105": [("a", 0.6), ("b", 0.48), ("e", 0.64)],
        "2608.90106": [],
    }
    lists = lists_of(pairs, section(top_k=10, probe_terms=8, min_shared=3))
    assert rounded(lists["2608.90101"]) == [("2608.90102", 0.6)]
    # two terms against min_shared 3: both must be held, one is not enough
    assert [i for i, _ in lists["2608.90103"]] == ["2608.90105"]

    work.reset()
    vecs, weights, postings = library(pairs)
    assert similar.search("2608.90106", vecs, weights, postings, section(min_shared=3)) == []
    counts = work.counts()
    assert counts["similar.searched"] == 1
    assert counts["similar.scored"] == 0


def test_the_floor_is_inclusive():
    pairs = {
        "2608.90201": [("a", 0.6), ("b", 0.8)],
        "2608.90202": [("a", 0.28), ("b", 0.96)],
    }
    s = vectors.dot(pairs["2608.90201"], dict(pairs["2608.90202"]))
    at_floor = lists_of(pairs, section(min_sim=s))
    assert at_floor["2608.90201"] == [("2608.90202", s)]
    above = lists_of(pairs, section(min_sim=math.nextafter(s, 2.0)))
    assert above["2608.90201"] == []
    assert above["2608.90202"] == []


def test_the_list_is_cut_at_top_k():
    pairs = {
        "2608.90300": [("a", 1.0)],
        "2608.90301": [("a", 0.6), ("x", 0.8)],
        "2608.90302": [("a", 0.8), ("y", 0.6)],
        "2608.90303": [("a", 0.28), ("z", 0.96)],
    }
    keys = {"probe_terms": 1, "min_shared": 1}
    everyone = lists_of(pairs, section(top_k=3, **keys))["2608.90300"]
    assert [i for i, _ in everyone] == ["2608.90302", "2608.90301", "2608.90303"]
    cut = lists_of(pairs, section(top_k=2, **keys))["2608.90300"]
    assert cut == everyone[:2]
    assert cut[0][1] > cut[1][1]


def test_equal_scores_stand_in_id_order():
    pairs = {
        "2608.90400": [("a", 0.6), ("b", 0.8)],
        "2608.90402": [("a", 0.8), ("b", 0.6)],
        "2608.90401": [("a", 0.8), ("b", 0.6)],
    }
    found = lists_of(pairs, section())["2608.90400"]
    assert [i for i, _ in found] == ["2608.90401", "2608.90402"]
    assert found[0][1] == found[1][1]
    assert [i for i, _ in lists_of(pairs, section(top_k=1))["2608.90400"]] == ["2608.90401"]


def test_withdrawn_papers_have_no_list_and_are_in_none(tmp_path, monkeypatch):
    papers = [
        {"arxiv_id": "2608.90501", "withdrawn": False},
        {"arxiv_id": "2608.90502", "withdrawn": True},
        {"arxiv_id": "2608.90503", "withdrawn": False},
    ]
    pairs = {
        "2608.90501": [("a", 0.6), ("b", 0.8)],
        "2608.90502": [("a", 0.6), ("b", 0.8)],
        "2608.90503": [("a", 0.8), ("b", 0.6)],
    }
    ctx = context(tmp_path, monkeypatch, papers, pairs, top_k=10, probe_terms=2, min_shared=2, min_sim=0.1)
    work.reset()
    assert similar.run(ctx) == {"lists": 2, "entries": 2}
    found = rows(tmp_path / "out" / "similar.jsonl")
    assert [row["arxiv_id"] for row in found] == ["2608.90501", "2608.90503"]
    assert [[entry["id"] for entry in row["similar"]] for row in found] == [["2608.90503"], ["2608.90501"]]
    counts = work.counts()
    assert counts["similar.searched"] == 2
    # the withdrawn paper holds both probe terms of each owner; scored, it would have made this 4
    assert counts["similar.scored"] == 2


def test_the_owner_is_not_its_own_neighbour():
    pairs = {
        "2608.90601": [("a", 0.6), ("b", 0.8)],
        "2608.90602": [("a", 0.6), ("b", 0.8)],
    }
    lists = lists_of(pairs, section())
    assert [i for i, _ in lists["2608.90601"]] == ["2608.90602"]
    assert [i for i, _ in lists["2608.90602"]] == ["2608.90601"]
    alone = lists_of({"2608.90601": pairs["2608.90601"]}, section())
    assert alone == {"2608.90601": []}


def test_the_file_has_one_row_per_listed_paper(tmp_path, monkeypatch):
    pairs = dict(EXAMPLE)
    pairs["2608.90006"] = [("e", 1.0)]
    pairs["2608.90005"] = [("e", 1.0)]
    papers = [{"arxiv_id": i, "withdrawn": False} for i in sorted(pairs)]
    ctx = context(tmp_path, monkeypatch, papers, pairs, top_k=2, probe_terms=2, min_shared=2, min_sim=0.1)
    units = similar.run(ctx)
    assert units == {"lists": 6, "entries": 5}
    assert "similar.jsonl" in ctx.files
    path = tmp_path / "out" / "similar.jsonl"
    assert ctx.files["similar.jsonl"]["bytes"] == path.stat().st_size
    found = rows(path)
    assert [row["arxiv_id"] for row in found] == sorted(pairs)
    for row in found:
        assert sorted(row) == ["arxiv_id", "similar"]
        for entry in row["similar"]:
            assert sorted(entry) == ["id", "score"]
            assert isinstance(entry["score"], float)
    by_id = {row["arxiv_id"]: row["similar"] for row in found}
    assert by_id["2608.90004"] == []
    assert by_id["2608.90005"] == [{"id": "2608.90006", "score": 1.0}]
    assert b'"score":1.0}' in path.read_bytes()
