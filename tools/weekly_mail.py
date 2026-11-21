"""The text of the weekly mail for one feed, made from a copy of the insights folder.

Usage:  python tools/weekly_mail.py FEED [WEEK] [--folder DIR]

It is meant for a host that has the result folder and not the application, so it uses the
standard library only. Without WEEK the mail is for the last week that has ended. The folder is
DIR, else <ARXIVLENS_HOME or data>/insights.
"""
import argparse
import json
import os
import re
import sys

PAPER = re.compile(r"^(\d+)\. (.*) \((\S+)\)$")
DETAIL = re.compile(r"^   (.*) \| score (\S+) \| (.*)$")
HEAD = re.compile(r"^(\S+) to (\S+), (.*), (\d+) ranked$")
WEEK = re.compile(r"^\d{4}-W\d{2}$")


class Refusal(Exception):
    """The answer is no: the folder has no such week to write about."""


def default_folder() -> str:
    home = os.environ.get("ARXIVLENS_HOME") or "data"
    return os.path.join(os.path.abspath(os.path.expanduser(home)), "insights")


def feed_rows(folder: str, feed: str) -> dict[str, dict]:
    """The rows of ranking.jsonl for one feed, by week label."""
    path = os.path.join(folder, "ranking.jsonl")
    if not os.path.isfile(path):
        raise Refusal(f"no result in {folder}")
    rows = {}
    with open(path, "rb") as f:
        for line in f:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("feed") == feed:
                rows[row["week"]] = row
    return rows


def read_page(folder: str, feed: str, week: str) -> list[str] | None:
    path = os.path.join(folder, "digests", feed, week + ".md")
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as f:
        return f.read().decode("utf-8").split("\n")


def page_head(lines: list[str]) -> tuple[str, str, str] | None:
    """Monday, Sunday and status from the third line of a page."""
    if len(lines) < 3:
        return None
    m = HEAD.match(lines[2])
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3)


def page_papers(lines: list[str]) -> list[dict]:
    """The papers a page shows, in its order: a PAPER line, then its DETAIL line."""
    papers = []
    i = 4
    while i < len(lines):
        head = PAPER.match(lines[i])
        detail = DETAIL.match(lines[i + 1]) if head and i + 1 < len(lines) else None
        if head and detail:
            papers.append({
                "rank": head.group(1),
                "title": head.group(2),
                "arxiv_id": head.group(3),
                "authors": detail.group(1),
                "score": detail.group(2),
                "topic": detail.group(3),
            })
            i += 2
        else:
            i += 1
    return papers


def topic_blurbs(folder: str) -> list[tuple[str, str]]:
    path = os.path.join(folder, "topics.json")
    if not os.path.isfile(path):
        return []
    with open(path, "rb") as f:
        data = json.loads(f.read())
    return [(t.get("name") or "", t.get("blurb") or "") for t in data.get("topics", [])]


def last_closed_week(folder: str, feed: str, rows: dict[str, dict]) -> str:
    # A mail goes out for a week that has ended; the page says which ones have.
    for week in sorted(rows, reverse=True):
        lines = read_page(folder, feed, week)
        head = page_head(lines) if lines is not None else None
        if head and head[2] == "closed":
            return week
    raise Refusal(f"no closed week for {feed}")


def compose(folder: str, feed: str, week: str | None) -> list[str]:
    rows = feed_rows(folder, feed)
    if week is None:
        week = last_closed_week(folder, feed, rows)
    row = rows.get(week)
    lines = read_page(folder, feed, week) if row is not None else None
    head = page_head(lines) if lines is not None else None
    if row is None or head is None:
        raise Refusal(f"no digest for {feed} {week}")
    monday, sunday, status = head
    members = len(row.get("papers", []))
    by_id = {p["arxiv_id"]: p for p in row.get("papers", [])}
    shown = page_papers(lines)
    blurbs = topic_blurbs(folder)

    out = [f"Subject: {feed} week {week}: {members} papers ranked", ""]
    days = f"{feed}, {monday} to {sunday}"
    if status != "closed":
        days += f" ({status})"
    out += [days, ""]
    names = []
    for p in shown:
        ranked = by_id.get(p["arxiv_id"], {})
        novelty = ranked.get("novelty")
        grounding = ranked.get("grounding", 0)
        if p["topic"] == "-":
            topic = "no topic"
        else:
            topic = f"topic: {p['topic']}"
            if p["topic"] not in names:
                names.append(p["topic"])
        out += [
            f"{p['rank']}. {p['title']}",
            f"   {p['authors']}",
            f"   https://arxiv.org/abs/{p['arxiv_id']}",
            f"   score {p['score']}, novelty {'-' if novelty is None else novelty}/10, "
            f"{grounding} references in the library, {topic}",
            "",
        ]
    if names:
        out.append("Topics")
        for name in names:
            blurb = next((b for n, b in blurbs if n == name), "")
            out.append(f"- {name}: {blurb}" if blurb else f"- {name}")
        out.append("")
    out.append(f"{members} papers ranked, {len(shown)} shown.")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print the weekly mail of one feed.")
    parser.add_argument("feed", help="feed code, such as cs.LG")
    parser.add_argument("week", nargs="?", help="week label, such as 2026-W36; default: the last closed week")
    parser.add_argument("--folder", help="result folder; default: <ARXIVLENS_HOME or data>/insights")
    args = parser.parse_args(argv)
    if args.week is not None and not WEEK.match(args.week):
        print(f"error: not a week label: {args.week}", file=sys.stderr)
        return 2
    folder = args.folder if args.folder else default_folder()
    try:
        lines = compose(folder, args.feed, args.week)
    except Refusal as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    sys.stdout.write("".join(line + "\n" for line in lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
