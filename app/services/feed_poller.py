"""From feed announcements to stored papers.

Each subscribed category's feed is read through the sources, the papers it lists are linked to
the category, and the ids that need it are asked of the API: new papers are stored, newer
versions of stored papers replace what was stored. What a feed did is counted as it goes, so a
feed that fails half-way still reports what it got done.
"""
import logging
from dataclasses import dataclass

from app.db import crud
from app.services.sources import Metadata

logger = logging.getLogger(__name__)

# Announce types that only ever update a stored paper; every other type may create one.
REPLACING_TYPES = {"replace", "replace-cross"}

# The arXiv API is asked for this many ids per request.
API_BATCH_SIZE = 100


@dataclass
class FeedStats:
    feed: str
    listed: int = 0
    new_papers: int = 0
    updated: int = 0
    skipped: int = 0
    error_message: str | None = None


def is_withdrawal(comment: str | None) -> bool:
    """arXiv says a version is a withdrawal in its comment."""
    return "withdrawn" in (comment or "").lower()


def _paper_values(meta: Metadata) -> dict:
    """The columns of a new paper row."""
    return {"arxiv_id": meta.arxiv_id, "published_at": meta.published_at, **_version_values(meta)}


def _version_values(meta: Metadata) -> dict:
    """The columns a replacement overwrites: everything the API says about the current version."""
    return {
        "version": meta.version,
        "title": meta.title,
        "authors": meta.authors,
        "abstract": meta.abstract,
        "pdf_url": meta.pdf_url,
        "updated_at": meta.updated_at,
        "categories": meta.categories,
        "journal_ref": meta.journal_ref,
        "doi": meta.doi,
        "comment": meta.comment,
    }


async def poll_feed(category, sources, stats: FeedStats) -> None:
    """Poll the feed of one category; stats is filled in as the work is done."""
    code = category.arxiv_code
    announcements = await sources.announcements(code)
    stats.listed = len(announcements)
    if not announcements:
        logger.info("No entries in feed for %s", code)
        return

    stored = await crud.get_stored_versions([a.arxiv_id for a in announcements])

    # Every stored paper the feed lists belongs to this category from now on.
    await crud.link_papers_to_category(
        [stored[a.arxiv_id][0] for a in announcements if a.arxiv_id in stored], category.id
    )

    to_create: list[str] = []
    to_update: list[str] = []
    for a in announcements:
        if a.arxiv_id not in stored:
            # replace and replace-cross never create a paper
            if a.kind not in REPLACING_TYPES:
                to_create.append(a.arxiv_id)
        elif a.kind in REPLACING_TYPES:
            if a.version is None or a.version > stored[a.arxiv_id][1]:
                to_update.append(a.arxiv_id)

    wanted = to_create + to_update
    if not wanted:
        logger.info("No new papers for %s", code)
        return

    creating = set(to_create)
    # All of them, in batches: each batch is stored as soon as it arrives.
    for start in range(0, len(wanted), API_BATCH_SIZE):
        batch = wanted[start:start + API_BATCH_SIZE]
        found = await sources.metadata(batch)

        new_rows: list[dict] = []
        for arxiv_id in batch:
            meta = found.get(arxiv_id)
            if meta is None:
                stats.skipped += 1
                logger.warning("arXiv API returned nothing for %s; skipped", arxiv_id)
                continue
            if arxiv_id in creating:
                new_rows.append(_paper_values(meta))
                continue
            paper_id, stored_version = stored[arxiv_id]
            if meta.version > stored_version and await crud.apply_replacement(
                paper_id, _version_values(meta), code
            ):
                stats.updated += 1
        if new_rows:
            stats.new_papers += await crud.store_new_papers(new_rows, category.id, code)

    logger.info(
        "%s: %d new papers, %d updated, %d skipped",
        code, stats.new_papers, stats.updated, stats.skipped,
    )


async def poll_all(sources, on_feed=None) -> list[FeedStats]:
    """Poll every subscribed category, by id. One failing feed does not stop the others."""
    categories = await crud.get_all_categories()
    polled: list[FeedStats] = []
    for category in sorted(categories, key=lambda c: c.id):
        stats = FeedStats(feed=category.arxiv_code)
        try:
            await poll_feed(category, sources, stats)
        except Exception as e:
            logger.exception("Error polling %s", category.arxiv_code)
            stats.error_message = (str(e) or type(e).__name__)[:500]
        polled.append(stats)
        if on_feed:
            await on_feed(stats)
    return polled
