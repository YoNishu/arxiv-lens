import asyncio
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import arxiv
import feedparser
import httpx
import yaml

from app.config import settings
from app.db import crud
from app.db.migrate import split_arxiv_id

logger = logging.getLogger(__name__)

# Announce types that may create a paper, and the ones that only ever update a stored paper.
CREATING_TYPES = {"new", "cross"}
REPLACING_TYPES = {"replace", "replace-cross"}

# The arXiv API is asked for this many ids per request.
API_BATCH_SIZE = 100

_NEW_STYLE_ID = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?")
_OLD_STYLE_ID = re.compile(r"([a-z][a-z\-]*(?:\.[A-Z]{2})?/\d{7})(v\d+)?")
_ANNOUNCE_TYPE = re.compile(r"Announce Type:\s*([a-z\-]+)", re.IGNORECASE)
_TRAILING_VERSION = re.compile(r"v(\d+)\s*$")


@dataclass
class PollStats:
    new_papers: int = 0
    updated: int = 0
    skipped: int = 0

    def add(self, other: "PollStats") -> None:
        self.new_papers += other.new_papers
        self.updated += other.updated
        self.skipped += other.skipped


@dataclass
class Announcement:
    arxiv_id: str  # without version
    version: int | None  # the version the feed announces, when it says so
    kind: str  # new, cross, replace, replace-cross


@dataclass
class PollResult:
    stats: PollStats = field(default_factory=PollStats)
    per_category: dict[str, int] = field(default_factory=dict)


def load_categories_from_yaml() -> list[dict]:
    path = Path(settings.categories_file)
    if not path.exists():
        logger.warning("Categories file %s not found", path)
        return []
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("categories", []) or []


async def sync_categories_from_yaml() -> None:
    """Create the categories of the seed file, each of them only the first time it is seen.

    A seeded category that was removed afterwards stays removed.
    """
    created = await crud.seed_categories(load_categories_from_yaml())
    for code in created:
        logger.info("Synced category %s from YAML", code)


def _extract_arxiv_id(link: str) -> str | None:
    """The arXiv id in a link or guid, without its version."""
    m = _NEW_STYLE_ID.search(link) or _OLD_STYLE_ID.search(link)
    return m.group(1) if m else None


def _parse_entry(entry) -> Announcement | None:
    link = entry.get("link", "") or ""
    guid = entry.get("id", "") or ""
    summary = entry.get("summary", "") or ""
    arxiv_id = _extract_arxiv_id(link) or _extract_arxiv_id(guid)
    if not arxiv_id:
        return None

    # Feed links carry no version; the guid (oai:arXiv.org:2401.00001v2) and the description
    # (arXiv:2401.00001v2 Announce Type: replace) do.
    version = None
    m = _TRAILING_VERSION.search(guid)
    if not m:
        m = re.search(re.escape(arxiv_id) + r"v(\d+)", summary)
    if m:
        version = int(m.group(1))

    kind = (entry.get("arxiv_announce_type") or "").strip().lower()
    if not kind:
        m = _ANNOUNCE_TYPE.search(summary)
        kind = m.group(1).lower() if m else "new"
    return Announcement(arxiv_id=arxiv_id, version=version, kind=kind)


async def fetch_announcements(arxiv_code: str) -> list[Announcement]:
    """Read the feed of one category: one announcement per paper, in feed order."""
    url = f"{settings.arxiv_rss_base.rstrip('/')}/{arxiv_code}"
    logger.info("Polling %s", url)
    # Fetched here rather than by feedparser, so a feed that cannot be read is an error in the
    # log and not an empty feed.
    headers = {"User-Agent": feedparser.USER_AGENT}
    async with httpx.AsyncClient(follow_redirects=True, timeout=60, headers=headers) as client:
        resp = await client.get(url)
        resp.raise_for_status()
    feed = await asyncio.to_thread(feedparser.parse, resp.content)

    seen: dict[str, Announcement] = {}
    for entry in feed.get("entries", []):
        ann = _parse_entry(entry)
        if not ann:
            continue
        previous = seen.get(ann.arxiv_id)
        if previous is None:
            seen[ann.arxiv_id] = ann
        elif (ann.version or 0) > (previous.version or 0):
            previous.version = ann.version
    return list(seen.values())


