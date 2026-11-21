"""The command line: python -m app [-v] COMMAND [arguments].

Everything the web server does to a home can be done from here as well: set up the database, set
the clock, run a cycle, play a corpus day by day, manage categories, look at a paper and call a
chat or MCP tool. Results go to stdout; an expected failure is one "error:" line on stderr. The
status is 0 when done, 1 when the answer is no, 2 for a usage or environment problem.
"""
import argparse
import json
import logging
import os
import re
import shlex
import sys
from datetime import date, datetime, timedelta, timezone

from app import clock, home, ids

logger = logging.getLogger(__name__)

_DAY = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
_LABEL_WIDTH = 12
_KIND_WIDTH = 9


class CliError(Exception):
    """An expected failure: main() prints it as one error line and ends with its status."""

    def __init__(self, message: str, status: int = 1):
        super().__init__(message)
        self.message = message
        self.status = status


def main(argv: list[str] | None = None) -> int:
    """Run one command and return its exit status. Safe to call again in the same process."""
    _utf8_output()
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        # argparse has printed its usage text already; callers such as play --then want the status.
        return e.code if isinstance(e.code, int) else 2
    root = logging.getLogger()
    level = root.level
    handler = None
    if not any(isinstance(h, _StderrHandler) for h in root.handlers):
        handler = _StderrHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
    root.setLevel(logging.INFO if args.verbose else logging.WARNING)
    try:
        return args.func(args)
    except CliError as e:
        print("error: %s" % e.message, file=sys.stderr)
        return e.status
    finally:
        # A command run by play --then must not change how the rest of the play logs.
        root.setLevel(level)
        if handler is not None:
            root.removeHandler(handler)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app", description="ArxivLens on the command line.")
    parser.add_argument("-v", dest="verbose", action="store_true", help="log at INFO instead of WARNING")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    db = _group(sub, "db", "the database of the home")
    db.add_parser("upgrade", help="apply the schema steps the database lacks").set_defaults(func=_db_upgrade)
    db.add_parser("info", help="schema step and row count of every table").set_defaults(func=_db_info)

    clk = _group(sub, "clock", "the simulated time of the home")
    clk.add_parser("show", help="the current instant and where it comes from").set_defaults(func=_clock_show)
    p = clk.add_parser("set", help="write the clock file")
    p.add_argument("instant", metavar="INSTANT", help="YYYY-MM-DD, YYYY-MM-DDTHH:MM or YYYY-MM-DDTHH:MM:SS, Z optional")
    p.set_defaults(func=_clock_set)
    p = clk.add_parser("advance", help="move the clock file forward")
    p.add_argument("duration", metavar="DURATION", help="90s, 15m, 2h or 3d")
    p.set_defaults(func=_clock_advance)

    sub.add_parser("cycle", help="poll every feed once and analyse what is pending").set_defaults(func=_cycle)

    p = sub.add_parser("play", help="feed a corpus into the home day by day, one cycle a day")
    p.add_argument("--corpus", metavar="DIR", required=True, help="a corpus folder (it holds corpus.json)")
    days = p.add_mutually_exclusive_group(required=True)
    days.add_argument("--until", metavar="DAY", help="last day to play")
    days.add_argument("--day", metavar="DAY", help="play this one day")
    p.add_argument("--from", dest="start", metavar="DAY", help="first day to play (default: the corpus start)")
    p.add_argument("--fast", action="store_true", help="answer from the corpus in-process instead of over HTTP")
    p.add_argument("--then", metavar="ARGS", help="a command to run after every day, such as \"insights build\"")
    p.set_defaults(func=_play)

    cat = _group(sub, "category", "subscribed categories")
    p = cat.add_parser("add", help="subscribe to a category")
    p.add_argument("code", metavar="CODE")
    p.add_argument("--name", metavar="NAME", help="display name (default: the code)")
    p.set_defaults(func=_category_add)
    p = cat.add_parser("remove", help="unsubscribe; papers no other category lists are deleted")
    p.add_argument("code", metavar="CODE")
    p.set_defaults(func=_category_remove)
    cat.add_parser("list", help="id, code, linked papers and name of every category").set_defaults(func=_category_list)

    pap = _group(sub, "paper", "stored papers")
    p = pap.add_parser("show", help="the stored fields and the timeline of a paper")
    p.add_argument("arxiv_id", metavar="ARXIV_ID")
    p.set_defaults(func=_paper_show)
    p = pap.add_parser("retry", help="make a failed paper pending again")
    p.add_argument("arxiv_id", metavar="ARXIV_ID")
    p.set_defaults(func=_paper_retry)

    p = sub.add_parser("tool", help="call a chat tool, or an MCP tool with --mcp")
    p.add_argument("--mcp", action="store_true", help="call the MCP tool of that name")
    p.add_argument("--url", metavar="URL", help="with --mcp: the served MCP endpoint to call instead of the one in-process")
    p.add_argument("name", metavar="NAME")
    p.add_argument("json", metavar="JSON", nargs="?", default="{}", help="the arguments as a JSON object")
    p.set_defaults(func=_tool)

    # The insights commands belong to their package; its cli module imports only light modules at
    # its top, so building the parser stays cheap.
    from app.insights import cli as insights_cli
    insights_cli.register(sub)
    return parser


