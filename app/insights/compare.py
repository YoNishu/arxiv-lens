"""Check a result folder against its stats.json, and compare two result folders.

Both work on the files alone: no stage module, no database, no settings. check() hashes every
file stats.json names; diff() cuts the files it knows into rows or entries so that it can name
the first unit that differs and say, per stage, how much of the result moved.
"""
import fnmatch
import hashlib
import json
import os
import stat
from pathlib import Path

from app.cli import CliError

STAGES = ("snapshot", "text", "vocab", "vectors", "similar", "citations", "topics", "trends", "ranking", "digests")

# How diff() cuts a file: path pattern (one * per path segment), stage, and the fields whose
# values make the key of a row. None: the file is one unit. ENTRIES: the entries of "topics".
ENTRIES = "entries"
UNITS = (
    ("papers.jsonl", "snapshot", ("arxiv_id",)),
    ("text/tokens.jsonl", "text", ("arxiv_id",)),
    ("text/refs.jsonl", "text", ("arxiv_id",)),
    ("vocab.json", "vocab", None),
    ("vectors.jsonl", "vectors", ("arxiv_id",)),
    ("similar.jsonl", "similar", ("arxiv_id",)),
    ("citations/edges.jsonl", "citations", ("src", "dst")),
    ("citations/external.jsonl", "citations", ("arxiv_id",)),
    ("citations/nodes.jsonl", "citations", ("arxiv_id",)),
    ("citations/pagerank.jsonl", "citations", ("arxiv_id",)),
    ("centroids.json", "topics", None),
    ("assignments.jsonl", "topics", ("arxiv_id",)),
    ("topics.json", "topics", ENTRIES),
    ("trends/*.json", "trends", None),
    ("authors.json", "trends", None),
    ("ranking.jsonl", "ranking", ("feed", "week")),
    ("digests/*/*.md", "digests", None),
)

MAX_FILE_LINES = 20
_CHUNK = 1 << 20


def _files(folder: Path, skip: tuple[str, ...]) -> set[str]:
    """Relative paths, written with /, of the regular files under a folder."""
    found = set()
    top = os.fspath(folder)
    for parent, dirs, names in os.walk(top):
        dirs.sort()
        for name in names:
            full = os.path.join(parent, name)
            if not stat.S_ISREG(os.lstat(full).st_mode):
                continue
            relative = os.path.relpath(full, top).replace(os.sep, "/")
            if relative not in skip:
                found.add(relative)
    return found


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(_CHUNK)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def check(folder: Path) -> tuple[list[str], bool]:
    """Lines naming changed, missing and unknown files; True when nothing named is changed or missing."""
    stats_path = os.path.join(os.fspath(folder), "stats.json")
    try:
        with open(stats_path, "rb") as f:
            stats = json.loads(f.read())
    except FileNotFoundError:
        raise CliError("no result in %s" % folder, 1)
    named = stats.get("files", {})
    on_disk = _files(folder, ("stats.json", "runs.jsonl"))
    kinds: dict[str, str] = {}
    for relative, want in named.items():
        if relative not in on_disk:
            kinds[relative] = "missing"
            continue
        full = os.path.join(os.fspath(folder), *relative.split("/"))
        # The size is cheaper than the hash and settles most changes.
        if os.stat(full).st_size != want.get("bytes") or _sha256(full) != want.get("sha256"):
            kinds[relative] = "changed"
    for relative in on_disk:
        if relative not in named:
            kinds[relative] = "unknown"
    lines = ["%s\t%s" % (kinds[p], p) for p in sorted(kinds)]
    kinds_list = list(kinds.values())
    changed = kinds_list.count("changed")
    missing = kinds_list.count("missing")
    unknown = kinds_list.count("unknown")
    lines.append("checked %d files: %d changed, %d missing, %d unknown" % (len(named), changed, missing, unknown))
    return lines, changed + missing == 0


def _rule(relative: str):
    """(stage, how it is cut) for a path of the table, or None for a file the table does not know."""
    parts = relative.split("/")
    for pattern, stage, cut in UNITS:
        pattern_parts = pattern.split("/")
        if len(pattern_parts) == len(parts) and all(
            fnmatch.fnmatchcase(part, wanted) for part, wanted in zip(parts, pattern_parts)
        ):
            return stage, cut
    return None


