"""Stage 8 of the insights build: weekly trends of a feed's papers, keywords and topics, and of authors."""
import json
from datetime import date

from app.insights import asof, config, pipeline, trends, work

FEED = "cs.LG"
THU = asof.of(date(2026, 9, 10))
E_ACUTE = chr(0x00E9)
ACUTE = chr(0x0301)


def paper(arxiv_id: str, published: str, keywords=None, state: str = "analysed", feeds=(FEED,),
          authors=("K. Maren",), withdrawn: bool = False) -> dict:
    if "T" not in published:
        published += "T12:00:00Z"
    return {
        "arxiv_id": arxiv_id,
        "feeds": list(feeds),
        "withdrawn": withdrawn,
        "state": state,
        "published": published,
        "keywords": None if keywords is None else list(keywords),
        "authors": list(authors),
    }


def add(rows: list, n: int, day: str, keywords=None, state: str = "analysed", topic=None, topic_of=None) -> None:
    """Append `n` papers published on `day`, with ids that follow the ones already in `rows`."""
    for _ in range(n):
        arxiv_id = f"2608.{90000 + len(rows):05d}"
        rows.append(paper(arxiv_id, day, keywords, state))
        if topic_of is not None:
            topic_of[arxiv_id] = topic


def section(recent: int = 4, baseline: int = 8, top: int = 15):
    return config.TrendSettings(recent_weeks=recent, baseline_weeks=baseline, top=top)


def example() -> tuple[list[dict], dict]:
    """The twelve papers of the worked example and their topics."""
    both = ["graph embedding", "node classification"]
    pair = ["sparse routing", "graph embedding"]
    rows = [
        paper("2608.90000", "2026-08-18", both),
        paper("2608.90001", "2026-08-18", both),
        paper("2608.90002", "2026-08-18", both),
        paper("2608.90003", "2026-08-25", ["sparse routing"]),
        paper("2608.90004", "2026-08-26", ["graph embedding"]),
        paper("2608.90005", "2026-09-01", pair),
        paper("2608.90006", "2026-09-01", pair),
        paper("2608.90007", "2026-09-01", pair),
        paper("2608.90008", "2026-09-01", pair),
        paper("2608.90009", "2026-09-06", ["sparse routing"]),
        paper("2608.90010", "2026-09-07", ["sparse routing"]),
        paper("2608.90011", "2026-09-02", state="pending"),
    ]
    topics = [0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1]
    topic_of = {row["arxiv_id"]: topic for row, topic in zip(rows, topics)}
    return rows, topic_of


def z_rounded(result: dict) -> dict:
    """A copy of a feed's trends with each z rounded to six places."""
    copy = json.loads(json.dumps(result))
    for entry in copy["rising"]:
        entry["z"] = round(entry["z"], 6)
    return copy


def test_trends_of_the_example_feed():
    work.reset()
    rows, topic_of = example()
    result = trends.feed_trends(FEED, rows, topic_of, THU, section(recent=2, baseline=2))
    assert z_rounded(result) == {
        "analysed": [3, 2, 5],
        "bursts": [{"before": 1, "count": 5, "term": "sparse routing"},
                   {"before": 4, "count": 4, "term": "graph embedding"}],
        "feed": FEED,
        "papers": [3, 2, 6],
        "rising": [{"baseline": 0, "recent": 6, "term": "sparse routing", "z": 1.936083}],
        "topics": [{"counts": [0, 0, 0, 2, 1, 2], "slope": [15, 35], "topic": 0},
                   {"counts": [0, 0, 0, 1, 1, 4], "slope": [24, 35], "topic": 1}],
        "weeks": ["2026-W34", "2026-W35", "2026-W36"],
    }
    assert isinstance(result["rising"][0]["z"], float)
    assert trends.z_score(5, 7, 3, 3) < 0.0
    assert work.counts()["trends.feeds"] == 1


