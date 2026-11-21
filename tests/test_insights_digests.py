"""Stage 10 of the insights build: the weekly digest page of each ranked bucket."""
from datetime import date

from app.insights import asof, config, digests, pipeline, work

THU = asof.of(date(2026, 9, 10))
NAMES = ["Graph and Embed", "Speech and Audio"]

PAPERS = {
    "2608.90003": {"arxiv_id": "2608.90003", "title": "Sparse Routing for Graph Embeddings",
                   "authors": ["K. Maren", "T. Soval", "R. Ilden", "A. Dorun"]},
    "2608.90004": {"arxiv_id": "2608.90004", "title": "Gated Mixtures at Scale", "authors": ["R. Ilden"]},
    "2608.90006": {"arxiv_id": "2608.90006", "title": "A Note on Routing", "authors": ["A. Dorun", "K. Maren"]},
}
TOPIC_OF = {"2608.90003": 0, "2608.90004": 0, "2608.90006": None}


def bucket(week: str, feed: str = "cs.LG") -> dict:
    """The bucket of the ranking example, under another week when asked."""
    return {"feed": feed, "week": week, "papers": [
        {"arxiv_id": "2608.90003", "grounding": 2, "heat": 4, "novelty": 7, "percentile": 100, "rank": 1,
         "score": 0.5439004051593306},
        {"arxiv_id": "2608.90004", "grounding": 2, "heat": 4, "novelty": 7, "percentile": 50, "rank": 2,
         "score": 0.5439004051593306},
        {"arxiv_id": "2608.90006", "grounding": 0, "heat": 0, "novelty": 9, "percentile": 0, "rank": 3,
         "score": 0.54},
    ]}


EXAMPLE_PAGE = """# cs.LG week 2026-W35

2026-08-24 to 2026-08-30, closed, 3 ranked

1. Sparse Routing for Graph Embeddings (2608.90003)
   K. Maren, T. Soval, R. Ilden et al. | score 0.54 | Graph and Embed
2. Gated Mixtures at Scale (2608.90004)
   R. Ilden | score 0.54 | Graph and Embed
3. A Note on Routing (2608.90006)
   A. Dorun, K. Maren | score 0.54 | -
"""


def header(page: str) -> str:
    return page.split("\n")[2]


def test_header_says_closed_or_the_day_of_an_open_week():
    cases = [
        ("2026-W35", date(2026, 9, 10), "2026-08-24 to 2026-08-30, closed, 3 ranked"),
        ("2026-W36", date(2026, 9, 10), "2026-08-31 to 2026-09-06, closed, 3 ranked"),
        ("2026-W37", date(2026, 9, 7), "2026-09-07 to 2026-09-13, open, day 1 of 7, 3 ranked"),
        ("2026-W37", date(2026, 9, 10), "2026-09-07 to 2026-09-13, open, day 4 of 7, 3 ranked"),
        ("2026-W37", date(2026, 9, 13), "2026-09-07 to 2026-09-13, open, day 7 of 7, 3 ranked"),
        ("2026-W37", date(2026, 9, 14), "2026-09-07 to 2026-09-13, closed, 3 ranked"),
        ("2026-W53", date(2027, 1, 3), "2026-12-28 to 2027-01-03, open, day 7 of 7, 3 ranked"),
    ]
    for week, day, expected in cases:
        page = digests.render(bucket(week), PAPERS, TOPIC_OF, NAMES, asof.of(day), 10)
        assert header(page) == expected, (week, day)
    closed = digests.render(bucket("2026-W36"), PAPERS, TOPIC_OF, NAMES, THU, 10)
    assert "2026-09-10" not in closed


