"""Stage 5: for every listed paper, the listed papers most like it.

The search does not score every pair. For the paper whose list is made (the owner) it takes the
owner's probe_terms heaviest terms, and a paper is scored only when it holds at least min_shared
of them (all of them when the owner has fewer). The test is made from the owner's side alone, so
one paper can stand in another's list without the reverse. The score is the dot of the two
vectors; a paper is kept from min_sim up, the list is ordered by falling score, then id, and cut
at top_k. similar.jsonl holds one row per listed paper; withdrawn papers have none and are in none.
"""
import logging

from app.insights import work
from app.insights.vectors import dot, probe

logger = logging.getLogger(__name__)

STAGE_VERSION = 1


def score(pairs: list[tuple[str, float]], weights: dict[str, float]) -> float:
    work.count("similar.scored")
    return dot(pairs, weights)


def search(owner: str, vectors: dict[str, list], weights: dict[str, dict], postings: dict[str, list[str]],
           settings) -> list[tuple[str, float]]:
    """The neighbour list of one owner as (arxiv_id, score) pairs."""
    work.count("similar.searched")
    terms = probe(vectors[owner], settings.probe_terms)
    need = min(settings.min_shared, len(terms))
    if need == 0:
        return []
    hits: dict[str, int] = {}
    for term in terms:
        for other in postings[term]:
            if other != owner:
                hits[other] = hits.get(other, 0) + 1
    kept: list[tuple[str, float]] = []
    for candidate in sorted(hits):
        if hits[candidate] < need:
            continue
        s = score(vectors[owner], weights[candidate])
        if s >= settings.min_sim:
            kept.append((candidate, s))
    kept.sort(key=lambda item: (-item[1], item[0]))
    return kept[:settings.top_k]


def run(ctx) -> dict[str, int]:
    section = ctx.settings.similar
    listed = sorted(paper["arxiv_id"] for paper in ctx.papers if not paper["withdrawn"])
    vectors = {arxiv_id: ctx.vectors[arxiv_id] for arxiv_id in listed}
    weights = {arxiv_id: dict(ctx.vectors[arxiv_id]) for arxiv_id in listed}
    postings: dict[str, list[str]] = {}
    for arxiv_id in listed:
        for term, _ in vectors[arxiv_id]:
            postings.setdefault(term, []).append(arxiv_id)
    rows = []
    entries = 0
    for arxiv_id in listed:
        found = search(arxiv_id, vectors, weights, postings, section)
        rows.append({"arxiv_id": arxiv_id, "similar": [{"id": other, "score": s} for other, s in found]})
        entries += len(found)
    ctx.write_jsonl("similar.jsonl", rows)
    return {"lists": len(rows), "entries": entries}
