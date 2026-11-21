"""The library report: twelve tables about a result folder, and an audit that recounts them.

write() reads every file of the folder once and the database once, and puts report.md and
audit.txt into a folder of their own. The tables describe the folder as the build left it; the
audit recounts what they read, against the database as it is now and against other files of the
same folder, so a folder that no longer matches the library, or a file changed by hand, shows
as a line marked differ. The report reads no clock: its "today" is the day of the build.
"""
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from app import home
from app.insights import config
from app.insights.asof import day_of, monday_of, week_label
from app.insights.trends import keyword_terms

logger = logging.getLogger(__name__)

TOP_TERMS = 8
TOP_CITED = 15
TOP_PAIRS = 10
LEADERS = 3
LEADER_WEEKS = 4
LAST_RUNS = 5

# The stages in the order a build runs them; runs.jsonl times each one under its name.
STAGE_NAMES = ("snapshot", "text", "vocab", "vectors", "similar", "citations", "topics", "trends", "ranking",
               "digests")

TITLES = ("Library by feed", "Weekly intake", "Vocabulary", "Rising terms", "Topics", "Topics by feed",
          "Most cited", "Citation flow", "Neighbour lists", "Versions and withdrawals", "Digest leaders",
          "Last runs")

# Spelled out rather than \s, so that what counts as white space does not depend on the Unicode
# tables of the Python that runs the report.
_SPACE = re.compile(r"[ \t\n\r\x0b\x0c]+")

STATES = ("analysed", "failed", "pending")


@dataclass
class FolderData:
    """Everything the report reads from a result folder, parsed once."""

    stats: dict
    papers: list[dict] = field(default_factory=list)
    by_id: dict[str, dict] = field(default_factory=dict)
    terms: dict[str, set] = field(default_factory=dict)
    refs: dict[str, dict] = field(default_factory=dict)
    vocab: dict = field(default_factory=dict)
    similar: dict[str, list] = field(default_factory=dict)
    edges: list[tuple[str, str]] = field(default_factory=list)
    external: list[dict] = field(default_factory=list)
    nodes: dict[str, dict] = field(default_factory=dict)
    pagerank: dict[str, float] = field(default_factory=dict)
    assigned: dict[str, tuple] = field(default_factory=dict)
    topics: list[dict] = field(default_factory=list)
    trends: dict[str, dict] = field(default_factory=dict)
    authors: dict = field(default_factory=dict)
    ranking: list[dict] = field(default_factory=list)
    runs: list[dict] = field(default_factory=list)
    # Per file named in stats.json: "<first 12 hex digits of its SHA-256> <bytes>", or "missing".
    on_disk: dict[str, str] = field(default_factory=dict)


def write(folder: Path, to: Path) -> tuple[int, int]:
    """Write report.md and audit.txt into `to`; return the number of recounts and how many differ."""
    from app.db import database

    folder = Path(folder)
    to = Path(to)
    if not (folder / "stats.json").is_file():
        _fail("no result in %s" % folder, 1)
    if _inside(to, folder):
        _fail("the report folder must not lie inside the result folder", 2)
    settings = config.load()
    data = _load(folder)
    db = database.run(_database())

    tables: dict = {}
    sections = [
        library(data, tables), intake(data, tables), vocabulary(data), rising(data), topics(data),
        topics_by_feed(data), most_cited(data), citation_flow(data), neighbours(data), versions(db),
        leaders(data), runs(data),
    ]
    recounts = audit(data, db, tables, settings)
    differing = 0
    for line in recounts:
        if line.startswith("differ\t"):
            differing += 1
    closing = "audit: %d recounts, %d differ" % (len(recounts), differing)

    home.write_file(to / "report.md", _markdown(data, sections, len(recounts), differing).encode("utf-8"))
    home.write_file(to / "audit.txt", ("\n".join(recounts + [closing]) + "\n").encode("utf-8"))
    logger.info("report written: %d recounts, %d differ", len(recounts), differing)
    return len(recounts), differing


def _fail(message: str, status: int):
    from app.cli import CliError

    raise CliError(message, status)


