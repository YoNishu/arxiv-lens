"""Work counters of an insights build: how often each piece of work was done in one run.

Each counter is raised by the one function that does that piece of work, once per call. The
build resets them when it starts and records them in its line of runs.jsonl, where
`insights show work` and tools/build_times.py read them.
"""
NAMES = ("snapshot.rows", "text.read", "text.parsed", "text.tokenized", "vocab.fits", "vectors.built",
         "similar.searched", "similar.scored", "citations.resolved", "citations.lists", "citations.graphs",
         "topics.fits", "topics.assigned", "labels.asked", "trends.feeds", "trends.authors",
         "ranking.buckets", "digests.pages")

_counts: dict[str, int] = dict.fromkeys(NAMES, 0)


def reset() -> None:
    for name in NAMES:
        _counts[name] = 0


def count(name: str, n: int = 1) -> None:
    if name not in _counts:
        raise KeyError(name)
    _counts[name] += n


def counts() -> dict[str, int]:
    """Every counter with its value, zeros included, in the order of NAMES."""
    return {name: _counts[name] for name in NAMES}