def test_a_paper_counts_in_its_published_week_once_that_week_has_ended():
    rows = [
        paper("2608.90000", "2026-08-30T23:59:59Z"),
        paper("2608.90001", "2026-08-31T00:00:00Z"),
        paper("2608.90002", "2026-09-07T00:00:00Z"),
    ]
    cases = [
        (date(2026, 9, 7), ["2026-W35", "2026-W36"], [1, 1]),
        (date(2026, 9, 10), ["2026-W35", "2026-W36"], [1, 1]),
        (date(2026, 9, 13), ["2026-W35", "2026-W36"], [1, 1]),
        (date(2026, 9, 14), ["2026-W35", "2026-W36", "2026-W37"], [1, 1, 1]),
        (date(2026, 9, 17), ["2026-W35", "2026-W36", "2026-W37"], [1, 1, 1]),
    ]
    for day, weeks, papers in cases:
        result = trends.feed_trends(FEED, rows, {}, asof.of(day), section())
        assert (result["weeks"], result["papers"]) == (weeks, papers), day
    nothing = trends.feed_trends(FEED, rows, {}, asof.of(date(2026, 8, 30)), section())
    assert nothing == {"feed": FEED, "weeks": [], "papers": [], "analysed": [], "rising": [], "bursts": [],
                       "topics": []}


def test_windows_follow_the_last_counted_week():
    rows, topic_of = example()
    later = trends.feed_trends(FEED, rows, topic_of, asof.of(date(2026, 9, 14)), section(recent=2, baseline=2))
    much_later = trends.feed_trends(FEED, rows, topic_of, asof.of(date(2026, 10, 12)), section(recent=2, baseline=2))
    assert later == much_later
    assert later["weeks"][-1] == "2026-W37"

    rows = []
    add(rows, 6, "2026-08-18", ["gamma"])
    add(rows, 3, "2026-08-25", ["alpha"])
    add(rows, 3, "2026-09-01", ["alpha"])
    day = asof.of(date(2026, 9, 14))
    one = trends.feed_trends(FEED, rows, {}, day, section(recent=1, baseline=2))
    assert one["rising"] == [{"term": "alpha", "recent": 3, "baseline": 3, "z": trends.z_score(3, 3, 3, 9)}]
    two = trends.feed_trends(FEED, rows, {}, day, section(recent=2, baseline=2))
    assert two["rising"] == [{"term": "alpha", "recent": 6, "baseline": 0, "z": trends.z_score(6, 6, 0, 6)}]
    # The baseline of eight weeks reaches far before the first week; those weeks count nothing.
    eight = trends.feed_trends(FEED, rows, {}, day, section(recent=2, baseline=8))
    assert eight["rising"] == two["rising"]
    assert eight["weeks"] == ["2026-W34", "2026-W35", "2026-W36"]


def test_rising_terms():
    assert round(trends.z_score(6, 40, 3, 70), 6) == 1.855769
    assert trends.z_score(4, 10, 4, 10) == 0.0
    assert trends.log_odds(0, 0) == 0.0

    even = []
    add(even, 4, "2026-08-25", ["even"])
    add(even, 6, "2026-08-25", ["other"])
    add(even, 4, "2026-09-01", ["even"])
    add(even, 6, "2026-09-01", ["other"])
    result = trends.feed_trends(FEED, even, {}, THU, section(recent=1, baseline=1))
    assert [entry["term"] for entry in result["rising"]] == []

    rows = []
    add(rows, 10, "2026-08-25", ["filler"])
    add(rows, 3, "2026-09-01", ["beta"])
    add(rows, 3, "2026-09-01", ["alpha"])
    add(rows, 4, "2026-09-01", ["zeta"])
    add(rows, 2, "2026-09-01", ["omega"])
    n1 = 12
    expected = [
        {"term": "zeta", "recent": 4, "baseline": 0, "z": trends.z_score(4, n1, 0, 10)},
        {"term": "alpha", "recent": 3, "baseline": 0, "z": trends.z_score(3, n1, 0, 10)},
        {"term": "beta", "recent": 3, "baseline": 0, "z": trends.z_score(3, n1, 0, 10)},
    ]
    result = trends.feed_trends(FEED, rows, {}, THU, section(recent=1, baseline=1))
    assert result["rising"] == expected
    cut = trends.feed_trends(FEED, rows, {}, THU, section(recent=1, baseline=1, top=2))
    assert cut["rising"] == expected[:2]


