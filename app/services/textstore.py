"""Paper texts: one gzip file per paper in the home's texts folder, described by a paper_texts row.

The text of a paper is kept apart from its analysis so that a try that fails at the model does
not have to fetch the PDF again. The row says which version the file holds and what its hash
is; a file whose content no longer matches its row is treated as missing.
"""
import gzip
import hashlib
import io
import logging
import os
import zlib
from pathlib import Path

from sqlalchemy import delete, insert, select

from app import clock, home, ids
from app.db.database import async_session
from app.db.models import PaperText

logger = logging.getLogger(__name__)

# What is stored when the PDF gave no text: this line, then the abstract.
PLACEHOLDER = "[Text extraction failed \u2014 abstract only]\n\n"

_texts = PaperText.__table__


def file_for(arxiv_id: str, version: int) -> Path:
    return home.texts_dir() / f"{ids.file_stem(arxiv_id)}v{version}.txt.gz"


def digest(text: str) -> str:
    """The hash a paper_texts row keeps: of the UTF-8 bytes, never of the compressed file."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write(arxiv_id: str, version: int, text: str) -> None:
    """Write the file of one text, replacing an earlier file of the same version."""
    buffer = io.BytesIO()
    # No file name and no time in the header, so the same text always gives the same file.
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, mtime=0, compresslevel=6) as f:
        f.write(text.encode("utf-8"))
    home.write_file(file_for(arxiv_id, version), buffer.getvalue())


def read(arxiv_id: str, version: int) -> str | None:
    """The text in the file of that version, or None when the file is missing or unreadable."""
    try:
        with open(file_for(arxiv_id, version), "rb") as f:
            data = f.read()
    except FileNotFoundError:
        return None
    except OSError as e:
        logger.warning("Text of %sv%s cannot be read: %s", arxiv_id, version, e)
        return None
    try:
        return gzip.decompress(data).decode("utf-8")
    except (OSError, EOFError, ValueError, zlib.error) as e:
        # A damaged or cut-off file: the caller fetches the text again.
        logger.warning("Text of %sv%s cannot be read: %s", arxiv_id, version, e)
        return None


async def get(paper) -> str | None:
    """The stored text of the paper's current version, when its file still matches its row."""
    async with async_session() as s:
        row = (
            await s.execute(select(_texts.c.version, _texts.c.sha256).where(_texts.c.paper_id == paper.id))
        ).first()
    if row is None or row.version != paper.version:
        return None
    text = read(paper.arxiv_id, paper.version)
    if text is None or digest(text) != row.sha256:
        return None
    return text


async def put(paper, text: str, extraction: str) -> None:
    """Store the text of the paper's current version.

    File first, row second, the file of the version the row named before last: a process that
    dies in between leaves at worst a file without a row, which the next write overwrites.
    """
    write(paper.arxiv_id, paper.version, text)
    async with async_session() as s:
        # Deleting first takes the write lock before anything is read, and hands back the
        # version the row named until now.
        previous = (
            await s.execute(delete(_texts).where(_texts.c.paper_id == paper.id).returning(_texts.c.version))
        ).first()
        await s.execute(
            insert(_texts).values(
                paper_id=paper.id,
                version=paper.version,
                sha256=digest(text),
                chars=len(text),
                extraction=extraction,
                extracted_at=clock.now(),
            )
        )
        await s.commit()
    if previous is not None and previous[0] != paper.version:
        remove(paper.arxiv_id, previous[0])


def remove(arxiv_id: str, version: int) -> None:
    """Remove the file of one text; a file that is not there is no error."""
    try:
        os.remove(file_for(arxiv_id, version))
    except FileNotFoundError:
        pass
