"""Stage 8: weekly trends of each feed and of the library's authors.

`trends/<feed>.json` holds, over the complete weeks by published day, the papers and analysed papers
of each week, the analysis keywords that are rising in the recent weeks against the baseline, the
keywords that burst in the last counted week, and how each topic's weekly count is moving.
`authors.json`, in the folder itself, counts per week the papers and the papers that bring an author
the library had not seen before, and names the authors with most papers lately.
"""
import logging
import math
import unicodedata
from datetime import date, timedelta

from app.insights import canon, textproc, work
from app.insights.asof import AsOf, day_of, monday_of, week_label

logger = logging.getLogger(__name__)

STAGE_VERSION = 1

MIN_COUNT = 3
BURST_WEEKS = 8
BURST_FACTOR = 2
MOMENTUM = (-5, -3, -1, 1, 3, 5)
MOMENTUM_DEN = 35
ACTIVE_MIN = 2


def keyword_terms(keywords: list[str] | None) -> list[str]:
    """The keywords of one paper as lower-case phrases of plain tokens, sorted, without repeats."""
    if keywords is None:
        return []
    found = set()
    for keyword in keywords:
        term = " ".join(textproc.TOKEN.findall(textproc.normalise(keyword)))
        if term:
            found.add(term)
    return sorted(found)


def log_odds(c: int, n: int) -> float:
    return canon.ln(2 * c + 1) - canon.ln(2 * (n - c) + 1)


def z_score(a: int, n1: int, b: int, n2: int) -> float:
    v = 2.0 / (2 * a + 1) + 2.0 / (2 * (n1 - a) + 1) + 2.0 / (2 * b + 1) + 2.0 / (2 * (n2 - b) + 1)
    return (log_odds(a, n1) - log_odds(b, n2)) / math.sqrt(v)


def feed_trends(feed: str, rows: list[dict], topic_of: dict[str, int | None], asof: AsOf, settings) -> dict:
    """Trends of one feed; `rows` are its listed papers, `settings` the trends section."""
    papers, analysed, terms, topics = {}, {}, {}, {}
    for row in rows:
        wk = monday_of(day_of(row["published"]))
        if wk < asof.week_start:
            papers[wk] = papers.get(wk, 0) + 1
            if row["state"] == "analysed":
                analysed[wk] = analysed.get(wk, 0) + 1
                for term in keyword_terms(row["keywords"]):
                    weeks = terms.setdefault(term, {})
                    weeks[wk] = weeks.get(wk, 0) + 1
            topic = topic_of.get(row["arxiv_id"])
            if topic is not None:
                weeks = topics.setdefault(topic, {})
                weeks[wk] = weeks.get(wk, 0) + 1
    work.count("trends.feeds")

    result = {"feed": feed, "weeks": [], "papers": [], "analysed": [], "rising": [], "bursts": [], "topics": []}
    if not papers:
        return result
    first = min(papers)
    last = max(papers)
    weeks = _mondays(first, last)
    result["weeks"] = [week_label(w) for w in weeks]
    result["papers"] = [papers.get(w, 0) for w in weeks]
    result["analysed"] = [analysed.get(w, 0) for w in weeks]

    # Every window is counted back from the last week that has papers, so a feed that has not been
    # fed for a while still shows its last weeks rather than empty ones.
    recent = _back(last, 0, settings.recent_weeks)
    baseline = _back(last, settings.recent_weeks, settings.baseline_weeks)
    before = _back(last, 1, BURST_WEEKS)
    six = list(reversed(_back(last, 0, len(MOMENTUM))))
    n1 = _over(analysed, recent)
    n2 = _over(analysed, baseline)

    rising = []
    for term in sorted(terms):
        a = _over(terms[term], recent)
        if a < MIN_COUNT:
            continue
        b = _over(terms[term], baseline)
        z = z_score(a, n1, b, n2)
        if z > 0.0:
            rising.append({"term": term, "recent": a, "baseline": b, "z": z})
    rising.sort(key=lambda entry: (-entry["z"], entry["term"]))
    result["rising"] = rising[:settings.top]

    bursts = []
    for term in sorted(terms):
        c = terms[term].get(last, 0)
        prev = _over(terms[term], before)
        if c >= MIN_COUNT and c * BURST_WEEKS > BURST_FACTOR * prev:
            bursts.append({"term": term, "count": c, "before": prev})
    bursts.sort(key=lambda entry: (-entry["count"], entry["before"], entry["term"]))
    result["bursts"] = bursts[:settings.top]

    for topic in sorted(topics):
        counts = [topics[topic].get(w, 0) for w in six]
        if not any(counts):
            continue
        slope = 0
        for weight, count in zip(MOMENTUM, counts):
            slope += weight * count
        result["topics"].append({"topic": topic, "counts": counts, "slope": [slope, MOMENTUM_DEN]})
    return result


def author_trends(rows: list[dict], settings) -> dict:
    """Papers and first-time authors per week over all listed papers, and the authors most active lately."""
    work.count("trends.authors")
    papers, with_new, by_author = {}, {}, {}
    seen = set()
    for row in sorted(rows, key=lambda r: (r["published"], r["arxiv_id"])):
        wk = monday_of(day_of(row["published"]))
        papers[wk] = papers.get(wk, 0) + 1
        names = sorted({unicodedata.normalize("NFC", name) for name in row["authors"]})
        if any(name not in seen for name in names):
            with_new[wk] = with_new.get(wk, 0) + 1
        for name in names:
            seen.add(name)
            weeks = by_author.setdefault(name, {})
            weeks[wk] = weeks.get(wk, 0) + 1

    if not papers:
        return {"weeks": [], "papers": [], "with_new_author": [], "active": []}
    last = max(papers)
    weeks = _mondays(min(papers), last)
    recent = _back(last, 0, settings.recent_weeks)
    active = []
    for name in sorted(by_author):
        n = _over(by_author[name], recent)
        if n >= ACTIVE_MIN:
            active.append({"author": name, "papers": n})
    active.sort(key=lambda entry: (-entry["papers"], entry["author"]))
    return {
        "weeks": [week_label(w) for w in weeks],
        "papers": [papers.get(w, 0) for w in weeks],
        "with_new_author": [with_new.get(w, 0) for w in weeks],
        "active": active[:settings.top],
    }


def run(ctx) -> dict[str, int]:
    settings = ctx.settings.trends
    feed_set = sorted({feed for paper in ctx.papers for feed in paper["feeds"]})
    listed = [paper for paper in ctx.papers if not paper["withdrawn"]]
    for feed in feed_set:
        rows = [paper for paper in listed if feed in paper["feeds"]]
        ctx.write_json(f"trends/{feed}.json", feed_trends(feed, rows, ctx.topic_of, ctx.asof, settings))
    # Not under trends/: a feed could be called "authors".
    ctx.write_json("authors.json", author_trends(listed, settings))
    ctx.prune("trends")
    return {"feeds": len(feed_set)}


def _mondays(first: date, last: date) -> list[date]:
    weeks = []
    wk = first
    while wk <= last:
        weeks.append(wk)
        wk += timedelta(days=7)
    return weeks


def _back(last: date, skip: int, n: int) -> list[date]:
    """`n` Mondays going back from `last`, after leaving out the `skip` newest ones."""
    return [last - timedelta(days=7 * (skip + i)) for i in range(n)]


def _over(counts: dict[date, int], window: list[date]) -> int:
    total = 0
    for wk in window:
        total += counts.get(wk, 0)
    return total
