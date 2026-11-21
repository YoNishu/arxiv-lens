"""The cited arXiv ids the library has not analysed, most cited first.

Usage:  python tools/missing_refs.py [--min N]

Counts come from the insights folder of the home; what is said about each id (not tracked,
withdrawn, failed with its last error, or waiting) comes from the database as it is now, so an
id the folder calls external may be a stored paper by now. Ids cited fewer than N times
(default 2) are left out.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # so "python tools/missing_refs.py" finds the app package

from app import home

ERROR_CHARS = 80


def cited_counts(folder: Path) -> dict[str, int] | None:
    """Id to the number of stored papers citing it; None when the folder has no citation files."""
    external = folder / "citations" / "external.jsonl"
    if not external.is_file():
        return None
    counts: dict[str, int] = {}
    with open(external, "rb") as f:
        for line in f:
            if line.strip():
                row = json.loads(line)
                counts[row["arxiv_id"]] = row["cited_by"]
    nodes = folder / "citations" / "nodes.jsonl"
    if nodes.is_file():
        with open(nodes, "rb") as f:
            for line in f:
                if line.strip():
                    row = json.loads(line)
                    if row["cited_by"] >= 1:
                        counts[row["arxiv_id"]] = row["cited_by"]
    return counts


async def _library() -> dict[str, tuple]:
    from sqlalchemy import and_, select

    from app.db.database import async_session, open_database
    from app.db.models import Analysis, Paper

    await open_database()
    query = (
        select(Paper.arxiv_id, Paper.status, Paper.attempts, Paper.error_message, Paper.withdrawn_at, Analysis.id)
        .outerjoin(Analysis, and_(Analysis.paper_id == Paper.id, Analysis.paper_version == Paper.version))
        .order_by(Paper.arxiv_id)
    )
    async with async_session() as session:
        rows = (await session.execute(query)).all()
    return {r[0]: (r[1], r[2], r[3], r[4], r[5] is not None) for r in rows}


def library() -> dict[str, tuple]:
    """arxiv_id to (status, attempts, error_message, withdrawn_at, analysed) for every stored paper."""
    from app.db import database

    return database.run(_library())


def _status_text(status) -> str:
    return getattr(status, "value", status)


def what(row: tuple | None) -> str | None:
    """What to say about a cited id, or None when its current version is analysed."""
    if row is None:
        return "not tracked"
    status, attempts, error, withdrawn_at, analysed = row
    if withdrawn_at is not None:
        return "withdrawn"
    if analysed:
        return None
    if _status_text(status) == "failed":
        squeezed = " ".join((error or "").split())[:ERROR_CHARS]
        # A cut can end on a space, and an empty error leaves one; output lines carry none at the end.
        return f"failed after {attempts} tries: {squeezed}".rstrip()
    return "waiting"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="List cited arXiv ids the library has not analysed.")
    parser.add_argument("--min", type=int, default=2, dest="least", metavar="N",
                        help="leave out ids cited fewer than N times (default 2)")
    args = parser.parse_args(argv)
    if args.least < 1:
        print("error: --min must be at least 1", file=sys.stderr)
        return 2
    folder = home.insights_dir()
    counts = cited_counts(folder)
    if counts is None:
        print(f"error: no result in {folder}", file=sys.stderr)
        return 1
    stored = library()
    kept = []
    for arxiv_id in sorted(counts):
        if counts[arxiv_id] < args.least:
            continue
        text = what(stored.get(arxiv_id))
        if text is not None:
            kept.append((counts[arxiv_id], arxiv_id, text))
    kept.sort(key=lambda k: (-k[0], k[1]))
    untracked = 0
    for count, arxiv_id, text in kept:
        if text == "not tracked":
            untracked += 1
        print(f"{count}\t{arxiv_id}\t{text}")
    print(f"{len(kept)} ids cited at least {args.least} times are not analysed: "
          f"{untracked} not tracked, {len(kept) - untracked} stored")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
