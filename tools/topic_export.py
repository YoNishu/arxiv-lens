"""The papers put with one topic, as CSV on stdout.

Usage:  python tools/topic_export.py TOPIC

Reads the result folder of the home named by ARXIVLENS_HOME. Rows follow the topic's own order,
most similar first; the similarity is written with four decimals.
"""
import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # so "python tools/topic_export.py" finds the app package

from app import home
from app.insights import store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the members of one topic as CSV.")
    parser.add_argument("topic", type=int, help="topic number, as in topics.json")
    args = parser.parse_args(argv)
    try:
        found = store.topic(args.topic)
    except store.NoResult:
        print(f"error: no result in {home.insights_dir()}", file=sys.stderr)
        return 1
    if found is None:
        print(f"error: no such topic: {args.topic}", file=sys.stderr)
        return 1
    writer = csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(["arxiv_id", "title", "similarity"])
    for member in found.get("members", []):
        writer.writerow([member["arxiv_id"], member.get("title") or "", format(member["sim"], ".4f")])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