def _group(sub, name: str, help_text: str):
    parser = sub.add_parser(name, help=help_text)
    return parser.add_subparsers(dest=name + "_command", metavar="COMMAND", required=True)


def _db_upgrade(args) -> int:
    applied, version = _run(_upgrade)
    for number, name in applied:
        print("step %d %s" % (number, name))
    print("schema %d" % version)
    return 0


async def _upgrade():
    from app.db import database, migrate

    applied = await database.open_database()
    async with database.engine.connect() as conn:
        version = await conn.run_sync(migrate.current_version)
    return applied, version


def _db_info(args) -> int:
    for name, value in _run(_table_counts):
        print("%s=%d" % (name, value))
    return 0


async def _table_counts() -> list[tuple[str, int]]:
    from sqlalchemy import func, select

    from app.db import database, migrate
    from app.db.models import Base

    await database.open_database()

    def count(conn):
        rows = [("schema", migrate.current_version(conn))]
        for name in sorted(Base.metadata.tables):
            table = Base.metadata.tables[name]
            rows.append((name, conn.execute(select(func.count()).select_from(table)).scalar_one()))
        return rows

    async with database.engine.connect() as conn:
        return await conn.run_sync(count)


def _clock_show(args) -> int:
    instant = _now()
    print(clock.format_instant(instant), clock.source())
    return 0


def _clock_set(args) -> int:
    instant = _parse(clock.parse_instant, args.instant)
    home.ensure()
    clock.set_to(instant)
    print(clock.format_instant(_now()))
    return 0


def _clock_advance(args) -> int:
    seconds = _parse(clock.parse_duration, args.duration)
    home.ensure()
    instant = _parse(clock.advance, seconds)
    print(clock.format_instant(instant))
    return 0


def _cycle(args) -> int:
    run, failed = _run(_one_cycle)
    for feed, error in failed:
        print("feed %s: %s" % (feed, error), file=sys.stderr)
    print(_run_line(run))
    if run.error_message:
        raise CliError(run.error_message, 1)
    return 0


async def _one_cycle():
    from sqlalchemy import select

    from app.db import database
    from app.db.models import RunFeed
    from app.services import sources
    from app.services.cycle import run_cycle

    await database.open_database()
    # Made here rather than inside the cycle, so that a home whose corpus has gone away is
    # reported as such before a run is recorded.
    run = await run_cycle(sources.current())
    async with database.async_session() as s:
        rows = await s.execute(
            select(RunFeed.feed, RunFeed.error_message)
            .where(RunFeed.run_id == run.id, RunFeed.error_message.is_not(None))
            .order_by(RunFeed.feed)
        )
        return run, [(feed, error) for feed, error in rows]


def _play(args) -> int:
    folder = args.corpus
    start, end = _corpus_range(folder)
    if args.day is not None:
        if args.start is not None:
            raise CliError("--from cannot be combined with --day", 2)
        first = last = _parse(_parse_day, args.day)
    else:
        first = _parse(_parse_day, args.start) if args.start is not None else start
        last = _parse(_parse_day, args.until)
    for day in (first, last):
        if not start <= day <= end:
            raise CliError("%s is outside the corpus (%s to %s)" % (day.isoformat(), start.isoformat(), end.isoformat()), 2)
    if first > last:
        raise CliError("nothing to play", 2)

    home.ensure()
    # Later cycles and the server read the marker, so a plain `cycle` keeps using this corpus.
    marker = {"fast": bool(args.fast), "path": os.path.abspath(folder)}
    home.write_file(home.corpus_file(), _canonical(marker))
    from app.services import sources

    world = sources.current()
    day = first
    while day <= last:
        clock.set_to(datetime(day.year, day.month, day.day, 8, 0, 0, tzinfo=timezone.utc))
        logger.info("Playing %s", day.isoformat())
        run = _run(_played_cycle, world)
        print("%s %s" % (day.isoformat(), _run_line(run)))
        if run.error_message:
            raise CliError(run.error_message, 1)
        if args.then is not None:
            sys.stdout.flush()
            status = main(shlex.split(args.then))
            if status != 0:
                return status
        day += timedelta(days=1)
    return 0


