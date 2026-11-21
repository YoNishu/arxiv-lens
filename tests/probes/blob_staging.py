"""Polls, then processes paper ARXIV_ID with blob staging recorded instead of done; prints JSON."""
import json
import sys

from app.db import crud, database
from app.services import feed_poller, pdf_extractor, processor, sources

calls = []


async def _upload(data: bytes, blob_name: str) -> str:
    calls.append(["upload", blob_name, isinstance(data, bytes) and data.startswith(b"%PDF")])
    return f"blob://{blob_name}"


async def _delete(blob_name: str) -> None:
    calls.append(["delete", blob_name])


async def _work(arxiv_id: str) -> str:
    await database.open_database()
    world = sources.current()
    await feed_poller.poll_all(world)
    paper = await crud.get_paper_by_arxiv_id(arxiv_id)
    return await processor.process_paper(paper.id, world)


def main() -> int:
    pdf_extractor.upload_to_blob = _upload
    pdf_extractor.delete_blob = _delete
    outcome = database.run(_work(sys.argv[1]))
    print(json.dumps({"calls": calls, "outcome": outcome}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
