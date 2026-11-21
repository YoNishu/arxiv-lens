"""Stage 10: one Markdown digest page per feed and announced week, `digests/<feed>/<week>.md`.

A page shows the first papers of its bucket in `ranking.jsonl` in rank order, each with its authors,
score and topic name from `topics.json`. Its third line says whether the week is closed or, for the
running week, which day of it the build was made on.
"""
import logging
from datetime import timedelta

from app.insights import work
from app.insights.asof import AsOf, week_monday

logger = logging.getLogger(__name__)

STAGE_VERSION = 1

AUTHORS_SHOWN = 3


def render(bucket: dict, papers: dict[str, dict], topic_of: dict[str, int | None], topic_names: list[str],
           asof: AsOf, top: int) -> str:
    """The page of one bucket; `papers` maps an id to its row of papers.jsonl."""
    work.count("digests.pages")
    monday = week_monday(bucket["week"])
    sunday = monday + timedelta(days=6)
    members = bucket["papers"]
    if monday < asof.week_start:
        status = "closed"
    else:
        status = f"open, day {asof.day.isoweekday()} of 7"
    feed = bucket["feed"]
    week = bucket["week"]
    lines = [
        f"# {feed} week {week}",
        "",
        f"{monday.isoformat()} to {sunday.isoformat()}, {status}, {len(members)} ranked",
        "",
    ]
    for member in members[:top]:
        arxiv_id = member["arxiv_id"]
        row = papers[arxiv_id]
        topic = topic_of.get(arxiv_id)
        topic_name = "-" if topic is None else topic_names[topic]
        score = format(member["score"], ".2f")
        lines.append(f"{member['rank']}. {row['title']} ({arxiv_id})")
        lines.append(f"   {_authors(row['authors'])} | score {score} | {topic_name}")
    return "\n".join(lines) + "\n"


def run(ctx) -> dict[str, int]:
    papers = {paper["arxiv_id"]: paper for paper in ctx.papers}
    top = ctx.settings.digests.top
    for bucket in ctx.buckets:
        page = render(bucket, papers, ctx.topic_of, ctx.topic_names, ctx.asof, top)
        ctx.write_text(f"digests/{bucket['feed']}/{bucket['week']}.md", page)
    ctx.prune("digests")
    return {"pages": len(ctx.buckets)}


def _authors(names: list[str]) -> str:
    shown = ", ".join(names[:AUTHORS_SHOWN])
    if len(names) > AUTHORS_SHOWN:
        shown += " et al."
    return shown
