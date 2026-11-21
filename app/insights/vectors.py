"""Stage 4: one TF-IDF vector per paper, and the vector helpers other stages share.

A paper's vector is built from the terms of its current title, abstract and keywords: each term
the vocabulary kept weighs (1 + ln count) * idf, and the vector is divided by its length.
vectors.jsonl holds one row per paper, its pairs ascending by term. Every similarity in the
similar papers and the topics is a dot of two such vectors.
"""
import logging
import math

from app.insights import canon, work

logger = logging.getLogger(__name__)

STAGE_VERSION = 1


def weigh(terms: dict[str, int], idf: dict[str, float]) -> list[tuple[str, float]]:
    """Unit-length pairs of the terms that have an idf, ascending by term; [] when none has."""
    raw: list[tuple[str, float]] = []
    total = 0.0
    for term in sorted(terms):
        if term not in idf:
            continue
        x = (1.0 + canon.ln(terms[term])) * idf[term]
        total += x * x
        raw.append((term, x))
    if not raw:
        return []
    norm = math.sqrt(total)
    return [(term, x / norm) for term, x in raw]


def dot(pairs: list[tuple[str, float]], weights: dict[str, float]) -> float:
    """Sum of the products over the shared terms, added in the term order of pairs.

    Both vectors keep their pairs ascending by term, so dot(a, dict(b)) and dot(b, dict(a)) add
    the same products in the same order and give the same double.
    """
    s = 0.0
    for term, x in pairs:
        if term in weights:
            s += x * weights[term]
    return s


def probe(pairs: list[tuple[str, float]], n: int) -> list[str]:
    """The n heaviest terms, heaviest first, equal weights by term."""
    ordered = sorted(pairs, key=lambda pair: (-pair[1], pair[0]))
    return [term for term, _ in ordered[:n]]


def build_row(arxiv_id: str, terms: dict[str, int], idf: dict[str, float]) -> dict:
    work.count("vectors.built")
    return {"arxiv_id": arxiv_id, "vector": [[term, weight] for term, weight in weigh(terms, idf)]}


def run(ctx) -> dict[str, int]:
    rows = []
    vectors: dict[str, list] = {}
    weights = 0
    for paper in ctx.papers:
        arxiv_id = paper["arxiv_id"]
        row = build_row(arxiv_id, ctx.tokens[arxiv_id]["terms"], ctx.idf)
        rows.append(row)
        vectors[arxiv_id] = [(term, weight) for term, weight in row["vector"]]
        weights += len(row["vector"])
    ctx.write_jsonl("vectors.jsonl", rows)
    ctx.vectors = vectors
    return {"papers": len(rows), "weights": weights}
