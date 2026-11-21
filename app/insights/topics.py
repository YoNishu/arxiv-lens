"""Stage 7: topics fitted on the reference papers, and every listed paper put with its nearest one.

The fit uses the vectors of the reference papers' first stored title and abstract, so a month's
topics hold still like its vocabulary. centroids.json holds the fitted topics, assignments.jsonl the
topic and similarity of every listed paper, and topics.json each topic's card (heaviest terms and
nearest papers), its name and how many listed papers it holds, in all and in the last four weeks.
"""
import logging
import math
from dataclasses import dataclass
from datetime import timedelta

from app.insights import asof, labels, vectors, work

logger = logging.getLogger(__name__)

STAGE_VERSION = 1

K_MIN = 8
K_MAX = 40
RECENT_WEEKS = 4


@dataclass
class Fit:
    seeds: list[str]
    centroids: list[dict[str, float]]
    topic: dict[str, int]
    sim: dict[str, float]


def topic_count(n_reference: int, n_fit: int) -> int:
    """The whole number nearest to sqrt(n_reference / 2), kept within bounds and below n_fit."""
    # Integers only: a float square root and round() would put the halves on either side by chance.
    k = (math.isqrt(2 * n_reference) + 1) // 2
    k = max(K_MIN, min(K_MAX, k))
    return min(k, n_fit)


def nearest(pairs: list[tuple[str, float]], centroids: list[dict[str, float]]) -> tuple[int, float]:
    """The topic with the largest dot and that dot; of equal dots the lower topic."""
    if not centroids:
        raise ValueError("nearest needs at least one centroid")
    best, best_dot = 0, -1.0
    for j, centroid in enumerate(centroids):
        d = vectors.dot(pairs, centroid)
        if d > best_dot:
            best, best_dot = j, d
    return best, best_dot


def _seeds(fit_ids: list[str], fit_vectors: dict[str, list], weights: dict[str, dict[str, float]],
           k: int) -> list[str]:
    seed = fit_ids[0]
    seeds = [seed]
    taken = {seed}
    near = {}
    for p in fit_ids:
        near[p] = vectors.dot(fit_vectors[p], weights[seed])
    while len(seeds) < k:
        # The paper whose nearest seed is farthest away; the smaller id among equals.
        _, seed = min((near[p], p) for p in fit_ids if p not in taken)
        seeds.append(seed)
        taken.add(seed)
        for p in fit_ids:
            d = vectors.dot(fit_vectors[p], weights[seed])
            if d > near[p]:
                near[p] = d
    return seeds


def _round(fit_ids: list[str], fit_vectors: dict[str, list], centroids: list[dict[str, float]]) -> None:
    members: list[list[str]] = [[] for _ in centroids]
    for p in fit_ids:
        j, _ = nearest(fit_vectors[p], centroids)
        members[j].append(p)
    for j, ids in enumerate(members):
        if not ids:
            continue
        total: dict[str, float] = {}
        for p in ids:
            for term, x in fit_vectors[p]:
                total[term] = total.get(term, 0.0) + x
        terms = sorted(total)
        sq = 0.0
        for term in terms:
            sq += total[term] * total[term]
        norm = math.sqrt(sq)
        centroids[j] = {term: total[term] / norm for term in terms}


def fit(fit_ids: list[str], fit_vectors: dict[str, list], k: int, iterations: int) -> Fit:
    """k topics of the fit papers (ids ascending): farthest-first seeds, then `iterations` rounds."""
    work.count("topics.fits")
    if k == 0:
        return Fit([], [], {}, {})
    weights = {p: dict(fit_vectors[p]) for p in fit_ids}
    seeds = _seeds(fit_ids, fit_vectors, weights, k)
    centroids = [dict(weights[seed]) for seed in seeds]
    # Always every round: a stop on convergence would make the fit hang on a comparison of floats.
    for _ in range(iterations):
        _round(fit_ids, fit_vectors, centroids)
    topic: dict[str, int] = {}
    sim: dict[str, float] = {}
    for p in fit_ids:
        topic[p], sim[p] = nearest(fit_vectors[p], centroids)
    return Fit(seeds, centroids, topic, sim)


