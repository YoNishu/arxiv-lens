"""Read side of a result folder for the API, the pages, the chat and MCP tools and two side tools.

Small files are parsed whole and kept until they change on disk (modification time or size).
The per-paper files are not held in memory: an index of byte offsets is kept for each, and a row
is read with one seek. Nothing here writes, opens the database or reads the settings.
"""
import copy
import json
import os
from pathlib import Path

from app import home


class NoResult(LookupError):
    """The folder has no stats.json."""


# absolute path of a file -> ((st_mtime_ns, st_size), parsed value)
_cache: dict[str, tuple[tuple[int, int], object]] = {}


def _root(folder) -> Path:
    return home.insights_dir() if folder is None else Path(folder)


def _cached(path, load):
    """The parsed value of a file, parsed again only when the file changed; None when it is missing."""
    key = os.path.abspath(path)
    try:
        st = os.stat(key)
    except FileNotFoundError:
        _cache.pop(key, None)
        return None
    signature = (st.st_mtime_ns, st.st_size)
    hit = _cache.get(key)
    if hit is not None and hit[0] == signature:
        return hit[1]
    value = load(key)
    _cache[key] = (signature, value)
    return value


def _load_json(path: str):
    with open(path, "rb") as f:
        return json.loads(f.read())


def _jsonl_rows(path: str):
    with open(path, "rb") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def _stats(root: Path) -> dict:
    value = _cached(root / "stats.json", _load_json)
    if value is None:
        raise NoResult("no result in %s" % root)
    return value


# Offset indexes of the per-paper files.

def _load_index(path: str) -> dict[str, tuple[int, int]]:
    index = {}
    offset = 0
    with open(path, "rb") as f:
        for line in f:
            row = _parse(line)
            if isinstance(row, dict) and isinstance(row.get("arxiv_id"), str):
                index[row["arxiv_id"]] = (offset, len(line))
            offset += len(line)
    return index


def _parse(data: bytes):
    try:
        return json.loads(data)
    except ValueError:
        return None


def _fetch(root: Path, name: str, ids: list[str]) -> dict[str, dict]:
    """Rows of a per-paper file by arxiv_id; ids without a row are left out."""
    path = os.path.abspath(root / name)
    found: dict[str, dict] = {}
    wanted = list(ids)
    for _ in range(2):
        index = _cached(path, _load_index)
        if not index:
            return found
        failed = []
        try:
            with open(path, "rb") as f:
                for arxiv_id in wanted:
                    place = index.get(arxiv_id)
                    if place is None:
                        continue
                    f.seek(place[0])
                    row = _parse(f.read(place[1]))
                    if isinstance(row, dict) and row.get("arxiv_id") == arxiv_id:
                        found[arxiv_id] = row
                    else:
                        failed.append(arxiv_id)
        except FileNotFoundError:
            failed = [i for i in wanted if i not in found]
        if not failed:
            break
        # The file was replaced after its index was made: index it again, once.
        _cache.pop(path, None)
        wanted = failed
    return found


def _titles(root: Path, ids: list[str]) -> dict[str, str | None]:
    rows = _fetch(root, "papers.jsonl", ids)
    return {i: rows[i].get("title") if i in rows else None for i in ids}


# Files parsed whole.

def _load_topics(path: str):
    value = _load_json(path)
    entries = value.get("topics") or []
    names = {t["topic"]: t.get("name") for t in entries}
    return entries, names


def _topics(root: Path):
    value = _cached(root / "topics.json", _load_topics)
    return value if value is not None else ([], {})


def _load_ranking(path: str):
    rows: dict[tuple[str, str], dict] = {}
    weeks: dict[str, list[str]] = {}
    for row in _jsonl_rows(path):
        rows[(row["feed"], row["week"])] = row
        weeks.setdefault(row["feed"], []).append(row["week"])
    for feed in weeks:
        weeks[feed] = sorted(set(weeks[feed]))
    return rows, weeks


def _ranking(root: Path):
    value = _cached(root / "ranking.jsonl", _load_ranking)
    return value if value is not None else ({}, {})


def _load_edges(path: str):
    cites: dict[str, list[str]] = {}
    citers: dict[str, list[str]] = {}
    for row in _jsonl_rows(path):
        cites.setdefault(row["src"], []).append(row["dst"])
        citers.setdefault(row["dst"], []).append(row["src"])
    for lists in (cites, citers):
        for key in lists:
            lists[key] = sorted(set(lists[key]))
    return cites, citers


def _edges(root: Path):
    value = _cached(root / "citations" / "edges.jsonl", _load_edges)
    return value if value is not None else ({}, {})


def _load_assignments(path: str):
    placed: dict[str, tuple[int | None, float]] = {}
    for row in _jsonl_rows(path):
        placed[row["arxiv_id"]] = (row.get("topic"), row.get("sim"))
    members: dict[int, list[str]] = {}
    for arxiv_id in sorted(placed):
        number = placed[arxiv_id][0]
        if number is not None:
            members.setdefault(number, []).append(arxiv_id)
    for number in members:
        members[number].sort(key=lambda i: (-placed[i][1], i))
    return placed, members