def test_bursts():
    rows = []
    last, before = "2026-08-31", ["2026-08-24", "2026-08-03", "2026-07-06"]
    plan = [
        # term, papers in the last week, papers in the eight weeks before
        ("edge", 3, 12),
        ("near", 3, 11),
        ("also", 3, 11),
        ("low", 3, 2),
        ("big", 5, 0),
        ("few", 2, 0),
    ]
    for term, count, prev in plan:
        add(rows, count, last, [term])
        for i in range(prev):
            add(rows, 1, before[i % len(before)], [term])
    # Nine weeks before the last one: outside the window, so "near" stays a burst.
    add(rows, 5, "2026-06-29", ["near"])
    result = trends.feed_trends(FEED, rows, {}, THU, section())
    assert result["weeks"][-1] == "2026-W36"
    assert result["bursts"] == [
        {"term": "big", "count": 5, "before": 0},
        {"term": "low", "count": 3, "before": 2},
        {"term": "also", "count": 3, "before": 11},
        {"term": "near", "count": 3, "before": 11},
    ]
    assert 3 * trends.BURST_WEEKS == trends.BURST_FACTOR * 12
    cut = trends.feed_trends(FEED, rows, {}, THU, section(top=2))
    assert cut["bursts"] == result["bursts"][:2]


def test_who_counts_in_a_feed(tmp_path, monkeypatch):
    work.reset()
    rows, topic_of = [], {}
    add(rows, 3, "2026-09-01", ["alpha"], topic=0, topic_of=topic_of)
    add(rows, 1, "2026-09-02", ["alpha"], state="pending", topic=1, topic_of=topic_of)
    result = trends.feed_trends(FEED, rows, topic_of, THU, section())
    assert result["papers"] == [4]
    assert result["analysed"] == [3]
    assert result["bursts"] == [{"term": "alpha", "count": 3, "before": 0}]
    assert result["topics"] == [{"topic": 0, "counts": [0, 0, 0, 0, 0, 3], "slope": [15, 35]},
                                {"topic": 1, "counts": [0, 0, 0, 0, 0, 1], "slope": [5, 35]}]

    # The momentum: the example's integers, and a count that grows by one paper a week.
    example_rows, example_topics = example()
    slopes = [entry["slope"] for entry in trends.feed_trends(FEED, example_rows, example_topics, THU,
                                                             section(recent=2, baseline=2))["topics"]]
    assert slopes == [[15, 35], [24, 35]]
    rows, topic_of = [], {}
    add(rows, 1, "2026-07-21", topic=3, topic_of=topic_of)
    for weeks_ago, monday in enumerate(["2026-08-31", "2026-08-24", "2026-08-17", "2026-08-10", "2026-08-03",
                                        "2026-07-27"]):
        add(rows, 6 - weeks_ago, monday, topic=2, topic_of=topic_of)
    result = trends.feed_trends(FEED, rows, topic_of, THU, section())
    assert result["topics"] == [{"topic": 2, "counts": [1, 2, 3, 4, 5, 6], "slope": [35, 35]}]

    # Withdrawn papers are left out of every feed; a feed of withdrawn papers only still has its file.
    monkeypatch.setenv("ARXIVLENS_HOME", str(tmp_path / "home"))
    out = tmp_path / "out"
    stale = out / "trends" / "q-bio.GN.json"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"{}\n")
    ctx = pipeline.Context(THU, config.load(), out)
    ctx.papers = [
        paper("2608.90100", "2026-09-01", ["alpha"], authors=("K. Maren",)),
        paper("2608.90101", "2026-09-01", ["alpha"], authors=("W. Gone",), withdrawn=True),
        paper("2608.90102", "2026-09-01", ["beta"], feeds=("math.ST",), authors=("W. Gone",), withdrawn=True),
    ]
    ctx.topic_of = {"2608.90100": 0}
    work.reset()
    assert trends.run(ctx) == {"feeds": 2}
    assert work.counts()["trends.feeds"] == 2
    assert work.counts()["trends.authors"] == 1
    lg = json.loads((out / "trends" / "cs.LG.json").read_text(encoding="utf-8"))
    assert (lg["papers"], lg["analysed"]) == ([1], [1])
    st = json.loads((out / "trends" / "math.ST.json").read_text(encoding="utf-8"))
    assert st == {"feed": "math.ST", "weeks": [], "papers": [], "analysed": [], "rising": [], "bursts": [],
                  "topics": []}
    authors = json.loads((out / "authors.json").read_text(encoding="utf-8"))
    assert authors["papers"] == [1]
    assert not stale.exists()
    assert sorted(ctx.files) == ["authors.json", "trends/cs.LG.json", "trends/math.ST.json"]