def assign(pairs: list[tuple[str, float]], centroids: list[dict[str, float]],
           min_sim: float) -> tuple[int | None, float]:
    """The nearest topic of a vector when its dot reaches min_sim, else None; with the dot."""
    work.count("topics.assigned")
    if not centroids or not pairs:
        return None, 0.0
    j, d = nearest(pairs, centroids)
    if d >= min_sim:
        return j, d
    return None, d


def card(topic: int, centroid: dict[str, float], fit: Fit, withdrawn: set[str], titles: dict[str, str],
         card_terms: int, exemplars: int) -> dict:
    """The heaviest terms of a topic and its fit members nearest to it, with today's titles."""
    terms = [term for term, _ in sorted(centroid.items(), key=lambda item: (-item[1], item[0]))[:card_terms]]
    members = [p for p, j in fit.topic.items() if j == topic and p not in withdrawn]
    members.sort(key=lambda p: (-fit.sim[p], p))
    return {
        "terms": terms,
        "exemplars": [{"arxiv_id": p, "title": titles[p]} for p in members[:exemplars]],
    }


def _centroids_file(fit_result: Fit, fitted: int) -> dict:
    members = [0] * len(fit_result.centroids)
    for j in fit_result.topic.values():
        members[j] += 1
    rows = []
    for j, centroid in enumerate(fit_result.centroids):
        rows.append({
            "centroid": [[term, weight] for term, weight in sorted(centroid.items())],
            "members": members[j],
            "seed": fit_result.seeds[j],
            "topic": j,
        })
    return {"fitted": fitted, "k": len(fit_result.centroids), "topics": rows}


def run(ctx) -> dict[str, int]:
    section = ctx.settings.topics

    fit_ids: list[str] = []
    fit_vectors: dict[str, list] = {}
    for arxiv_id in ctx.reference:
        pairs = vectors.weigh(ctx.tokens[arxiv_id]["first_terms"], ctx.idf)
        if pairs:
            fit_ids.append(arxiv_id)
            fit_vectors[arxiv_id] = pairs
    k = topic_count(len(ctx.reference), len(fit_ids))
    fit_result = fit(fit_ids, fit_vectors, k, section.iterations)
    ctx.write_json("centroids.json", _centroids_file(fit_result, len(fit_ids)))

    listed = [row for row in ctx.papers if not row["withdrawn"]]
    topic_of: dict[str, int | None] = {}
    rows = []
    for row in listed:
        arxiv_id = row["arxiv_id"]
        j, d = assign(ctx.vectors[arxiv_id], fit_result.centroids, section.min_topic_sim)
        topic_of[arxiv_id] = j
        rows.append({"arxiv_id": arxiv_id, "sim": d, "topic": j})
    ctx.write_jsonl("assignments.jsonl", rows)

    withdrawn = {row["arxiv_id"] for row in ctx.papers if row["withdrawn"]}
    titles = {row["arxiv_id"]: row["title"] for row in ctx.papers}
    cards = [
        card(j, fit_result.centroids[j], fit_result, withdrawn, titles, section.card_terms, section.exemplars)
        for j in range(k)
    ]
    names = labels.name_topics(cards)

    since = ctx.asof.week_start - timedelta(days=7 * (RECENT_WEEKS - 1))
    size = [0] * k
    recent = [0] * k
    for row in listed:
        j = topic_of[row["arxiv_id"]]
        if j is None:
            continue
        size[j] += 1
        if asof.day_of(row["announced"]) >= since:
            recent[j] += 1
    topics = []
    for j in range(k):
        topics.append({
            "blurb": names[j]["blurb"],
            "exemplars": cards[j]["exemplars"],
            "name": names[j]["name"],
            "recent": recent[j],
            "size": size[j],
            "source": names[j]["source"],
            "terms": cards[j]["terms"],
            "topic": j,
        })
    ctx.write_json("topics.json", {"k": k, "topics": topics})

    ctx.topic_of = topic_of
    ctx.topic_names = [name["name"] for name in names]
    assigned = len([j for j in topic_of.values() if j is not None])
    return {"topics": k, "assigned": assigned, "unassigned": len(listed) - assigned}