def _inside(to: Path, folder: Path) -> bool:
    """True when `to` is the result folder or lies under it, links resolved."""
    a = os.path.realpath(to)
    b = os.path.realpath(folder)
    return os.path.commonpath([a, b]) == b


def share(n: int, d: int) -> str:
    """n of d as a percentage with one decimal, rounded half up, in integers only."""
    if d == 0:
        return "-"
    t = (2000 * n + d) // (2 * d)
    return "%d.%d%%" % (t // 10, t % 10)


def median(values: list[int]) -> str:
    """The lower median, or - for no values."""
    if not values:
        return "-"
    return str(sorted(values)[(len(values) - 1) // 2])


def cell(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return format(value, ".2f")
    return _SPACE.sub(" ", str(value)).replace("|", "\\|")


def table(header: list[str], rows: list[list]) -> list[str]:
    lines = ["| " + " | ".join(cell(h) for h in header) + " |", "|" + "---|" * len(header)]
    for row in rows:
        lines.append("| " + " | ".join(cell(value) for value in row) + " |")
    return lines


def _captioned(caption: str, header: list[str], rows: list[list]) -> list[str]:
    return [caption, ""] + table(header, rows)


def _blocks(*blocks: list[str]) -> list[str]:
    lines: list[str] = []
    for block in blocks:
        if lines:
            lines.append("")
        lines.extend(block)
    return lines


def _fixed(value, digits: int) -> str | None:
    if value is None:
        return None
    return format(value, ".%df" % digits)


def _markdown(data: FolderData, sections: list[list[str]], recounts: int, differing: int) -> str:
    stats = data.stats
    inputs = stats["inputs"]
    as_of = stats["as_of"]
    feeds = " ".join(inputs["feeds"]) or "-"
    lines = [
        "# Library report",
        "",
        "As of %s (week %s, month %s)." % (as_of["day"], as_of["week"], as_of["month"]),
        "%d papers: %d analysed, %d failed, %d pending; %d withdrawn. Feeds: %s."
        % (inputs["papers"], inputs["analysed"], inputs["failed"], inputs["pending"], inputs["withdrawn"], feeds),
        "Settings %s." % stats["settings"][:12],
    ]
    for number, (title, blocks) in enumerate(zip(TITLES, sections), start=1):
        lines += ["", "## R%d %s" % (number, title), ""] + blocks
    lines += ["", "## Audit", "", "%d recounts, %d differ (audit.txt)." % (recounts, differing)]
    return "\n".join(lines) + "\n"


def _hash_and_size(data: bytes) -> str:
    return "%s %d" % (hashlib.sha256(data).hexdigest()[:12], len(data))


def _read_bytes(folder: Path, rel: str, seen: dict[str, str]) -> bytes:
    try:
        with open(folder / rel, "rb") as f:
            data = f.read()
    except OSError:
        _fail("cannot read %s in %s" % (rel, folder), 1)
    seen[rel] = _hash_and_size(data)
    return data


def _read_json(folder: Path, rel: str, seen: dict[str, str]):
    data = _read_bytes(folder, rel, seen)
    try:
        return json.loads(data.decode("utf-8"))
    except ValueError:
        _fail("cannot read %s in %s" % (rel, folder), 1)


def _read_jsonl(folder: Path, rel: str, seen: dict[str, str]) -> list:
    data = _read_bytes(folder, rel, seen)
    try:
        # A blank line holds no row.
        return [json.loads(line) for line in data.decode("utf-8").split("\n") if line.strip()]
    except ValueError:
        _fail("cannot read %s in %s" % (rel, folder), 1)


def _load(folder: Path) -> FolderData:
    """Every file of the folder the tables need, in the order a build writes them."""
    seen: dict[str, str] = {}
    data = FolderData(stats=_read_json(folder, "stats.json", seen))

    data.papers = sorted(_read_jsonl(folder, "papers.jsonl", seen), key=lambda row: row["arxiv_id"])
    data.by_id = {row["arxiv_id"]: row for row in data.papers}
    for row in _read_jsonl(folder, "text/tokens.jsonl", seen):
        data.terms[row["arxiv_id"]] = set(row["terms"])
    for row in _read_jsonl(folder, "text/refs.jsonl", seen):
        data.refs[row["arxiv_id"]] = row
    data.vocab = _read_json(folder, "vocab.json", seen)
    for row in _read_jsonl(folder, "similar.jsonl", seen):
        data.similar[row["arxiv_id"]] = row["similar"]
    data.edges = [(row["src"], row["dst"]) for row in _read_jsonl(folder, "citations/edges.jsonl", seen)]
    data.external = _read_jsonl(folder, "citations/external.jsonl", seen)
    for row in _read_jsonl(folder, "citations/nodes.jsonl", seen):
        data.nodes[row["arxiv_id"]] = row
    for row in _read_jsonl(folder, "citations/pagerank.jsonl", seen):
        data.pagerank[row["arxiv_id"]] = row["pagerank"]
    for row in _read_jsonl(folder, "assignments.jsonl", seen):
        data.assigned[row["arxiv_id"]] = (row["topic"], row["sim"])
    data.topics = _read_json(folder, "topics.json", seen)["topics"]
    for feed in data.stats["inputs"]["feeds"]:
        data.trends[feed] = _read_json(folder, "trends/%s.json" % feed, seen)
    data.authors = _read_json(folder, "authors.json", seen)
    data.ranking = _read_jsonl(folder, "ranking.jsonl", seen)
    if (folder / "runs.jsonl").exists():
        data.runs = _read_jsonl(folder, "runs.jsonl", {})

    # The audit compares every named file with stats.json; those no table reads are hashed here.
    for rel in sorted(data.stats["files"]):
        if rel not in seen:
            try:
                with open(folder / rel, "rb") as f:
                    seen[rel] = _hash_and_size(f.read())
            except FileNotFoundError:
                seen[rel] = "missing"
            except OSError:
                _fail("cannot read %s in %s" % (rel, folder), 1)
        data.on_disk[rel] = seen[rel]
    return data


def _utc_day(value: datetime | None) -> date | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).date()


async def _database() -> dict:
    """What the audit and the versions table need from the database, all from one state of it."""
    from sqlalchemy import and_, func, select

    from app.db import database
    from app.db.models import Analysis, Category, Paper, PaperCategory, PaperVersion

    await database.open_database()
    async with database.engine.connect() as conn:
        if conn.dialect.name == "sqlite":
            # The sqlite3 driver opens a transaction only before a write; without this BEGIN each
            # query below would see the database as it is at that moment.
            await conn.exec_driver_sql("BEGIN")
        else:
            conn = await conn.execution_options(isolation_level="REPEATABLE READ")
        try:
            current = and_(Analysis.paper_id == Paper.id, Analysis.paper_version == Paper.version)
            q1 = (await conn.execute(select(func.count()).select_from(Paper))).scalar_one()
            q2 = (await conn.execute(
                select(Category.arxiv_code, func.count())
                .select_from(PaperCategory)
                .join(Category, Category.id == PaperCategory.category_id)
                .group_by(Category.arxiv_code)
                .order_by(Category.arxiv_code)
            )).all()
            q3 = (await conn.execute(
                select(
                    Paper.arxiv_id, Paper.version, Paper.error_message.is_not(None).label("failed"),
                    Paper.withdrawn_at, Analysis.id.is_not(None).label("analysed"),
                )
                .select_from(Paper)
                .outerjoin(Analysis, current)
                .order_by(Paper.arxiv_id)
            )).all()
            q4 = (await conn.execute(select(func.count()).select_from(Analysis).join(Paper, current))).scalar_one()
            q5 = (await conn.execute(
                select(Paper.arxiv_id, PaperVersion.version, PaperVersion.seen_at)
                .select_from(PaperVersion)
                .join(Paper, Paper.id == PaperVersion.paper_id)
                .order_by(Paper.arxiv_id, PaperVersion.version)
            )).all()
        finally:
            await conn.rollback()

    papers = []
    for arxiv_id, version, failed, withdrawn_at, analysed in sorted(q3, key=lambda row: row[0]):
        # The state rule of the snapshot: an analysis of the current version, else an error text.
        if analysed:
            state = "analysed"
        elif failed:
            state = "failed"
        else:
            state = "pending"
        papers.append({"arxiv_id": arxiv_id, "version": version, "state": state, "withdrawn": _utc_day(withdrawn_at)})
    return {
        "papers": int(q1),
        "feeds": {code: int(n) for code, n in q2},
        "rows": papers,
        "analyses": int(q4),
        "versions": [(arxiv_id, version, _utc_day(seen_at))
                     for arxiv_id, version, seen_at in sorted(q5, key=lambda row: (row[0], row[1]))],
    }


def _feeds(data: FolderData) -> list[str]:
    codes: set[str] = set()
    for row in data.papers:
        codes.update(row["feeds"])
    return sorted(codes)


def _is_source(row: dict) -> bool:
    return not row["withdrawn"] and row["state"] == "analysed"


def _feeds_of(data: FolderData, arxiv_id: str) -> list[str]:
    row = data.by_id.get(arxiv_id)
    return row["feeds"] if row is not None else []


def _title(data: FolderData, arxiv_id: str) -> str | None:
    row = data.by_id.get(arxiv_id)
    return row["title"] if row is not None else None


def _week_of(text: str) -> str:
    return week_label(monday_of(day_of(text)))


def library(data: FolderData, tables: dict) -> list[str]:
    """R1: papers per feed by state, versions, cross-listing and resolved references."""
    rows = []
    papers_of: dict[str, int] = {}
    for feed in _feeds(data) + [None]:
        members = data.papers if feed is None else [row for row in data.papers if feed in row["feeds"]]
        states = {state: 0 for state in STATES}
        withdrawn = later = crossed = 0
        entries = []
        sources = resolved = 0
        for row in members:
            states[row["state"]] += 1
            withdrawn += 1 if row["withdrawn"] else 0
            later += 1 if row["version"] >= 2 else 0
            crossed += 1 if len(row["feeds"]) >= 2 else 0
            if row["state"] == "analysed" and row["arxiv_id"] in data.refs:
                entries.append(data.refs[row["arxiv_id"]]["entries"])
            if _is_source(row):
                sources += 1
                if data.nodes.get(row["arxiv_id"], {}).get("cites", 0) >= 1:
                    resolved += 1
        if feed is not None:
            papers_of[feed] = len(members)
        rows.append([feed if feed is not None else "all", len(members), states["analysed"], states["failed"],
                     states["pending"], withdrawn, later, crossed, median(entries), share(resolved, sources)])
    tables["library"] = papers_of
    return table(["feed", "papers", "analysed", "failed", "pending", "withdrawn", "later versions", "cross-listed",
                  "median refs", "with a resolved ref"], rows)


def intake(data: FolderData, tables: dict) -> list[str]:
    """R2: papers announced per week and feed."""
    today = day_of(data.stats["as_of"]["day"])
    running = data.stats["as_of"]["week"]
    groups: dict[tuple[str, str], list[dict]] = {}
    for row in data.papers:
        week = _week_of(row["announced"])
        for feed in row["feeds"]:
            groups.setdefault((week, feed), []).append(row)
    rows = []
    intake_of: dict[str, int] = {}
    for week, feed in sorted(groups):
        members = groups[(week, feed)]
        analysed = [row for row in members if row["state"] == "analysed"]
        total = 0
        scored = 0
        for row in analysed:
            if row["novelty"] is not None:
                total += row["novelty"]
                scored += 1
        mean = format(total / scored, ".2f") if scored else None
        label = week if week != running else "%s (open, day %d of 7)" % (week, today.isoweekday())
        rows.append([label, feed, len(members), len(analysed), mean])
        intake_of[feed] = intake_of.get(feed, 0) + len(members)
    tables["intake"] = intake_of
    return table(["week", "feed", "announced", "analysed", "mean novelty"], rows)


def vocabulary(data: FolderData) -> list[str]:
    """R3: the reference of the vocabulary, and the terms most listed papers of each feed hold."""
    reference = data.vocab["reference"]
    terms = data.vocab["terms"]
    sentence = "Reference month %s: %d papers, %d terms." % (reference["month"], reference["papers"], len(terms))
    if reference["whole_library"]:
        sentence += " The whole library is the reference."
    rows = []
    for feed in _feeds(data):
        counts: dict[str, int] = {}
        for row in data.papers:
            if row["withdrawn"] or feed not in row["feeds"]:
                continue
            for term in data.terms.get(row["arxiv_id"], ()):
                if term in terms:
                    counts[term] = counts.get(term, 0) + 1
        for term in sorted(counts, key=lambda t: (-counts[t], t))[:TOP_TERMS]:
            rows.append([feed, term, counts[term], terms[term]["df"], _fixed(terms[term]["idf"], 3)])
    return _blocks([sentence], table(["feed", "term", "papers", "df", "idf"], rows))


def rising(data: FolderData) -> list[str]:
    """R4: the rising terms of every feed, as the trends files list them."""
    rows = []
    for feed in _feeds(data):
        trend = data.trends.get(feed)
        if trend is None:
            continue
        for entry in trend["rising"]:
            rows.append([feed, entry["term"], entry["recent"], entry["baseline"], _fixed(entry["z"], 2)])
    return table(["feed", "term", "recent", "baseline", "z"], rows)


def topics(data: FolderData) -> list[str]:
    """R5: the topics with their momentum over all feeds and how close their papers sit."""
    momentum: dict[int, int] = {}
    for feed in sorted(data.trends):
        for entry in data.trends[feed]["topics"]:
            momentum[entry["topic"]] = momentum.get(entry["topic"], 0) + entry["slope"][0]
    totals: dict[int, float] = {}
    counts: dict[int, int] = {}
    for arxiv_id in sorted(data.assigned):
        topic, sim = data.assigned[arxiv_id]
        if topic is not None:
            totals[topic] = totals.get(topic, 0.0) + sim
            counts[topic] = counts.get(topic, 0) + 1
    rows = []
    for entry in sorted(data.topics, key=lambda t: t["topic"]):
        number = entry["topic"]
        cohesion = _fixed(totals[number] / counts[number], 3) if counts.get(number) else None
        rows.append([number, entry["name"], entry["source"], entry["size"], entry["recent"],
                     format(momentum.get(number, 0) / 35, "+.2f"), ", ".join(entry["terms"][:5]), cohesion])
    return table(["topic", "name", "source", "size", "recent", "momentum", "terms", "cohesion"], rows)


def topics_by_feed(data: FolderData) -> list[str]:
    """R6: listed papers per topic and feed."""
    feeds = _feeds(data)
    counts: dict[tuple, int] = {}
    for arxiv_id in sorted(data.assigned):
        row = data.by_id.get(arxiv_id)
        if row is None or row["withdrawn"]:
            continue
        topic = data.assigned[arxiv_id][0]
        for feed in row["feeds"]:
            counts[(topic, feed)] = counts.get((topic, feed), 0) + 1
    rows = []
    for entry in sorted(data.topics, key=lambda t: t["topic"]):
        rows.append(["%d %s" % (entry["topic"], entry["name"])] + [counts.get((entry["topic"], f), 0) for f in feeds])
    rows.append(["no topic"] + [counts.get((None, f), 0) for f in feeds])
    return table(["topic"] + feeds, rows)


def most_cited(data: FolderData) -> list[str]:
    """R7: the most cited papers of the library and outside it."""
    citers: dict[str, list[str]] = {}
    for src, dst in data.edges:
        citers.setdefault(dst, []).append(src)
    place = {}
    for number, arxiv_id in enumerate(sorted(data.pagerank, key=lambda p: (-data.pagerank[p], p)), start=1):
        place[arxiv_id] = number
    cited = sorted((node for node in data.nodes.values() if node["cited_by"] >= 1),
                   key=lambda node: (-node["cited_by"], node["arxiv_id"]))[:TOP_CITED]
    inside = []
    for node in cited:
        arxiv_id = node["arxiv_id"]
        feeds: set[str] = set()
        for src in citers.get(arxiv_id, ()):
            feeds.update(_feeds_of(data, src))
        inside.append([arxiv_id, _title(data, arxiv_id), node["cited_by"], place.get(arxiv_id),
                       " ".join(sorted(feeds)) or None])
    outside = [[row["arxiv_id"], row["cited_by"]]
               for row in sorted(data.external, key=lambda row: (-row["cited_by"], row["arxiv_id"]))[:TOP_CITED]]
    return _blocks(
        _captioned("Papers of the library:", ["arxiv_id", "title", "cited by", "pagerank place", "citing feeds"],
                   inside),
        _captioned("Papers the library does not hold:", ["arxiv_id", "cited by"], outside),
    )


def citation_flow(data: FolderData) -> list[str]:
    """R8: edges between feeds, and the pairs of papers most often cited together."""
    feeds = _feeds(data)
    flow: dict[tuple[str, str], int] = {}
    for src, dst in data.edges:
        for a in _feeds_of(data, src):
            for b in _feeds_of(data, dst):
                flow[(a, b)] = flow.get((a, b), 0) + 1
    matrix = [[a] + [flow.get((a, b), 0) for b in feeds] for a in feeds]
    pairs: dict[tuple[str, str], int] = {}
    for arxiv_id in sorted(data.nodes):
        for entry in data.nodes[arxiv_id]["cocited"]:
            other = entry["id"]
            key = (arxiv_id, other) if arxiv_id < other else (other, arxiv_id)
            pairs[key] = max(pairs.get(key, 0), entry["count"])
    top = sorted(pairs, key=lambda key: (-pairs[key], key[0], key[1]))[:TOP_PAIRS]
    return _blocks(
        _captioned("Edges from the feed of the citing paper (rows) to the feed of the cited paper (columns):",
                   ["from"] + feeds, matrix),
        _captioned("Papers most often cited together:", ["paper", "paper", "cited together"],
                   [[a, b, pairs[(a, b)]] for a, b in top]),
    )


def neighbours(data: FolderData) -> list[str]:
    """R9: lengths of the neighbour lists, and per feed how good and how mutual they are."""
    lengths: dict[int, int] = {}
    for arxiv_id in sorted(data.similar):
        n = len(data.similar[arxiv_id])
        lengths[n] = lengths.get(n, 0) + 1
    by_length = [[n, lengths[n]] for n in sorted(lengths)]
    rows = []
    for feed in _feeds(data) + [None]:
        owners = [row["arxiv_id"] for row in data.papers
                  if not row["withdrawn"] and (feed is None or feed in row["feeds"])
                  and data.similar.get(row["arxiv_id"])]
        best = 0.0
        mutual = 0
        entries = 0
        crossing = 0
        for owner in owners:
            listed = data.similar[owner]
            best += listed[0]["score"]
            back = data.similar.get(listed[0]["id"])
            if back and back[0]["id"] == owner:
                mutual += 1
            own = set(_feeds_of(data, owner))
            for entry in listed:
                entries += 1
                if not own.intersection(_feeds_of(data, entry["id"])):
                    crossing += 1
        mean = _fixed(best / len(owners), 3) if owners else None
        rows.append([feed if feed is not None else "all", len(owners), mean, share(mutual, len(owners)),
                     share(crossing, entries)])
    return _blocks(
        _captioned("Lists by length:", ["neighbours", "papers"], by_length),
        _captioned("By feed:", ["feed", "lists", "mean best score", "mutual best", "cross-feed"], rows),
    )


def versions(db: dict) -> list[str]:
    """R10: new versions and withdrawals per week, from the database."""
    weeks: dict[str, list[int]] = {}
    first: dict[str, dict[int, date]] = {}
    for arxiv_id, version, seen in db["versions"]:
        if seen is None:
            continue
        first.setdefault(arxiv_id, {})[version] = seen
        if version >= 2:
            weeks.setdefault(week_label(monday_of(seen)), [0, 0])[0] += 1
    for row in db["rows"]:
        if row["withdrawn"] is not None:
            weeks.setdefault(week_label(monday_of(row["withdrawn"])), [0, 0])[1] += 1
    rows = [[week, weeks[week][0], weeks[week][1]] for week in sorted(weeks)]
    days = []
    for arxiv_id in sorted(first):
        seen = first[arxiv_id]
        if 1 in seen and 2 in seen:
            days.append((seen[2] - seen[1]).days)
    return _blocks(
        table(["week", "new versions", "withdrawals"], rows),
        ["Median days from the first stored version to version 2: %s." % median(days)],
    )


def leaders(data: FolderData) -> list[str]:
    """R11: the leaders of the last closed weeks, and how often a week brings a new author."""
    running = data.stats["as_of"]["week"]
    rows = []
    for feed in _feeds(data):
        closed = sorted((row for row in data.ranking if row["feed"] == feed and row["week"] < running),
                        key=lambda row: row["week"])[-LEADER_WEEKS:]
        for bucket in closed:
            for paper in bucket["papers"][:LEADERS]:
                rows.append([feed, bucket["week"], paper["rank"], paper["arxiv_id"], _title(data, paper["arxiv_id"]),
                             _fixed(paper["score"], 2)])
    authors = data.authors
    weekly = []
    for i, week in enumerate(authors["weeks"]):
        weekly.append([week, authors["papers"][i], authors["with_new_author"][i],
                       share(authors["with_new_author"][i], authors["papers"][i])])
    return _blocks(
        _captioned("First three of the last four closed weeks:", ["feed", "week", "rank", "arxiv_id", "title", "score"],
                   rows),
        _captioned("Papers with a first-time author:", ["week", "papers", "with a new author", "share"], weekly),
    )


def runs(data: FolderData) -> list[str]:
    """R12: seconds and work counts of the last builds."""
    last = data.runs[-LAST_RUNS:]
    numbers = list(range(len(data.runs) - len(last) + 1, len(data.runs) + 1))
    seconds = []
    order: list[str] = []
    for number, run in zip(numbers, last):
        times = run.get("seconds", {})
        seconds.append([number, run.get("started"), run.get("kind"), run.get("as_of"), _fixed(times.get("total"), 3)]
                       + [_fixed(times.get(name), 3) for name in STAGE_NAMES])
        for name in sorted(run.get("work", {})):
            if name not in order:
                order.append(name)
    work = [[name] + [run.get("work", {}).get(name) for run in last] for name in order]
    return _blocks(
        _captioned("Seconds:", ["run", "started", "kind", "as of", "total"] + list(STAGE_NAMES), seconds),
        _captioned("Work:", ["counter"] + ["run %d" % number for number in numbers], work),
    )


def _recount(name: str, got, want, ok: bool | None = None) -> str:
    got, want = str(got), str(want)
    if ok is None:
        ok = got == want
    return "%s\t%s\t%s\t%s" % ("ok" if ok else "differ", name, got, want)


def audit(data: FolderData, db: dict, tables: dict, settings) -> list[str]:
    """One line per recount: verdict, name, what the tables read, what the recount found."""
    lines = []
    papers = data.papers
    rows = db["rows"]

    lines.append(_recount("papers", len(papers), db["papers"]))
    feeds = _feeds(data)
    for code in sorted(set(feeds) | set(db["feeds"])):
        lines.append(_recount("papers in %s" % code, tables["library"].get(code, 0), db["feeds"].get(code, 0)))
    for state in STATES:
        got = len([row for row in papers if row["state"] == state])
        want = len([row for row in rows if row["state"] == state])
        lines.append(_recount("state %s" % state, got, want))
    lines.append(_recount("withdrawn", len([row for row in papers if row["withdrawn"]]),
                          len([row for row in rows if row["withdrawn"] is not None])))
    lines.append(_recount("analyses of the current version", len([row for row in papers if row["state"] == "analysed"]),
                          db["analyses"]))
    lines.append(_recount("later versions", len([row for row in papers if row["version"] >= 2]),
                          len([row for row in rows if row["version"] >= 2])))

    nodes = [data.nodes[arxiv_id] for arxiv_id in sorted(data.nodes)]
    lines.append(_recount("edges against cited_by", len(data.edges), sum(node["cited_by"] for node in nodes)))  # ints
    lines.append(_recount("edges against cites", len(data.edges), sum(node["cites"] for node in nodes)))  # ints
    unplaced = len([arxiv_id for arxiv_id in data.assigned if data.assigned[arxiv_id][0] is None])
    sizes = sum(entry["size"] for entry in data.topics)  # ints
    lines.append(_recount("topic sizes and papers without topic", sizes + unplaced,
                          len([row for row in papers if not row["withdrawn"]])))

    disorder = too_long = own = gone = 0
    for owner in sorted(data.similar):
        listed = data.similar[owner]
        keys = [(-entry["score"], entry["id"]) for entry in listed]
        if any(keys[i] >= keys[i + 1] for i in range(len(keys) - 1)):
            disorder += 1
        if len(listed) > settings.similar.top_k:
            too_long += 1
        if any(entry["id"] == owner for entry in listed):
            own += 1
        if any(data.by_id.get(entry["id"]) is None or data.by_id[entry["id"]]["withdrawn"] for entry in listed):
            gone += 1
    lines.append(_recount("neighbour lists out of order", disorder, 0))
    lines.append(_recount("neighbour lists too long", too_long, 0))
    lines.append(_recount("neighbour lists naming their owner", own, 0))
    lines.append(_recount("neighbour lists naming a withdrawn paper", gone, 0))

    for feed in feeds:
        lines.append(_recount("intake of %s" % feed, tables["intake"].get(feed, 0), tables["library"].get(feed, 0)))

    lines.extend(_trend_recounts(data, settings.trends.recent_weeks))

    total = 0.0
    for arxiv_id in sorted(data.pagerank):
        total += data.pagerank[arxiv_id]
    want = 1.0 if papers else 0.0
    lines.append(_recount("pagerank total", format(total, ".9f"), format(want, ".9f"), abs(total - want) <= 1e-9))

    unordered = 0
    for bucket in data.ranking:
        members = bucket["papers"]
        keys = [(-paper["score"], paper["arxiv_id"]) for paper in members]
        ranks = [paper["rank"] for paper in members]
        if ranks != list(range(1, len(members) + 1)) or any(keys[i] >= keys[i + 1] for i in range(len(keys) - 1)):
            unordered += 1
    lines.append(_recount("buckets out of order", unordered, 0))

    named = data.stats["files"]
    for rel in sorted(named):
        recorded = "%s %d" % (named[rel]["sha256"][:12], named[rel]["bytes"])
        lines.append(_recount("file %s" % rel, data.on_disk[rel], recorded))
    lines.append(_recount("settings", data.stats["settings"][:12], config.digest(settings)[:12]))
    return lines


def _trend_recounts(data: FolderData, recent_weeks: int) -> list[str]:
    """The recent count of the first three rising terms of each feed, counted again from the papers.

    A term counts for an analysed, listed paper of the feed whose keywords spell it the way the
    trends do, published in one of the last recent_weeks weeks the feed's trends file names.
    """
    spelled: dict[str, tuple[set, str]] = {}
    for row in data.papers:
        if _is_source(row):
            spelled[row["arxiv_id"]] = (set(keyword_terms(row["keywords"])), _week_of(row["published"]))
    found = []
    for feed in sorted(data.trends):
        trend = data.trends[feed]
        window = set(trend["weeks"][-recent_weeks:])
        for entry in trend["rising"][:3]:
            term = entry["term"]
            n = 0
            for row in data.papers:
                if feed not in row["feeds"] or row["arxiv_id"] not in spelled:
                    continue
                terms, week = spelled[row["arxiv_id"]]
                if term in terms and week in window:
                    n += 1
            found.append(((feed, term), _recount("recent count of %s in %s" % (term, feed), entry["recent"], n)))
    return [line for _, line in sorted(found)]
