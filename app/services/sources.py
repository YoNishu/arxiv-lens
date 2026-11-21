"""The four outside services a cycle talks to: arXiv's feeds, arXiv's API, the PDF host, the model.

HttpSources reaches them over HTTP, the way production runs. CorpusSources answers the same
questions in-process from a corpus folder (see dev/), for the day of the clock, so a home can be
fed without any server. Both have the same five methods and fail with the same exceptions and
the same words, so the poller and the processor cannot tell them apart.
"""
import asyncio
import importlib.util
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yaml

from app import clock, faults, home, ids
from app.config import settings
from app.services import paper_analyzer, pdf_extractor

logger = logging.getLogger(__name__)

_NEW_STYLE_ID = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?")
_OLD_STYLE_ID = re.compile(r"([a-z][a-z\-]*(?:\.[A-Z]{2})?/\d{7})(v\d+)?")
_ANNOUNCE_TYPE = re.compile(r"Announce Type:\s*([a-z\-]+)", re.IGNORECASE)
_TRAILING_VERSION = re.compile(r"v(\d+)\s*$")

_STANDIN_MODULE = "arxivlens_standin"


@dataclass
class Announcement:
    arxiv_id: str  # without version
    version: int | None  # the version the feed announces, when it says so
    kind: str  # new, cross, replace, replace-cross


@dataclass
class Metadata:
    """One API entry: the latest version of a paper."""

    arxiv_id: str
    version: int
    title: str
    authors: list[str]
    abstract: str
    pdf_url: str
    published_at: datetime
    updated_at: datetime
    categories: list[str]
    journal_ref: str | None
    doi: str | None
    comment: str | None


def _deduplicated(announcements: list[Announcement]) -> list[Announcement]:
    """One announcement per paper, in feed order: the first kind, the highest version."""
    seen: dict[str, Announcement] = {}
    for ann in announcements:
        previous = seen.get(ann.arxiv_id)
        if previous is None:
            seen[ann.arxiv_id] = ann
        elif (ann.version or 0) > (previous.version or 0):
            previous.version = ann.version
    return list(seen.values())


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _pdf_link(arxiv_id: str, version: int) -> str:
    return f"https://arxiv.org/pdf/{arxiv_id}v{version}"


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


class HttpSources:
    """arXiv's feeds and API, the PDF host and the model endpoint, over HTTP.

    Addresses and the API delay are read from the settings when a request is made. With seeds,
    the categories to seed are those feed codes instead of the categories file.
    """

    def __init__(self, seeds: list[str] | None = None):
        self._seeds = list(seeds) if seeds is not None else None
        # One API client per object: it keeps the pause between its own requests.
        self._client = None

    def seed_categories(self) -> list[dict]:
        if self._seeds is not None:
            return [{"arxiv_code": code, "name": code} for code in self._seeds]
        path = Path(settings.categories_file)
        if not path.exists():
            # Running without a categories file is normal: categories are then added by hand.
            logger.info("No categories file at %s; nothing to seed", path)
            return []
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data.get("categories", []) or []

    async def announcements(self, arxiv_code: str) -> list[Announcement]:
        """Read the feed of one category: one announcement per paper, in feed order."""
        import feedparser

        url = f"{settings.arxiv_rss_base.rstrip('/')}/{arxiv_code}"
        logger.info("Polling %s", url)
        # Fetched here rather than by feedparser, so a feed that cannot be read is an error in the
        # log and not an empty feed.
        headers = {"User-Agent": feedparser.USER_AGENT}
        async with httpx.AsyncClient(follow_redirects=True, timeout=60, headers=headers) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        feed = await asyncio.to_thread(feedparser.parse, resp.content)
        parsed = [_parse_entry(entry) for entry in feed.get("entries", [])]
        return _deduplicated([ann for ann in parsed if ann is not None])

    def _api_client(self):
        import arxiv

        from app.services.feed_poller import API_BATCH_SIZE

        if self._client is None:
            # Pages as large as the poller's batches: one request per batch.
            self._client = arxiv.Client(
                page_size=API_BATCH_SIZE, delay_seconds=settings.arxiv_api_delay_seconds, num_retries=3
            )
        self._client.query_url_format = settings.arxiv_api_url.rstrip("?") + "?{}"
        return self._client

    async def metadata(self, arxiv_ids: list[str]) -> dict[str, Metadata]:
        """Ask the API for one batch of ids. Ids it has no entry for are missing from the answer."""
        import arxiv

        if not arxiv_ids:
            return {}
        client = self._api_client()
        # max_results=None: without it the client stops after its default of 100 results.
        search = arxiv.Search(id_list=list(arxiv_ids), max_results=None)
        results = await asyncio.to_thread(lambda: list(client.results(search)))
        found: dict[str, Metadata] = {}
        for result in results:
            meta = self._from_result(result)
            found[meta.arxiv_id] = meta
        return found

    @staticmethod
    def _from_result(result) -> Metadata:
        arxiv_id, version = ids.split(result.entry_id.split("/abs/")[-1])
        version = version or 1
        return Metadata(
            arxiv_id=arxiv_id,
            version=version,
            title=result.title.strip(),
            authors=[a.name for a in result.authors],
            abstract=result.summary.strip(),
            pdf_url=result.pdf_url or _pdf_link(arxiv_id, version),
            published_at=_utc(result.published),
            updated_at=_utc(result.updated),
            categories=list(result.categories),
            journal_ref=result.journal_ref or None,
            doi=result.doi or None,
            comment=result.comment or None,
        )

    async def pdf(self, paper) -> bytes:
        if settings.arxiv_pdf_base:
            url = f"{settings.arxiv_pdf_base.rstrip('/')}/{paper.arxiv_id}v{paper.version}"
        else:
            url = paper.pdf_url
        return await pdf_extractor.download_pdf(url)

    async def analysis(self, paper, text: str) -> dict:
        return await paper_analyzer.analyze_paper(
            paper.arxiv_id, paper.version, paper.title, paper.abstract, text
        )


