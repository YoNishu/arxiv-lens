"""A paper's neighbours, the papers it cites and the papers that cite it, from the insights folder.

Usage:  python tools/related.py ARXIV_ID

Reads the result folder of the home named by ARXIVLENS_HOME. The last line is always
"cited by N", the number of stored papers whose reference lists name this one.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # so "python tools/related.py" finds the app package

from app import home
from app.insights import store


def _line(*parts: str) -> str:
    # A paper without a title in the folder must not leave spaces at the end of its line.
    return "  ".join(parts).rstrip()


def describe(found: dict) -> list[str]:
    state = f"state {found['state']}"
    if found.get("withdrawn"):
        state += " (withdrawn)"
    feeds = "feeds " + (" ".join(found.get("feeds") or []) or "-")
    topic = found.get("topic")
    if topic is None:
        topic_text = "topic -"
    else:
        topic_text = f"topic {topic['topic']} {topic.get('name') or ''}".rstrip()
    lines = [_line(found["arxiv_id"], found.get("title") or ""), ", ".join([state, feeds, topic_text]), ""]
    lines.append("similar")
    for entry in found.get("similar", []):
        lines.append("  " + _line(format(entry["score"], ".2f"), entry["arxiv_id"], entry.get("title") or ""))
    lines.append("cites")
    for entry in found.get("cites", []):
        lines.append("  " + _line(entry["arxiv_id"], entry.get("title") or ""))
    lines.append("citing papers")
    for entry in found.get("cited_by", []):
        lines.append("  " + _line(entry["arxiv_id"], entry.get("title") or ""))
    lines.append(f"cited by {found.get('cited_by_count', 0)}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Show the papers related to one paper.")
    parser.add_argument("arxiv_id", help="arXiv id without version, such as 2608.90105")
    args = parser.parse_args(argv)
    try:
        found = store.related(args.arxiv_id)
    except store.NoResult:
        print(f"error: no result in {home.insights_dir()}", file=sys.stderr)
        return 1
    if found is None:
        print(f"error: no such paper: {args.arxiv_id}", file=sys.stderr)
        return 1
    sys.stdout.write("".join(line + "\n" for line in describe(found)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
