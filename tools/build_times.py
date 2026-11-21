"""How long the last insights builds took, stage by stage, and the work each one counted.

Usage:  python tools/build_times.py [--last N]

Reads <home>/insights/runs.jsonl, one line per finished build, and shows the last N of them
(default 10), oldest first: a tab-separated table of seconds, then the work counters of each run.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # so "python tools/build_times.py" finds the app package

from app import home

STAGES = ["snapshot", "text", "vocab", "vectors", "similar", "citations", "topics", "trends", "ranking", "digests"]


def read_runs(path: Path) -> list[tuple[int, dict]]:
    """(line number, parsed line) for every line of the file that holds a run."""
    runs = []
    with open(path, "rb") as f:
        for number, line in enumerate(f, start=1):
            if line.strip():
                runs.append((number, json.loads(line)))
    return runs


def seconds_keys(last: dict) -> list[str]:
    present = [k for k in last.get("seconds", {}) if k != "total"]
    known = [k for k in STAGES if k in present]
    return known + sorted(k for k in present if k not in STAGES)


def _seconds(value) -> str:
    return "-" if value is None else format(value, ".3f")


def table(runs: list[tuple[int, dict]]) -> list[str]:
    keys = seconds_keys(runs[-1][1])
    lines = ["\t".join(["run", "started", "kind", "as_of", "total"] + keys)]
    for number, run in runs:
        seconds = run.get("seconds", {})
        cells = [str(number), run.get("started") or "-", run.get("kind") or "-", run.get("as_of") or "-",
                 _seconds(seconds.get("total"))]
        cells += [_seconds(seconds.get(k)) for k in keys]
        lines.append("\t".join(cells))
    lines.append("")
    for number, run in runs:
        work = run.get("work", {})
        pairs = " ".join(f"{name}={work[name]}" for name in sorted(work))
        lines.append(f"run {number}: {pairs}" if pairs else f"run {number}:")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Show the times and work counts of the last insights builds.")
    parser.add_argument("--last", type=int, default=10, help="how many runs to show (default 10)")
    args = parser.parse_args(argv)
    if args.last < 1:
        print("error: --last must be at least 1", file=sys.stderr)
        return 2
    folder = home.insights_dir()
    path = folder / "runs.jsonl"
    runs = read_runs(path) if path.is_file() else []
    if not runs:
        print(f"error: no run recorded in {folder}", file=sys.stderr)
        return 1
    sys.stdout.write("".join(line + "\n" for line in table(runs[-args.last:])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
