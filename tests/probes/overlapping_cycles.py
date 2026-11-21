"""Runs four whole cycles at the same moment in one event loop; prints the totals of each run."""
import asyncio
import json

from app.db import database
from app.services.cycle import run_cycle


async def _work() -> list[list[int]]:
    await database.open_database()
    runs = await asyncio.gather(*[run_cycle() for _ in range(4)])
    return [[run.new_papers, run.processed, run.failed] for run in runs]


def main() -> int:
    print(json.dumps(database.run(_work())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