def _api_client() -> arxiv.Client:
    """A client for the batches of one feed; it pauses delay_seconds between its requests."""
    client = arxiv.Client(
        page_size=API_BATCH_SIZE, delay_seconds=settings.arxiv_api_delay_seconds, num_retries=3
    )
    client.query_url_format = settings.arxiv_api_url.rstrip("?") + "?{}"
    return client


async def _fetch_metadata(client: arxiv.Client, arxiv_ids: list[str]) -> dict[str, tuple[int, arxiv.Result]]:
    """Ask the API for one batch of ids. Maps the id without version to (version, result).

    Ids the API has no entry for are simply missing from the answer.
    """
    if not arxiv_ids:
        return {}
    # max_results=None: without it the client stops after its default of 100 results.
    search = arxiv.Search(id_list=arxiv_ids, max_results=None)
    results = await asyncio.to_thread(lambda: list(client.results(search)))
    mapping: dict[str, tuple[int, arxiv.Result]] = {}
    for r in results:
        base, version = split_arxiv_id(r.entry_id.split("/abs/")[-1])
        mapping[base] = (version or 1, r)
    return mapping


def _paper_values(arxiv_id: str, version: int, result: arxiv.Result) -> dict:
    return {
        "arxiv_id": arxiv_id,
        "version": version,
        "title": result.title,
        "authors": [a.name for a in result.authors],
        "abstract": result.summary,
        "pdf_url": result.pdf_url,
        "published_at": result.published,
        "categories": list(result.categories),
        "journal_ref": result.journal_ref,
        "doi": result.doi,
    }


async def poll_category_stats(arxiv_code: str, category_id: int) -> PollStats:
    stats = PollStats()
    announcements = await fetch_announcements(arxiv_code)
    if not announcements:
        logger.info("No entries in feed for %s", arxiv_code)
        return stats

    stored = await crud.get_stored_versions([a.arxiv_id for a in announcements])

    # Every stored paper the feed lists belongs to this category from now on.
    await crud.link_papers_to_category(
        [stored[a.arxiv_id][0] for a in announcements if a.arxiv_id in stored], category_id
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
        logger.info("No new papers for %s", arxiv_code)
        return stats

    creating = set(to_create)
    client = _api_client()
    # All of them, in batches: each batch is stored as soon as it arrives.
    for start in range(0, len(wanted), API_BATCH_SIZE):
        batch = wanted[start:start + API_BATCH_SIZE]
        metadata = await _fetch_metadata(client, batch)

        new_rows: list[dict] = []
        for arxiv_id in batch:
            found = metadata.get(arxiv_id)
            if found is None:
                stats.skipped += 1
                logger.warning("arXiv API returned nothing for %s — skipped", arxiv_id)
                continue
            version, result = found
            if arxiv_id in creating:
                new_rows.append(_paper_values(arxiv_id, version, result))
            else:
                paper_id, stored_version = stored[arxiv_id]
                if version > stored_version and await crud.apply_replacement(
                    paper_id, version, result.title, result.summary, result.pdf_url
                ):
                    stats.updated += 1
        stats.new_papers += await crud.store_new_papers(new_rows, category_id)

    logger.info(
        "%s: %d new papers, %d updated, %d skipped",
        arxiv_code, stats.new_papers, stats.updated, stats.skipped,
    )
    return stats


async def poll_category(arxiv_code: str, category_id: int) -> int:
    return (await poll_category_stats(arxiv_code, category_id)).new_papers


async def poll_all(on_progress=None) -> PollResult:
    """Poll every subscribed category. One failing feed does not stop the others."""
    categories = await crud.get_all_categories()
    outcome = PollResult()
    for cat in sorted(categories, key=lambda c: c.id):
        try:
            stats = await poll_category_stats(cat.arxiv_code, cat.id)
        except Exception:
            logger.exception("Error polling %s", cat.arxiv_code)
            stats = PollStats()
        outcome.stats.add(stats)
        outcome.per_category[cat.name] = stats.new_papers
        if on_progress:
            await on_progress(outcome.stats)
    return outcome


async def poll_all_categories() -> dict[str, int]:
    return (await poll_all()).per_category