async def _played_cycle(world):
    from app.db import database
    from app.services.cycle import run_cycle

    await database.open_database()
    return await run_cycle(world)


def _corpus_range(folder: str) -> tuple[date, date]:
    try:
        with open(os.path.join(folder, "corpus.json"), "rb") as f:
            data = f.read()
    except OSError:
        raise CliError("corpus not found: %s" % folder, 2) from None
    try:
        manifest = json.loads(data.decode("utf-8"))
        return _parse_day(manifest["start"]), _parse_day(manifest["end"])
    except (ValueError, KeyError, TypeError):
        raise CliError("corpus not readable: %s" % folder, 2) from None


def _run_line(run) -> str:
    return "run %d: new=%d updated=%d skipped=%d processed=%d failed=%d" % (
        run.id, run.new_papers, run.updated, run.skipped, run.processed, run.failed,
    )


def _category_add(args) -> int:
    code = args.code.strip()
    if not ids.is_category_code(code):
        raise CliError("not a category code: %s" % args.code, 2)
    _run(_add_category, code, args.name or code)
    print("added %s" % code)
    return 0


async def _add_category(code: str, name: str) -> None:
    from app.db import crud, database

    await database.open_database()
    if await crud.get_category_by_code(code) is not None:
        raise CliError("already subscribed: %s" % code, 1)
    await crud.create_category(name=name, arxiv_code=code)


def _category_remove(args) -> int:
    code = args.code.strip()
    deleted, kept = _run(_remove_category, code)
    print("removed %s: %d papers deleted, %d kept" % (code, deleted, kept))
    return 0


async def _remove_category(code: str) -> tuple[int, int]:
    from app.db import crud, database

    await database.open_database()
    category = await crud.get_category_by_code(code)
    outcome = None if category is None else await crud.delete_category(category.id)
    if outcome is None:
        raise CliError("not subscribed: %s" % code, 1)
    return outcome


def _category_list(args) -> int:
    for category_id, code, papers, name in _run(_categories):
        print("%d\t%s\t%d\t%s" % (category_id, code, papers, name))
    return 0


async def _categories() -> list[tuple]:
    from sqlalchemy import func, select

    from app.db import database
    from app.db.models import Category, PaperCategory

    await database.open_database()
    async with database.async_session() as s:
        rows = await s.execute(
            select(Category.id, Category.arxiv_code, func.count(PaperCategory.paper_id), Category.name)
            .outerjoin(PaperCategory, PaperCategory.category_id == Category.id)
            .group_by(Category.id, Category.arxiv_code, Category.name)
            .order_by(Category.id)
        )
        return [tuple(row) for row in rows]


def _paper_show(args) -> int:
    arxiv_id = args.arxiv_id.strip()
    paper, feeds, text, events = _run(_paper_facts, arxiv_id)
    analysis = paper.analysis
    fields = [
        ("arxiv_id", paper.arxiv_id),
        ("version", str(paper.version)),
        ("title", paper.title),
        ("authors", "; ".join(paper.authors or [])),
        ("categories", " ".join(paper.categories or [])),
        ("feeds", " ".join(feeds)),
        ("published", _instant(paper.published_at)),
        ("updated", _instant(paper.updated_at)),
        ("stored", _instant(paper.created_at)),
        ("status", str(getattr(paper.status, "value", paper.status))),
        ("attempts", str(paper.attempts)),
        ("withdrawn", _instant(paper.withdrawn_at)),
        ("error", paper.error_message),
        ("analysis", "v%d novelty %s" % (analysis.paper_version, analysis.novelty_score) if analysis else None),
        ("text", "v%d %d chars %s" % (text.version, text.chars, text.extraction) if text else None),
    ]
    for label, value in fields:
        print(label.ljust(_LABEL_WIDTH) + ((value or "").rstrip() or "-"))
    print("timeline")
    for event in events:
        line = "%s  %s  v%d" % (clock.format_instant(event.at), event.kind.ljust(_KIND_WIDTH), event.version)
        if event.feed:
            line += "  " + event.feed
        print(line)
    return 0


