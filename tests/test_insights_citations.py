"""Stage 6 of the insights build: edges, external ids, node lists, connected parts and PageRank."""
import dataclasses
import json
import math
import random
from datetime import date

import pytest

from app.insights import asof, citations, config, pipeline, work

A = "2608.90001"
B = "2608.90002"
C = "2608.90003"
D = "2608.90004"
E = "2608.90005"
F = "2608.90006"
G = "2608.90007"

# The library of the worked example: three papers cite inside and outside it.
EXAMPLE_CITES = {
    C: [A, B, "2203.91234"],
    D: [A, B, "2203.91234", "cs/9812901"],
    E: [A, C, "2203.91234", "cs/9812901"],
}

EXAMPLE_NODES = [
    {"arxiv_id": A, "cited_by": 3, "cites": 0, "cocited": [{"count": 2, "id": B}], "coupled": [], "external": 0},
    {"arxiv_id": B, "cited_by": 2, "cites": 0, "cocited": [{"count": 2, "id": A}], "coupled": [], "external": 0},
    {
        "arxiv_id": C,
        "cited_by": 1,
        "cites": 2,
        "cocited": [],
        "coupled": [{"id": D, "shared": 3, "union": 4}, {"id": E, "shared": 2, "union": 5}],
        "external": 1,
    },
    {
        "arxiv_id": D,
        "cited_by": 0,
        "cites": 2,
        "cocited": [],
        "coupled": [{"id": C, "shared": 3, "union": 4}, {"id": E, "shared": 3, "union": 5}],
        "external": 2,
    },
    {
        "arxiv_id": E,
        "cited_by": 0,
        "cites": 2,
        "cocited": [],
        "coupled": [{"id": D, "shared": 3, "union": 5}, {"id": C, "shared": 2, "union": 5}],
        "external": 2,
    },
    {"arxiv_id": F, "cited_by": 0, "cites": 0, "cocited": [], "coupled": [], "external": 0},
]


def paper(arxiv_id: str, state: str = "analysed", withdrawn: bool = False) -> dict:
    return {"arxiv_id": arxiv_id, "state": state, "withdrawn": withdrawn, "feeds": ["cs.LG"], "announced": "2026-08-24"}


def refs_row(arxiv_id: str, cites: list[str]) -> dict:
    return {"arxiv_id": arxiv_id, "text_sha256": None, "entries": len(cites), "cites": list(cites), "other": 0}


def example_library() -> tuple[list[dict], dict[str, dict]]:
    papers = [paper(A), paper(B, state="pending"), paper(C), paper(D), paper(E), paper(F)]
    refs = {A: refs_row(A, []), F: refs_row(F, [])}
    for source, listed in EXAMPLE_CITES.items():
        refs[source] = refs_row(source, listed)
    return papers, refs


@pytest.fixture
def make_ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("ARXIVLENS_HOME", str(tmp_path / "home"))
    settings = config.load()
    settings = dataclasses.replace(
        settings, citations=dataclasses.replace(settings.citations, damping=0.85, list_size=5)
    )
    made = []

    def make(papers: list[dict], refs: dict[str, dict]):
        ctx = pipeline.Context(asof.of(date(2026, 9, 10)), settings, tmp_path / f"out{len(made)}")
        ctx.papers = sorted(papers, key=lambda row: row["arxiv_id"])
        ctx.refs = refs
        made.append(ctx)
        return ctx

    return make