def test_keywords_as_trend_terms():
    assert trends.keyword_terms(None) == []
    assert trends.keyword_terms([]) == []
    assert trends.keyword_terms(["Graph Neural Networks", "graph  neural\tnetworks", "GRAPH neural networks"]) == [
        "graph neural networks"]
    accented = ["R" + E_ACUTE + "seaux de neurones", "Ree" + ACUTE + "l"]
    assert trends.keyword_terms(accented) == ["reel", "reseaux de neurones"]
    assert trends.keyword_terms(["3D", "$x^2$", "--", "Graph-Based Learning", "large-scale"]) == [
        "graph-based learning", "large-scale"]
    # Two spellings of one keyword count once for the paper.
    rows = []
    add(rows, 3, "2026-09-01", ["Sparse Routing", "sparse  routing"])
    result = trends.feed_trends(FEED, rows, {}, THU, section())
    assert result["bursts"] == [{"term": "sparse routing", "count": 3, "before": 0}]


def test_authors_of_the_example():
    work.reset()
    nfc = "Jos" + E_ACUTE + " Arden"
    nfd = "Jose" + ACUTE + " Arden"
    rows = [
        paper("2608.90005", "2026-09-09T08:00:00Z", authors=(nfd,)),
        paper("2608.90000", "2026-08-18T08:00:00Z", authors=("K. Maren", "T. Soval")),
        paper("2608.90001", "2026-08-19T08:00:00Z", authors=("K. Maren",)),
        paper("2608.90002", "2026-08-25T08:00:00Z", authors=("R. Ilden", "K. Maren")),
        paper("2608.90003", "2026-08-26T08:00:00Z", authors=("T. Soval",)),
        paper("2608.90004", "2026-09-08T08:00:00Z", authors=(nfc,)),
    ]
    result = trends.author_trends(rows, section(recent=4))
    assert result == {
        "active": [{"author": "K. Maren", "papers": 3}, {"author": nfc, "papers": 2},
                   {"author": "T. Soval", "papers": 2}],
        "papers": [2, 2, 0, 2],
        "weeks": ["2026-W34", "2026-W35", "2026-W36", "2026-W37"],
        "with_new_author": [1, 1, 0, 1],
    }
    assert work.counts()["trends.authors"] == 1
    assert trends.author_trends(rows, section(recent=4, top=1))["active"] == [{"author": "K. Maren", "papers": 3}]
    # Only the last week is recent: the two spellings make two papers of one author.
    assert trends.author_trends(rows, section(recent=1))["active"] == [{"author": nfc, "papers": 2}]
    assert trends.author_trends([], section()) == {"weeks": [], "papers": [], "with_new_author": [], "active": []}
