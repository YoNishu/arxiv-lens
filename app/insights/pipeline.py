"""The insights build: ten stages from a snapshot of the database to the weekly digest pages.

build() reads the settings and the clock once, runs the stages in their fixed order on one
Context, then writes stats.json, which names every file the stages wrote with its size and
hash, and appends one line to runs.jsonl with the seconds and the work counts of the run.
read_stats() and last_run() read those two files back for the show commands.
"""
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from app import clock, home
from app.insights import asof, canon, config, work
from app.insights import citations, digests, ranking, similar, snapshot, textproc, topics, trends, vectors, vocab
from app.insights.asof import AsOf
from app.insights.config import Settings

logger = logging.getLogger(__name__)

STAGES = (("snapshot", snapshot), ("text", textproc), ("vocab", vocab), ("vectors", vectors), ("similar", similar),
          ("citations", citations), ("topics", topics), ("trends", trends), ("ranking", ranking), ("digests", digests))


@dataclass
class Context:
    """What one build carries from stage to stage, and the files it has written so far."""

    asof: AsOf
    settings: Settings
    out: Path
    files: dict[str, dict] = field(default_factory=dict)
    papers: list[dict] = field(default_factory=list)
    tokens: dict[str, dict] = field(default_factory=dict)
    refs: dict[str, dict] = field(default_factory=dict)
    reference: list[str] = field(default_factory=list)
    idf: dict[str, float] = field(default_factory=dict)
    vectors: dict[str, list] = field(default_factory=dict)
    cites: dict[str, list[str]] = field(default_factory=dict)
    citers: dict[str, list[str]] = field(default_factory=dict)
    topic_of: dict[str, int | None] = field(default_factory=dict)
    topic_names: list[str] = field(default_factory=list)
    buckets: list[dict] = field(default_factory=list)

    def write_json(self, rel: str, value) -> None:
        self._record(rel, canon.write_json(self.out / rel, value))

    def write_jsonl(self, rel: str, rows: list[dict]) -> None:
        self._record(rel, canon.write_jsonl(self.out / rel, rows))

    def write_text(self, rel: str, text: str) -> None:
        self._record(rel, canon.write_text(self.out / rel, text))

    def prune(self, top: str) -> None:
        """Make out/<top> hold what this build wrote there and nothing else.

        The names of the files under trends/ and digests/ depend on the data, so a feed or a
        week that is gone would otherwise leave its files behind from an earlier build.
        """
        base = self.out / top
        if not base.is_dir():
            return
        files = []
        folders = []
        for parent, dirnames, filenames in os.walk(base):
            for name in dirnames:
                full = os.path.join(parent, name)
                # A link to a folder is removed like a file; os.walk does not go into it.
                (files if os.path.islink(full) else folders).append(self._rel(full))
            for name in filenames:
                files.append(self._rel(os.path.join(parent, name)))
        for rel in sorted(files):
            if rel not in self.files:
                os.unlink(self.out / rel)
        for rel in sorted(folders, key=lambda rel: (-rel.count("/"), rel)):
            if not os.listdir(self.out / rel):
                os.rmdir(self.out / rel)
        if not os.listdir(base):
            os.rmdir(base)

    def _rel(self, full: str) -> str:
        return os.path.relpath(full, self.out).replace(os.sep, "/")

    def _record(self, rel: str, written: tuple[str, int]) -> None:
        self.files[rel] = {"bytes": written[1], "sha256": written[0]}


def build(out=None) -> dict:
    """Build the result folder (the home's insights folder unless out is given); returns stats.json."""
    work.reset()
    started = time.perf_counter()
    settings = config.load()
    try:
        now = clock.now()
    except ValueError as error:
        from app.cli import CliError

        raise CliError(str(error), 2) from None
    ctx = Context(asof.of(now.date()), settings, Path(out) if out else home.insights_dir())
    units = {}
    seconds = {}
    for name, module in STAGES:
        begun = time.perf_counter()
        units[name] = module.run(ctx)
        seconds[name] = time.perf_counter() - begun
        logger.info("stage %s: %.3f s", name, seconds[name])
    stats = _stats(ctx, units)
    canon.write_json(ctx.out / "stats.json", stats)
    seconds["total"] = time.perf_counter() - started
    run = {
        "as_of": ctx.asof.day.isoformat(),
        "kind": "build",
        "seconds": {name: round(value, 3) for name, value in seconds.items()},
        "settings": stats["settings"],
        "started": clock.format_instant(now),
        "work": work.counts(),
    }
    # Appended, never rewritten: it is the history of the folder, and a build that does not
    # get this far leaves no line.
    with open(ctx.out / "runs.jsonl", "ab") as f:
        f.write(canon.dumps(run) + b"\n")
    logger.info("built %s", ctx.out)
    return stats


def read_stats(folder) -> dict:
    """stats.json of a folder, refused when it is missing or was written by other stage code."""
    from app.cli import CliError

    path = Path(folder) / "stats.json"
    if not path.is_file():
        raise CliError("no result in %s" % folder, 1)
    with open(path, "rb") as f:
        stats = json.loads(f.read().decode("utf-8"))
    written = stats.get("stages", {})
    for name, module in STAGES:
        entry = written.get(name)
        if not isinstance(entry, dict) or entry.get("version") != module.STAGE_VERSION:
            raise CliError("%s was written by another version of stage %s" % (folder, name), 1)
    return stats


def last_run(folder) -> dict:
    """The last line of runs.jsonl."""
    path = Path(folder) / "runs.jsonl"
    last = None
    if path.is_file():
        with open(path, "rb") as f:
            for line in f:
                if line.strip():
                    last = line
    if last is None:
        from app.cli import CliError

        raise CliError("no run recorded in %s" % folder, 1)
    return json.loads(last.decode("utf-8"))


def _stats(ctx: Context, units: dict) -> dict:
    states = {"analysed": 0, "failed": 0, "pending": 0}
    withdrawn = 0
    feeds = set()
    for row in ctx.papers:
        states[row["state"]] += 1
        if row["withdrawn"]:
            withdrawn += 1
        feeds.update(row["feeds"])
    return {
        "as_of": {"day": ctx.asof.day.isoformat(), "month": ctx.asof.month, "week": ctx.asof.week},
        "files": {rel: dict(entry) for rel, entry in sorted(ctx.files.items())},
        "inputs": {
            "analysed": states["analysed"],
            "failed": states["failed"],
            "feeds": sorted(feeds),
            "papers": len(ctx.papers),
            "pending": states["pending"],
            "withdrawn": withdrawn,
        },
        "settings": config.digest(ctx.settings),
        "stages": {name: dict(units[name], version=module.STAGE_VERSION) for name, module in STAGES},
    }