def rows(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def indexes(raw: dict[str, list[str]], stored: set[str]):
    """The four indexes of a hand-built library, built the way the stage builds them."""
    cites, citers, raw_citers = {}, {}, {}
    for source in sorted(raw):
        cites[source] = [c for c in raw[source] if c in stored]
        for cited in cites[source]:
            citers.setdefault(cited, []).append(source)
        for cited in raw[source]:
            raw_citers.setdefault(cited, []).append(source)
    return cites, citers, raw, raw_citers


def citers_of(cites: dict[str, list[str]]) -> dict[str, list[str]]:
    citers = {}
    for source in sorted(cites):
        for cited in cites[source]:
            citers.setdefault(cited, []).append(source)
    return citers


def test_resolve_keeps_list_order(make_ctx):
    work.reset()
    stored = {E, A, B}
    inside, outside = citations.resolve([E, "2203.91234", A, "cs/9812901", B], stored)
    assert inside == [E, A, B]
    assert outside == ["2203.91234", "cs/9812901"]
    assert citations.resolve([], stored) == ([], [])
    assert work.counts()["citations.resolved"] == 2

    # A stored paper is a target whatever its state, withdrawn included.
    papers = [paper(A), paper(B, state="pending"), paper(C, state="failed"), paper(D, withdrawn=True)]
    refs = {A: refs_row(A, [D, B, C]), D: refs_row(D, [])}
    ctx = make_ctx(papers, refs)
    citations.run(ctx)
    assert rows(ctx.out / "citations" / "edges.jsonl") == [
        {"src": A, "dst": B},
        {"src": A, "dst": C},
        {"src": A, "dst": D},
    ]
    assert ctx.cites == {A: [D, B, C]}
    assert ctx.citers == {B: [A], C: [A], D: [A]}


def test_only_sources_cite(make_ctx):
    work.reset()
    papers = [paper(A, state="pending"), paper(B), paper(C, withdrawn=True), paper(D, state="failed")]
    refs = {
        A: refs_row(A, [B, "2203.91234"]),
        B: refs_row(B, []),
        C: refs_row(C, [B, "cs/9812901"]),
        D: refs_row(D, [B, "2203.91234"]),
    }
    ctx = make_ctx(papers, refs)
    units = citations.run(ctx)
    out = ctx.out / "citations"
    assert (out / "edges.jsonl").read_bytes() == b""
    assert (out / "external.jsonl").read_bytes() == b""
    assert [row["cited_by"] for row in rows(out / "nodes.jsonl")] == [0, 0, 0, 0]
    assert [row["external"] for row in rows(out / "nodes.jsonl")] == [0, 0, 0, 0]
    assert ctx.cites == {B: []}
    assert ctx.citers == {}
    assert units["sources"] == 1
    assert units["edges"] == 0
    assert units["external"] == 0
    assert work.counts()["citations.resolved"] == 1


def test_edges_and_external_ids_of_the_example(make_ctx):
    work.reset()
    papers, refs = example_library()
    ctx = make_ctx(papers, refs)
    units = citations.run(ctx)
    out = ctx.out / "citations"
    assert rows(out / "edges.jsonl") == [
        {"src": C, "dst": A},
        {"src": C, "dst": B},
        {"src": D, "dst": A},
        {"src": D, "dst": B},
        {"src": E, "dst": A},
        {"src": E, "dst": C},
    ]
    assert rows(out / "external.jsonl") == [
        {"arxiv_id": "2203.91234", "cited_by": 3},
        {"arxiv_id": "cs/9812901", "cited_by": 2},
    ]
    assert units == {"sources": 5, "edges": 6, "external": 2, "nodes": 6, "components": 2}
    assert ctx.cites == {A: [], C: [A, B], D: [A, B], E: [A, C], F: []}
    assert ctx.citers == {A: [C, D, E], B: [C, D], C: [E]}
    assert sorted(ctx.files) == [
        "citations/edges.jsonl",
        "citations/external.jsonl",
        "citations/nodes.jsonl",
        "citations/pagerank.jsonl",
    ]
    counts = work.counts()
    assert counts["citations.resolved"] == 5
    assert counts["citations.lists"] == 6
    assert counts["citations.graphs"] == 1


def test_node_rows_of_the_example(make_ctx):
    papers, refs = example_library()
    ctx = make_ctx(papers, refs)
    citations.run(ctx)
    found = rows(ctx.out / "citations" / "nodes.jsonl")
    assert len(found) == len(EXAMPLE_NODES)
    for got, expected in zip(found, EXAMPLE_NODES):
        assert got == expected, expected["arxiv_id"]
        assert sorted(got) == ["arxiv_id", "cited_by", "cites", "cocited", "coupled", "external"]


def test_co_citation_floor_and_order():
    work.reset()
    target, x, y, z, w = A, "2608.90009", B, D, G
    s1, s2, s3, s4 = "2608.90011", "2608.90012", "2608.90013", "2608.90014"
    raw = {s1: [target, x, y, z], s2: [target, y, x], s3: [target, z, w], s4: [target, z, x]}
    stored = {target, x, y, z, w, s1, s2, s3, s4}
    cites, citers, raw, raw_citers = indexes(raw, stored)

    # x and z are cited together with the target three times, y twice, w once (below the floor).
    row = citations.node(target, cites, citers, raw, raw_citers, 5)
    assert row["cocited"] == [{"id": z, "count": 3}, {"id": x, "count": 3}, {"id": y, "count": 2}]
    assert row["cited_by"] == 4
    assert row["coupled"] == []
    assert citations.node(target, cites, citers, raw, raw_citers, 2)["cocited"] == [
        {"id": z, "count": 3},
        {"id": x, "count": 3},
    ]
    assert citations.node(target, cites, citers, raw, raw_citers, 1)["cocited"] == [{"id": z, "count": 3}]
    # A pair cited together once only is left out.
    assert citations.node(w, cites, citers, raw, raw_citers, 5)["cocited"] == []
    assert work.counts()["citations.lists"] == 4


def test_coupling_order():
    owner = "2608.90010"
    e1, e2, e3, e4, e5, e6, e7 = (f"2608.9010{i}" for i in range(1, 8))
    x1 = "2203.91234"
    q1, q2, q3, q4, q5 = "2608.90015", "2608.90012", "2608.90013", "2608.90014", "2608.90011"
    raw = {
        owner: [e1, e2, e3, x1],
        q1: [e1, e2, e3],
        q2: [e1, e2, e3, x1, e4, e5],
        q3: [e1, e2, x1],
        q4: [e1, e2, e3, e6, e7],
        q5: [e1],
    }
    stored = {owner, q1, q2, q3, q4, q5, e1, e2, e3, e4, e5, e6, e7}
    cites, citers, raw, raw_citers = indexes(raw, stored)

    row = citations.node(owner, cites, citers, raw, raw_citers, 5)
    # More shared ids first; equal shared ids: the smaller union first; equal both: the id. The
    # external id x1 counts as a shared reference (q3 shares three ids with the owner, not two).
    assert row["coupled"] == [
        {"id": q2, "shared": 4, "union": 6},
        {"id": q3, "shared": 3, "union": 4},
        {"id": q1, "shared": 3, "union": 4},
        {"id": q4, "shared": 3, "union": 6},
    ]
    assert row["external"] == 1
    assert citations.node(owner, cites, citers, raw, raw_citers, 2)["coupled"] == row["coupled"][:2]
    # A paper that is not a source has no coupling list, however many of its citers share references.
    assert citations.node(e1, cites, citers, raw, raw_citers, 5)["coupled"] == []


def test_an_external_id_becomes_an_edge_when_its_paper_is_stored(make_ctx):
    papers, refs = example_library()
    before = make_ctx(papers, refs)
    citations.run(before)
    late = "2203.91234"
    after = make_ctx(papers + [paper(late, state="pending")], refs)
    units = citations.run(after)

    old_external = rows(before.out / "citations" / "external.jsonl")
    new_external = rows(after.out / "citations" / "external.jsonl")
    assert {"arxiv_id": late, "cited_by": 3} in old_external
    assert new_external == [{"arxiv_id": "cs/9812901", "cited_by": 2}]
    new_edges = rows(after.out / "citations" / "edges.jsonl")
    for source in (C, D, E):
        assert {"src": source, "dst": late} in new_edges
    assert len(new_edges) == 9
    assert units["external"] == 1

    old_nodes = {row["arxiv_id"]: row for row in rows(before.out / "citations" / "nodes.jsonl")}
    new_nodes = {row["arxiv_id"]: row for row in rows(after.out / "citations" / "nodes.jsonl")}
    assert late not in old_nodes
    assert new_nodes[late]["cited_by"] == 3
    assert (old_nodes[C]["cites"], old_nodes[C]["external"]) == (2, 1)
    assert (new_nodes[C]["cites"], new_nodes[C]["external"]) == (3, 0)
    assert after.citers[late] == [C, D, E]


def test_connected_parts_are_named_by_their_smallest_id():
    # G cites B and D cites G: one part reached against and along the direction of the edges.
    cites = {G: [B], D: [G], C: [E]}
    citers = citers_of(cites)
    ids = [A, B, C, D, E, F, G]
    result = citations.graph_pass(ids, cites, citers, 0.85)
    assert list(result) == ids
    assert {p: result[p][0] for p in ids} == {A: A, B: B, C: C, D: B, E: C, F: F, G: B}


def test_pagerank_on_small_graphs():
    cases = [
        ("a cites b", [A, B], {A: [B]}, {A: 0.350877, B: 0.649123}),
        ("a cites b, b cites c", [A, B, C], {A: [B], B: [C]}, {A: 0.184417, B: 0.341171, C: 0.474412}),
        ("b, c, d cite a", [A, B, C, D], {B: [A], C: [A], D: [A]}, {A: 0.541985, B: 0.152672, C: 0.152672, D: 0.152672}),
        (
            "a and b cite each other, c and d alone",
            [A, B, C, D],
            {A: [B], B: [A]},
            {A: 0.434783, B: 0.434783, C: 0.065217, D: 0.065217},
        ),
        ("one paper", [A], {}, {A: 1.0}),
    ]
    for name, ids, cites, expected in cases:
        result = citations.graph_pass(ids, cites, citers_of(cites), 0.85)
        assert {p: round(result[p][1], 6) for p in ids} == expected, name
    parts = citations.graph_pass([A, B, C, D], {A: [B], B: [A]}, {A: [B], B: [A]}, 0.85)
    assert sorted({part for part, _rank in parts.values()}) == [A, C, D]


def test_pagerank_adds_up_to_one(make_ctx):
    papers, refs = example_library()
    ctx = make_ctx(papers, refs)
    citations.run(ctx)
    ranks = rows(ctx.out / "citations" / "pagerank.jsonl")
    assert [row["arxiv_id"] for row in ranks] == [A, B, C, D, E, F]
    assert [row["component"] for row in ranks] == [A, A, A, A, A, F]
    assert [round(row["pagerank"], 6) for row in ranks] == [0.275565, 0.227872, 0.15991, 0.112218, 0.112218, 0.112218]
    assert all(isinstance(row["pagerank"], float) for row in ranks)
    assert abs(math.fsum(row["pagerank"] for row in ranks) - 1.0) < 1e-9

    chance = random.Random(42)
    ids = [f"2608.{90000 + i}" for i in range(200)]
    cites = {}
    for p in ids:
        if chance.random() < 0.3:
            # About a third of the papers cite nothing and spread their rank over everybody.
            continue
        cites[p] = chance.sample([q for q in ids if q != p], chance.randint(1, 6))
    result = citations.graph_pass(ids, cites, citers_of(cites), 0.85)
    assert abs(math.fsum(rank for _part, rank in result.values()) - 1.0) < 1e-9

    work.reset()
    assert citations.graph_pass([], {}, {}, 0.85) == {}
    assert work.counts()["citations.graphs"] == 1


def test_a_paper_cited_only_by_a_withdrawn_paper_is_not_cited(make_ctx):
    # D is withdrawn; its list names A, B and C. Only E counts, and E names A and B once together.
    papers = [paper(A, state="pending"), paper(B, state="pending"), paper(C, state="pending"), paper(D, withdrawn=True), paper(E)]
    refs = {D: refs_row(D, [A, B, C]), E: refs_row(E, [A, B])}
    ctx = make_ctx(papers, refs)
    citations.run(ctx)
    nodes = {row["arxiv_id"]: row for row in rows(ctx.out / "citations" / "nodes.jsonl")}
    assert nodes[C]["cited_by"] == 0
    assert nodes[C]["cocited"] == []
    assert nodes[A]["cited_by"] == 1
    assert nodes[A]["cocited"] == []
    assert nodes[B]["cocited"] == []
    assert (nodes[D]["cites"], nodes[D]["external"], nodes[D]["coupled"]) == (0, 0, [])
    assert rows(ctx.out / "citations" / "edges.jsonl") == [{"src": E, "dst": A}, {"src": E, "dst": B}]
    assert C not in ctx.citers