async def _paper_facts(arxiv_id: str):
    from sqlalchemy import select

    from app.db import crud, database, history
    from app.db.models import Category, PaperCategory, PaperText

    await database.open_database()
    paper = await crud.get_paper_by_arxiv_id(arxiv_id)
    if paper is None:
        raise CliError("no such paper: %s" % arxiv_id, 1)
    async with database.async_session() as s:
        feeds = (
            await s.execute(
                select(Category.arxiv_code)
                .join(PaperCategory, PaperCategory.category_id == Category.id)
                .where(PaperCategory.paper_id == paper.id)
                .order_by(Category.arxiv_code)
            )
        ).scalars().all()
        text = (await s.execute(select(PaperText).where(PaperText.paper_id == paper.id))).scalar_one_or_none()
    events = await history.timeline(paper.id)
    return paper, list(feeds), text, events


def _paper_retry(args) -> int:
    arxiv_id = args.arxiv_id.strip()
    _run(_retry, arxiv_id)
    print("queued %s" % arxiv_id)
    return 0


async def _retry(arxiv_id: str) -> None:
    from app.db import crud, database

    await database.open_database()
    paper = await crud.get_paper_by_arxiv_id(arxiv_id)
    outcome = "not_found" if paper is None else await crud.retry_paper(paper.id)
    if outcome == "not_found":
        raise CliError("no such paper: %s" % arxiv_id, 1)
    if outcome != "ok":
        status = getattr(paper.status, "value", paper.status)
        raise CliError("%s is %s: only a failed paper can be retried" % (arxiv_id, status), 1)


def _tool(args) -> int:
    try:
        values = json.loads(args.json)
    except ValueError:
        values = None
    if not isinstance(values, dict):
        raise CliError("not a JSON object: %s" % args.json, 2)
    if args.url and not args.mcp:
        raise CliError("--url needs --mcp", 2)
    if args.mcp:
        text = _run(_mcp_tool, args.name, values, args.url)
    else:
        text = _run(_chat_tool, args.name, values)
    sys.stdout.write(text.rstrip("\n") + "\n")
    return 0


async def _chat_tool(name: str, values: dict) -> str:
    from app.db import database
    from app.services import chat_agent

    if name not in [tool["function"]["name"] for tool in chat_agent.TOOLS]:
        raise CliError("unknown tool: %s" % name, 1)
    await database.open_database()
    return await chat_agent._exec_tool(name, values)


async def _mcp_tool(name: str, values: dict, url: str | None) -> str:
    from fastmcp import Client

    if url:
        target = url
    else:
        from app.db import database
        from app.mcp.server import mcp

        await database.open_database()
        target = mcp
    async with Client(target) as client:
        if name not in [tool.name for tool in await client.list_tools()]:
            raise CliError("unknown tool: %s" % name, 1)
        result = await client.call_tool(name, values)
    # Older fastmcp releases hand back the list of parts itself instead of a result object.
    parts = getattr(result, "content", result)
    return "\n".join(part.text for part in parts if getattr(part, "text", None) is not None)


def _run(work, *args):
    """Run one coroutine function against the home's database, after checking the clock."""
    _now()
    from app.db import database

    return database.run(work(*args))


def _now() -> datetime:
    try:
        return clock.now()
    except ValueError as e:
        # A clock file or ARXIVLENS_CLOCK that does not parse is an environment error, not a crash.
        raise CliError(str(e), 2) from None


def _parse(parse, value):
    try:
        return parse(value)
    except ValueError as e:
        raise CliError(str(e), 2) from None


def _parse_day(text: str) -> date:
    match = _DAY.fullmatch(text)
    if match is None:
        raise ValueError("not a day: %s" % text)
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        raise ValueError("not a day: %s" % text) from None


def _instant(value: datetime | None) -> str | None:
    return clock.format_instant(value) if value is not None else None


def _canonical(value) -> bytes:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return (text + "\n").encode("utf-8")


def _utf8_output() -> None:
    # Titles and author names hold characters outside ASCII; what is printed must not depend on
    # the locale of the terminal.
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
        if encoding != "utf8" and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


class _StderrHandler(logging.StreamHandler):
    """A log handler that writes to whatever sys.stderr is at the moment a line is logged.

    main() runs again inside play --then and under drivers that capture sys.stderr; a handler
    bound to the stream of an earlier call would keep writing there.
    """

    @property
    def stream(self):
        return sys.stderr

    @stream.setter
    def stream(self, value):
        pass
