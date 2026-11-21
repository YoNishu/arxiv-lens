"""Stage 6: the citation graph inside the library.

Built from the reference lists of the analysed, listed papers (text/refs.jsonl). The folder shows it
in citations/: edges.jsonl (who cites whom among stored papers), external.jsonl (cited arXiv ids the
library does not hold, with how many papers name them), nodes.jsonl (per paper its degrees and the
papers most often cited with it or sharing most of its references) and pagerank.jsonl (per paper its
connected part and its PageRank).
"""
import logging

from app.insights import work

logger = logging.getLogger(__name__)

STAGE_VERSION = 1

# Fewest common citers for a co-citation entry, and fewest shared reference ids for a coupling entry.
MIN_COCITED = 2
MIN_COUPLED = 2
# PageRank always runs all of its rounds, so two builds of one library do the same arithmetic.
ROUNDS = 40


def is_source(paper: dict) -> bool:
    """Whether the paper's reference list counts: analysed and not withdrawn."""
    return paper["state"] == "analysed" and not paper["withdrawn"]


def resolve(cites: list[str], stored: set[str]) -> tuple[list[str], list[str]]:
    """Split a reference list into the ids of stored papers and the others, each in list order."""
    work.count("citations.resolved")
    inside = []
    outside = []
    for cited in cites:
        if cited in stored:
            inside.append(cited)
        else:
            outside.append(cited)
    return inside, outside


def _cocited(paper_id: str, cites: dict[str, list[str]], citers: dict[str, list[str]], list_size: int) -> list[dict]:
    together: dict[str, int] = {}
    for citer in citers.get(paper_id, ()):
        for other in cites[citer]:
            if other != paper_id:
                together[other] = together.get(other, 0) + 1
    kept = sorted((-count, other) for other, count in together.items() if count >= MIN_COCITED)
    return [{"id": other, "count": -negative} for negative, other in kept[:list_size]]


def _coupled(paper_id: str, raw: dict[str, list[str]], raw_citers: dict[str, list[str]], list_size: int) -> list[dict]:
    if paper_id not in raw:
        # Only a paper whose own list counts can share references with another.
        return []
    shared: dict[str, int] = {}
    for cited in raw[paper_id]:
        for other in raw_citers[cited]:
            if other != paper_id:
                shared[other] = shared.get(other, 0) + 1
    own = len(raw[paper_id])
    # Among equal shared counts a smaller union is a larger share of common references, so the
    # order of the falling share is kept in whole numbers.
    kept = sorted(
        (-count, own + len(raw[other]) - count, other)
        for other, count in shared.items()
        if count >= MIN_COUPLED
    )
    return [{"id": other, "shared": -negative, "union": union} for negative, union, other in kept[:list_size]]


def node(
    paper_id: str,
    cites: dict[str, list[str]],
    citers: dict[str, list[str]],
    raw: dict[str, list[str]],
    raw_citers: dict[str, list[str]],
    list_size: int,
) -> dict:
    """One row of nodes.jsonl: degrees, external references, co-cited and coupled papers."""
    work.count("citations.lists")
    inside = len(cites.get(paper_id, ()))
    return {
        "arxiv_id": paper_id,
        "cited_by": len(citers.get(paper_id, ())),
        "cites": inside,
        "external": len(raw.get(paper_id, ())) - inside,
        "cocited": _cocited(paper_id, cites, citers, list_size),
        "coupled": _coupled(paper_id, raw, raw_citers, list_size),
    }


def _components(ids: list[str], cites: dict[str, list[str]], citers: dict[str, list[str]]) -> dict[str, str]:
    part: dict[str, str] = {}
    for start in ids:
        if start in part:
            continue
        # Papers are visited in ascending order, so the paper that opens a part is its smallest id.
        part[start] = start
        stack = [start]
        while stack:
            current = stack.pop()
            for neighbours in (cites.get(current, ()), citers.get(current, ())):
                for other in neighbours:
                    if other not in part:
                        part[other] = start
                        stack.append(other)
    return part


def _pagerank(ids: list[str], cites: dict[str, list[str]], citers: dict[str, list[str]], damping: float) -> dict[str, float]:
    n = len(ids)
    rank = {p: 1.0 / n for p in ids}
    base = (1.0 - damping) / n
    for _ in range(ROUNDS):
        dangling = 0.0
        share: dict[str, float] = {}
        for p in ids:
            deg = len(cites.get(p, ()))
            if deg > 0:
                share[p] = rank[p] / deg
            else:
                # A paper that cites nothing spreads its rank evenly over all papers.
                dangling += rank[p]
        spread = dangling / n
        new: dict[str, float] = {}
        for p in ids:
            incoming = 0.0
            for citer in citers.get(p, ()):
                incoming += share[citer]
            new[p] = base + damping * (incoming + spread)
        rank = new
    return rank


def graph_pass(
    ids: list[str], cites: dict[str, list[str]], citers: dict[str, list[str]], damping: float
) -> dict[str, tuple[str, float]]:
    """Per stored paper (ids ascending): the name of its connected part and its PageRank."""
    work.count("citations.graphs")
    if not ids:
        return {}
    part = _components(ids, cites, citers)
    rank = _pagerank(ids, cites, citers, damping)
    return {p: (part[p], rank[p]) for p in ids}


def run(ctx) -> dict[str, int]:
    settings = ctx.settings.citations
    ids = [paper["arxiv_id"] for paper in ctx.papers]
    stored = set(ids)
    sources = [paper["arxiv_id"] for paper in ctx.papers if is_source(paper)]

    cites: dict[str, list[str]] = {}
    citers: dict[str, list[str]] = {}
    raw: dict[str, list[str]] = {}
    raw_citers: dict[str, list[str]] = {}
    for source in sources:
        listed = ctx.refs[source]["cites"]
        inside, _outside = resolve(listed, stored)
        cites[source] = inside
        raw[source] = list(listed)
        # Sources come in ascending order, so both citer lists stay ascending.
        for cited in inside:
            citers.setdefault(cited, []).append(source)
        for cited in listed:
            raw_citers.setdefault(cited, []).append(source)

    edges = sorted((source, cited) for source in sources for cited in cites[source])
    ctx.write_jsonl("citations/edges.jsonl", [{"src": src, "dst": dst} for src, dst in edges])

    external = [
        {"arxiv_id": cited, "cited_by": len(raw_citers[cited])}
        for cited in sorted(raw_citers)
        if cited not in stored
    ]
    ctx.write_jsonl("citations/external.jsonl", external)

    nodes = [node(paper_id, cites, citers, raw, raw_citers, settings.list_size) for paper_id in ids]
    ctx.write_jsonl("citations/nodes.jsonl", nodes)

    graph = graph_pass(ids, cites, citers, settings.damping)
    ctx.write_jsonl(
        "citations/pagerank.jsonl",
        [{"arxiv_id": paper_id, "component": graph[paper_id][0], "pagerank": graph[paper_id][1]} for paper_id in ids],
    )

    ctx.cites = cites
    ctx.citers = citers
    components = 0
    for paper_id in ids:
        if graph[paper_id][0] == paper_id:
            components += 1
    return {
        "sources": len(sources),
        "edges": len(edges),
        "external": len(external),
        "nodes": len(nodes),
        "components": components,
    }