def test_page_of_the_example():
    work.reset()
    page = digests.render(bucket("2026-W35"), PAPERS, TOPIC_OF, NAMES, THU, 10)
    assert page == EXAMPLE_PAGE
    assert work.counts()["digests.pages"] == 1

    papers = {
        "2608.90010": {"arxiv_id": "2608.90010", "title": "Costs *in* $O(n)$ time",
                       "authors": ["A. Dorun", "K. Maren", "T. Soval"]},
        "2608.90011": {"arxiv_id": "2608.90011", "title": "Speech Units", "authors": []},
    }
    rounded = {"feed": "eess.AS", "week": "2026-W35", "papers": [
        {"arxiv_id": "2608.90010", "grounding": 0, "heat": 0, "novelty": 9, "percentile": 100, "rank": 1,
         "score": 0.995},
        {"arxiv_id": "2608.90011", "grounding": 0, "heat": 0, "novelty": 5, "percentile": 0, "rank": 2,
         "score": 0.545},
    ]}
    page = digests.render(rounded, papers, {"2608.90010": 1, "2608.90011": None}, NAMES, THU, 10)
    assert page.split("\n") == [
        "# eess.AS week 2026-W35",
        "",
        "2026-08-24 to 2026-08-30, closed, 2 ranked",
        "",
        "1. Costs *in* $O(n)$ time (2608.90010)",
        "   A. Dorun, K. Maren, T. Soval | score 0.99 | Speech and Audio",
        "2. Speech Units (2608.90011)",
        "    | score 0.55 | -",
        "",
    ]
    assert page.endswith("-\n") and not page.endswith("\n\n")


def test_the_page_is_cut_and_still_counts_all():
    page = digests.render(bucket("2026-W37"), PAPERS, TOPIC_OF, NAMES, THU, 2)
    lines = page.split("\n")
    assert lines[:3] == ["# cs.LG week 2026-W37", "", "2026-09-07 to 2026-09-13, open, day 4 of 7, 3 ranked"]
    assert lines[4:] == EXAMPLE_PAGE.split("\n")[4:8] + [""]
    assert "2608.90006" not in page
    one = digests.render(bucket("2026-W35"), PAPERS, TOPIC_OF, NAMES, THU, 1)
    assert one.count("\n") == 6 and "3 ranked" in one


def test_pages_of_buckets_that_are_gone_are_removed(tmp_path, monkeypatch):
    monkeypatch.setenv("ARXIVLENS_HOME", str(tmp_path / "home"))
    settings = config.load()
    out = tmp_path / "out"

    def build(buckets: list[dict]) -> pipeline.Context:
        ctx = pipeline.Context(THU, settings, out)
        ctx.papers = [PAPERS[key] for key in sorted(PAPERS)]
        ctx.topic_of = dict(TOPIC_OF)
        ctx.topic_names = list(NAMES)
        ctx.buckets = buckets
        work.reset()
        assert digests.run(ctx) == {"pages": len(buckets)}
        assert work.counts()["digests.pages"] == len(buckets)
        return ctx

    first = build([bucket("2026-W35"), bucket("2026-W36"), bucket("2026-W36", feed="stat.ML")])
    assert sorted(first.files) == ["digests/cs.LG/2026-W35.md", "digests/cs.LG/2026-W36.md",
                                   "digests/stat.ML/2026-W36.md"]
    assert (out / "digests" / "cs.LG" / "2026-W35.md").read_bytes() == EXAMPLE_PAGE.encode("utf-8")
    assert (out / "digests" / "stat.ML" / "2026-W36.md").exists()

    second = build([bucket("2026-W35"), bucket("2026-W36")])
    assert sorted(second.files) == ["digests/cs.LG/2026-W35.md", "digests/cs.LG/2026-W36.md"]
    assert not (out / "digests" / "stat.ML").exists()
    assert sorted(p.name for p in (out / "digests").iterdir()) == ["cs.LG"]
    assert sorted(p.name for p in (out / "digests" / "cs.LG").iterdir()) == ["2026-W35.md", "2026-W36.md"]

    build([])
    assert not (out / "digests").exists()
