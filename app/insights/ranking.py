"""Stage 9: weekly rankings per feed.

Every feed's analysed, listed papers of one announced week are ranked with what the library knew at
the end of that week: only references to papers announced by then count, and only citations from
papers announced by then. The score mixes the model's novelty, how many such references a paper has
and how often the papers it builds on are cited. The running week is ranked the same way with its
own last day, so the day of the build never enters a ranking. The folder shows it in ranking.jsonl,
one row per feed and week.
"""
import logging
from datetime import date, timedelta

from app.insights import asof, canon, work

logger = logging.getLogger(__name__)

STAGE_VERSION = 1

# References that count for the grounding part; citations at which the heat part is full.
GROUNDING_CAP = 8
HEAT_CAP = 50


def is_source(paper: dict) -> bool:
    """Whether the paper is ranked at all: analysed and not withdrawn."""
    return paper["state"] == "analysed" and not paper["withdrawn"]


def cites_as_of(target: str, end: date, citers: dict[str, list[str]], announced: dict[str, date]) -> int:
    """How many papers announced by `end` cite `target`."""
    n = 0
    for q in citers.get(target, ()):
        if announced[q] <= end:
            n += 1
    return n


def _score(novelty: int, grounding: int, heat: int, settings) -> float:
    return (
        settings.novelty * (novelty / 10)
        + settings.grounding * (min(grounding, GROUNDING_CAP) / GROUNDING_CAP)
        + settings.heat * min(1.0, canon.ln(heat + 1) / canon.ln(HEAT_CAP + 1))
    )


def rank_bucket(
    feed: str,
    monday: date,
    members: list[str],
    novelty: dict[str, int],
    cites: dict[str, list[str]],
    citers: dict[str, list[str]],
    announced: dict[str, date],
    settings,
) -> dict:
    """The row of ranking.jsonl for one feed and the week that starts on `monday`."""
    work.count("ranking.buckets")
    end = monday + timedelta(days=6)
    scored = []
    for paper_id in members:
        # Today's reference lists, but only the papers that were in the library by the week's end.
        targets = [b for b in cites.get(paper_id, ()) if announced[b] <= end]
        grounding = len(targets)
        heat = 0
        for b in targets:
            heat += cites_as_of(b, end, citers, announced)
        score = _score(novelty[paper_id], grounding, heat, settings)
        scored.append((-score, paper_id, novelty[paper_id], grounding, heat))
    scored.sort()

    m = len(scored)
    papers = []
    for rank, (negative, paper_id, paper_novelty, grounding, heat) in enumerate(scored, start=1):
        percentile = 100 if m == 1 else 100 * (m - rank) // (m - 1)
        papers.append(
            {
                "arxiv_id": paper_id,
                "rank": rank,
                "score": -negative,
                "novelty": paper_novelty,
                "grounding": grounding,
                "heat": heat,
                "percentile": percentile,
            }
        )
    return {"feed": feed, "week": asof.week_label(monday), "papers": papers}


def run(ctx) -> dict[str, int]:
    settings = ctx.settings.ranking
    announced = {paper["arxiv_id"]: asof.day_of(paper["announced"]) for paper in ctx.papers}
    novelty: dict[str, int] = {}
    members: dict[tuple[str, date], list[str]] = {}
    for paper in ctx.papers:
        if not is_source(paper):
            continue
        paper_id = paper["arxiv_id"]
        novelty[paper_id] = paper["novelty"]
        monday = asof.monday_of(announced[paper_id])
        for feed in paper["feeds"]:
            members.setdefault((feed, monday), []).append(paper_id)

    rows = [
        rank_bucket(feed, monday, members[(feed, monday)], novelty, ctx.cites, ctx.citers, announced, settings)
        for feed, monday in sorted(members)
    ]
    ctx.write_jsonl("ranking.jsonl", rows)
    ctx.buckets = rows
    ranked = sum(len(row["papers"]) for row in rows)  # ints
    return {"buckets": len(rows), "ranked": ranked}