def _standin():
    """dev/standin.py as a module, loaded by its path so that nothing depends on sys.path."""
    module = sys.modules.get(_STANDIN_MODULE)
    if module is None:
        spec = importlib.util.spec_from_file_location(_STANDIN_MODULE, home.ROOT / "dev" / "standin.py")
        module = importlib.util.module_from_spec(spec)
        # Registered before it runs: dataclasses look their module up while the class is made.
        sys.modules[_STANDIN_MODULE] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            del sys.modules[_STANDIN_MODULE]
            raise
    return module


class CorpusSources:
    """The same four services answered in-process from a corpus folder, for the clock's day.

    The answers come from the stand-in's own World, so a home fed this way ends up the same as
    one fed over HTTP from a stand-in serving the same corpus.
    """

    def __init__(self, path):
        self.path = os.fspath(path)
        standin = _standin()
        self._unavailable = standin.Unavailable
        self._world = standin.World(corpus=self.path, today=clock.today)

    def seed_categories(self) -> list[dict]:
        return [{"arxiv_code": code, "name": code} for code in self._world.feeds]

    async def announcements(self, arxiv_code: str) -> list[Announcement]:
        listed = self._world.announcements(arxiv_code)
        return _deduplicated([Announcement(arxiv_id=i, version=v, kind=k) for i, v, k in listed])

    async def metadata(self, arxiv_ids: list[str]) -> dict[str, Metadata]:
        found: dict[str, Metadata] = {}
        for entry in self._world.entries(list(arxiv_ids)):
            meta = self._from_entry(entry)
            found[meta.arxiv_id] = meta
        return found

    @staticmethod
    def _from_entry(entry: dict) -> Metadata:
        # The strings the HTTP path would store for the same entry: stripped, empty ones as None.
        arxiv_id, suffix = ids.split(entry["id"])
        version = int(entry.get("v") or suffix or 1)
        return Metadata(
            arxiv_id=arxiv_id,
            version=version,
            title=entry["title"].strip(),
            authors=list(entry["authors"]),
            abstract=entry["abstract"].strip(),
            pdf_url=_pdf_link(arxiv_id, version),
            published_at=clock.parse_instant(entry["published"]),
            updated_at=clock.parse_instant(entry["updated"]),
            categories=list(entry["categories"]),
            journal_ref=entry.get("journal_ref") or None,
            doi=entry.get("doi") or None,
            comment=entry.get("comment") or None,
        )

    async def pdf(self, paper) -> bytes:
        data = self._world.pdf(paper.arxiv_id, paper.version)
        if data is None:
            raise pdf_extractor.PdfUnavailable("PDF request failed with status 404")
        return data

    async def analysis(self, paper, text: str) -> dict:
        await faults.slow()
        try:
            answer = self._world.analysis(paper.arxiv_id, paper.version)
        except self._unavailable as e:
            raise paper_analyzer.ModelError(f"Model request failed with status {_status_of(e)}") from None
        return paper_analyzer.checked(answer)


def _status_of(error) -> int | None:
    """The HTTP status the stand-in's World gave up with."""
    status = getattr(error, "status", None)
    if status is None and error.args:
        status = error.args[0]
    return status


def current() -> HttpSources | CorpusSources:
    """The sources of this home: its corpus when play has given it one, else the real services."""
    marker = home.corpus_file()
    if not marker.exists():
        return HttpSources()
    with open(marker, "rb") as f:
        recorded = json.loads(f.read().decode("utf-8"))
    path = recorded["path"]
    manifest = Path(path) / "corpus.json"
    if not manifest.is_file():
        from app.cli import CliError

        raise CliError(f"corpus not found: {path}", 2)
    if recorded.get("fast"):
        return CorpusSources(path)
    with open(manifest, "rb") as f:
        feeds = json.loads(f.read().decode("utf-8"))["feeds"]
    return HttpSources(seeds=feeds)
