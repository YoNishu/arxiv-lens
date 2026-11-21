"""The insights commands: build a result folder, read it back, check it and compare two of them.

register() adds them under `python -m app insights`. Only the standard library and app.home are
imported here; each handler imports what it needs when it runs, so the rest of the command line
starts without loading the stages. Handlers return the exit status and raise app.cli.CliError
for an expected failure.
"""
import json
import os
import sys
from pathlib import Path

from app import home


def register(sub) -> None:
    """Add the parser `insights` with its sub-commands to the top-level sub-parsers."""
    parser = sub.add_parser("insights", help="build and read the insights result folder")
    commands = parser.add_subparsers(dest="insights_command", metavar="COMMAND", required=True)

    build = commands.add_parser("build", help="compute every result again from the database")
    _out_option(build)
    build.set_defaults(func=_build)

    show = commands.add_parser("show", help="print part of a result folder")
    shows = show.add_subparsers(dest="insights_show", metavar="WHAT", required=True)
    show_work = shows.add_parser("work", help="work counts of the last build")
    _out_option(show_work)
    show_work.set_defaults(func=_show_work)
    show_topics = shows.add_parser("topics", help="the topics with their sizes and names")
    _out_option(show_topics)
    show_topics.set_defaults(func=_show_topics)
    show_trends = shows.add_parser("trends", help="rising terms and bursts of one feed")
    show_trends.add_argument("feed", metavar="FEED")
    _out_option(show_trends)
    show_trends.set_defaults(func=_show_trends)
    show_paper = shows.add_parser("paper", help="topic, citations and similar papers of one paper")
    show_paper.add_argument("arxiv_id", metavar="ARXIV_ID")
    _out_option(show_paper)
    show_paper.set_defaults(func=_show_paper)

    settings = commands.add_parser("config", help="print the settings in force")
    settings.set_defaults(func=_config)

    check = commands.add_parser("check", help="compare the files of a result folder with its stats.json")
    _out_option(check)
    check.set_defaults(func=_check)

    diff = commands.add_parser("diff", help="compare two result folders file by file")
    diff.add_argument("a", metavar="A")
    diff.add_argument("b", metavar="B")
    diff.set_defaults(func=_diff)

    report = commands.add_parser("report", help="write the library report and its audit")
    _out_option(report)
    report.add_argument("--to", metavar="DIR", help="folder for report.md and audit.txt (default: report in the home)")
    report.set_defaults(func=_report)


def _out_option(parser) -> None:
    parser.add_argument("--out", metavar="OUT", help="result folder (default: insights in the home)")


def _folder(args) -> Path:
    if args.out:
        return Path(os.path.abspath(args.out))
    return home.insights_dir()


def _build(args) -> int:
    from app.insights import pipeline

    stats = pipeline.build(_folder(args))
    print("built %d files: %d papers, %d topics, as of %s"
          % (len(stats["files"]), stats["inputs"]["papers"], stats["stages"]["topics"]["topics"], stats["as_of"]["day"]))
    return 0


def _show_work(args) -> int:
    from app.insights import pipeline, work

    run = pipeline.last_run(_folder(args))
    for name in work.NAMES:
        print("%s=%d" % (name, run["work"][name]))
    return 0


def _show_topics(args) -> int:
    from app.insights import pipeline

    folder = _folder(args)
    pipeline.read_stats(folder)
    for topic in _read_json(folder, "topics.json")["topics"]:
        print("%d\t%d\t%d\t%s\t%s" % (topic["topic"], topic["size"], topic["recent"], topic["source"], topic["name"]))
    return 0


def _show_trends(args) -> int:
    from app.cli import CliError
    from app.insights import pipeline

    folder = _folder(args)
    stats = pipeline.read_stats(folder)
    # Only a feed the build knows becomes part of a path.
    if args.feed not in stats["inputs"]["feeds"]:
        raise CliError("no trends for %s" % args.feed, 1)
    trends = _read_json(folder, "trends/%s.json" % args.feed)
    for entry in trends["rising"]:
        print("rising\t%s\t%d\t%d\t%s" % (entry["term"], entry["recent"], entry["baseline"], format(entry["z"], ".2f")))
    for entry in trends["bursts"]:
        print("burst\t%s\t%d\t%d" % (entry["term"], entry["count"], entry["before"]))
    return 0


def _show_paper(args) -> int:
    from app.cli import CliError
    from app.insights import pipeline

    folder = _folder(args)
    pipeline.read_stats(folder)
    paper = _find_row(folder, "papers.jsonl", args.arxiv_id)
    if paper is None:
        raise CliError("no such paper: %s" % args.arxiv_id, 1)
    assignment = _find_row(folder, "assignments.jsonl", args.arxiv_id)
    topic = "-"
    if assignment is not None and assignment["topic"] is not None:
        names = {entry["topic"]: entry["name"] for entry in _read_json(folder, "topics.json")["topics"]}
        topic = "%d %s" % (assignment["topic"], names.get(assignment["topic"], "-"))
    node = _find_row(folder, "citations/nodes.jsonl", args.arxiv_id) or {"cited_by": 0, "cites": 0}
    neighbours = _find_row(folder, "similar.jsonl", args.arxiv_id)
    lines = [
        ("arxiv_id", paper["arxiv_id"]),
        ("state", paper["state"]),
        ("feeds", " ".join(paper["feeds"]) or "-"),
        ("topic", topic),
        ("cited_by", str(node["cited_by"])),
        ("cites", str(node["cites"])),
    ]
    for entry in neighbours["similar"] if neighbours is not None else []:
        lines.append(("similar", "%s %s" % (entry["id"], format(entry["score"], ".2f"))))
    for label, value in lines:
        print("%-12s%s" % (label, value))
    return 0


def _config(args) -> int:
    from app.insights import config

    sys.stdout.write(config.dump(config.load()))
    return 0


def _check(args) -> int:
    from app.insights import compare

    lines, whole = compare.check(_folder(args))
    for line in lines:
        print(line)
    return 0 if whole else 1


def _diff(args) -> int:
    from app.insights import compare

    lines, differing = compare.diff(Path(os.path.abspath(args.a)), Path(os.path.abspath(args.b)))
    for line in lines:
        print(line)
    return 0 if differing == 0 else 1


def _report(args) -> int:
    from app.insights import report

    to = Path(os.path.abspath(args.to)) if args.to else home.report_dir()
    recounts, differing = report.write(_folder(args), to)
    print("audit: %d recounts, %d differ" % (recounts, differing))
    return 1 if differing else 0


def _read_json(folder: Path, rel: str):
    from app.cli import CliError

    try:
        with open(folder / rel, "rb") as f:
            return json.loads(f.read().decode("utf-8"))
    except (OSError, ValueError):
        raise CliError("cannot read %s in %s" % (rel, folder), 1) from None


def _find_row(folder: Path, rel: str, arxiv_id: str) -> dict | None:
    """The row of one paper in a .jsonl file of the folder, or None."""
    from app.cli import CliError

    needle = json.dumps(arxiv_id, ensure_ascii=False).encode("utf-8")
    try:
        with open(folder / rel, "rb") as f:
            for line in f:
                # Most lines cannot hold the id at all; only those that can are parsed.
                if needle not in line:
                    continue
                row = json.loads(line.decode("utf-8"))
                if row.get("arxiv_id") == arxiv_id:
                    return row
    except (OSError, ValueError):
        raise CliError("cannot read %s in %s" % (rel, folder), 1) from None
    return None