def _key_value(value, fields):
    """The key of one row, or None when the row does not carry it."""
    if not isinstance(value, dict):
        return None
    key = []
    for field in fields:
        item = value.get(field)
        if not isinstance(item, str):
            return None
        key.append(item)
    return tuple(key)


def _units(data: bytes, cut) -> dict | None:
    """Key -> bytes of each unit of a file; None when the file cannot be cut as the table says."""
    if cut == ENTRIES:
        try:
            value = json.loads(data)
        except ValueError:
            return None
        entries = value.get("topics") if isinstance(value, dict) else None
        if not isinstance(entries, list):
            return None
        units = {}
        for entry in entries:
            number = entry.get("topic") if isinstance(entry, dict) else None
            if not isinstance(number, int) or isinstance(number, bool):
                return None
            text = json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            units[(number,)] = units.get((number,), b"") + text.encode("utf-8")
        return units
    lines = data.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    units = {}
    for line in lines:
        try:
            value = json.loads(line)
        except ValueError:
            return None
        key = _key_value(value, cut)
        if key is None:
            return None
        # A key that repeats keeps all its lines, so that a change in any of them shows.
        units[key] = units.get(key, b"") + line + b"\n"
    return units


def _printed(key: tuple, cut) -> str:
    if cut == ENTRIES:
        return "topic %d" % key[0]
    return " ".join(key)


def _read(path: Path) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _compare_file(a_path: Path | None, b_path: Path | None, cut) -> tuple[bool, int, int, str | None]:
    """(file differs, unit keys of both sides, units that differ, first differing key as printed)."""
    a_data = _read(a_path) if a_path is not None else None
    b_data = _read(b_path) if b_path is not None else None
    differs = a_data != b_data
    if cut is None:
        return differs, 1, 1 if differs else 0, None
    if not differs:
        units = _units(a_data, cut)
        return False, 1 if units is None else len(units), 0, None
    a_units = _units(a_data, cut) if a_data is not None else {}
    b_units = _units(b_data, cut) if b_data is not None else {}
    if a_units is None or b_units is None:
        return True, 1, 1, None
    keys = set(a_units) | set(b_units)
    differing = sorted(k for k in keys if a_units.get(k) != b_units.get(k))
    first = _printed(differing[0], cut) if differing else None
    return True, len(keys), len(differing), first


def diff(a: Path, b: Path) -> tuple[list[str], int]:
    """Lines naming the files that differ, one line per stage, a closing line; the number of files."""
    for folder in (a, b):
        if not os.path.isdir(folder):
            raise CliError("not a folder: %s" % folder, 2)
    a_files = _files(a, ("runs.jsonl",))
    b_files = _files(b, ("runs.jsonl",))
    units = {stage: 0 for stage in STAGES}
    differing_units = {stage: 0 for stage in STAGES}
    file_lines = []
    for relative in sorted(a_files | b_files):
        rule = _rule(relative)
        cut = rule[1] if rule is not None else None
        parts = relative.split("/")
        a_path = Path(a, *parts) if relative in a_files else None
        b_path = Path(b, *parts) if relative in b_files else None
        differs, n, d, first = _compare_file(a_path, b_path, cut)
        if rule is not None:
            units[rule[0]] += n
            differing_units[rule[0]] += d
        if not differs:
            continue
        if a_path is None:
            file_lines.append("only-b\t%s" % relative)
        elif b_path is None:
            file_lines.append("only-a\t%s" % relative)
        elif first is not None:
            file_lines.append("differ\t%s\t%s" % (relative, first))
        else:
            file_lines.append("differ\t%s" % relative)
    lines = file_lines[:MAX_FILE_LINES]
    if len(file_lines) > MAX_FILE_LINES:
        lines.append("... %d more" % (len(file_lines) - MAX_FILE_LINES))
    for stage in STAGES:
        lines.append("stage %s: %d of %d units differ" % (stage, differing_units[stage], units[stage]))
    lines.append("%d files differ" % len(file_lines))
    return lines, len(file_lines)
