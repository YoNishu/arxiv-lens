"""Claims ARXIV_ID, applies a replacement from FEED, completes with the claimed version; prints JSON."""
import json
import sys
import uuid
from datetime import timedelta

from app.db import crud, database
from app.services.sources import Metadata


async def _work(arxiv_id: str, feed: str) -> dict:
    await database.open_database()
    stored = await crud.get_paper_by_arxiv_id(arxiv_id)
    token = uuid.uuid4().hex
    paper = await crud.claim_paper(stored.id, token)
    newer = paper.version + 1
    replacement = Metadata(
        arxiv_id=arxiv_id,
        version=newer,
        title=f"{paper.title} (replaced)",
        authors=list(paper.authors),
        abstract=paper.abstract,
        pdf_url=f"https://arxiv.org/pdf/{arxiv_id}v{newer}",
        published_at=paper.published_at,
        updated_at=paper.published_at + timedelta(days=newer),
        categories=list(paper.categories),
        journal_ref=None,
        doi=None,
        comment=None,
    )
    replaced = await crud.apply_replacement(paper.id, replacement, feed)
    analysis = {
        "summary": f"Summary of {arxiv_id}.",
        "key_contributions": ["A contribution"],
        "methodology": "Method.",
        "limitations": None,
        "keywords": ["kw"],
        "novelty_score": 5,
    }
    completed = await crud.complete_paper(paper.id, token, paper.version, analysis)
    return {"claimed": paper.version, "replaced": replaced, "completed": completed}


def main() -> int:
    print(json.dumps(database.run(_work(sys.argv[1], sys.argv[2]))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