def _assignments(root: Path):
    value = _cached(root / "assignments.jsonl", _load_assignments)
    return value if value is not None else ({}, {})


# The functions the surfaces call.

def stats(folder=None) -> dict:
    """The content of stats.json; NoResult when the folder has none."""
    return copy.deepcopy(_stats(_root(folder)))


def paper(arxiv_id: str, folder=None) -> dict | None:
    root = _root(folder)
    _stats(root)
    return _fetch(root, "papers.jsonl", [arxiv_id]).get(arxiv_id)


def topics(folder=None) -> list[dict]:
    root = _root(folder)
    _stats(root)
    return copy.deepcopy(_topics(root)[0])


def topic(number: int, folder=None) -> dict | None:
    root = _root(folder)
    _stats(root)
    entry = next((t for t in _topics(root)[0] if t.get("topic") == number), None)
    if entry is None:
        return None
    placed, members = _assignments(root)
    ids = members.get(number, [])
    titles = _titles(root, ids)
    result = copy.deepcopy(entry)
    result["members"] = [{"arxiv_id": i, "title": titles[i], "sim": placed[i][1]} for i in ids]
    return result


def trends(feed: str, folder=None) -> dict | None:
    root = _root(folder)
    if feed not in _stats(root).get("inputs", {}).get("feeds", []):
        return None
    value = _cached(root / "trends" / (feed + ".json"), _load_json)
    if value is None:
        return None
    names = _topics(root)[1]
    result = copy.deepcopy(value)
    for entry in result.get("topics", []):
        entry["name"] = names.get(entry.get("topic"))
    return result


def weeks(feed: str, folder=None) -> list[str]:
    root = _root(folder)
    _stats(root)
    return list(_ranking(root)[1].get(feed, []))


def digest(feed: str, week: str, folder=None) -> dict | None:
    root = _root(folder)
    as_of = _stats(root)["as_of"]
    row = _ranking(root)[0].get((feed, week))
    if row is None:
        return None
    members = sorted(row.get("papers", []), key=lambda m: (m["rank"], m["arxiv_id"]))
    ids = [m["arxiv_id"] for m in members]
    rows = _fetch(root, "papers.jsonl", ids)
    placed = _assignments(root)[0]
    names = _topics(root)[1]
    papers = []
    for member in members:
        found = rows.get(member["arxiv_id"], {})
        number = placed.get(member["arxiv_id"], (None, 0.0))[0]
        entry = dict(member)
        entry["title"] = found.get("title")
        entry["authors"] = list(found.get("authors", []))
        entry["topic"] = names.get(number) if number is not None else None
        papers.append(entry)
    page_path = root / "digests" / feed / (week + ".md")
    try:
        with open(page_path, "rb") as f:
            page = f.read().decode("utf-8")
    except FileNotFoundError:
        page = ""
    return {
        "feed": feed,
        "week": week,
        "closed": week < as_of["week"],
        "ranked": len(members),
        "papers": papers,
        "page": page,
    }


def related(arxiv_id: str, folder=None) -> dict | None:
    """Everything the result says about one paper: its topic, similar papers and citations."""
    root = _root(folder)
    _stats(root)
    row = _fetch(root, "papers.jsonl", [arxiv_id]).get(arxiv_id)
    if row is None:
        return None
    placed = _assignments(root)[0]
    number = placed.get(arxiv_id, (None, 0.0))[0]
    topic_value = None
    if number is not None:
        topic_value = {"topic": number, "name": _topics(root)[1].get(number)}
    similar_row = _fetch(root, "similar.jsonl", [arxiv_id]).get(arxiv_id, {})
    neighbours = similar_row.get("similar", [])
    cites_map, citers_map = _edges(root)
    cites = cites_map.get(arxiv_id, [])
    cited_by = citers_map.get(arxiv_id, [])
    node = _fetch(root, "citations/nodes.jsonl", [arxiv_id]).get(arxiv_id, {})
    titles = _titles(root, sorted(set([n["id"] for n in neighbours] + cites + cited_by)))
    return {
        "arxiv_id": arxiv_id,
        "title": row.get("title"),
        "state": row.get("state"),
        "withdrawn": row.get("withdrawn"),
        "feeds": row.get("feeds"),
        "topic": topic_value,
        "similar": [{"arxiv_id": n["id"], "title": titles[n["id"]], "score": n["score"]} for n in neighbours],
        "cites": [{"arxiv_id": i, "title": titles[i]} for i in cites],
        "cited_by": [{"arxiv_id": i, "title": titles[i]} for i in cited_by],
        "cited_by_count": node.get("cited_by", 0),
        "external": node.get("external", 0),
    }
