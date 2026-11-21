"""The stages on the bundled small corpus: the planted papers read back from builds made on six days."""

import json
import re
import unicodedata
from datetime import date, timedelta

from conftest import SMALL


def rows(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def by_id(items: list[dict]) -> dict[str, dict]:
    return {item["arxiv_id"]: item for item in items}


def feeds_of_corpus() -> list[str]:
    return json.loads((SMALL / "corpus.json").read_text(encoding="utf-8"))["feeds"]


def version(small, arxiv_id: str, v: int) -> dict:
    return next(item for item in small.versions[arxiv_id]["versions"] if item["v"] == v)


def day_of(text: str) -> date:
    return date(int(text[0:4]), int(text[5:7]), int(text[8:10]))


def monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


def week_label(monday: date) -> str:
    year, week, _ = monday.isocalendar()
    return "%04d-W%02d" % (year, week)


def mondays(first: date, last: date) -> list[date]:
    found = []
    while first <= last:
        found.append(first)
        first += timedelta(days=7)
    return found


def week_start(folder) -> date:
    return monday_of(day_of(load(folder / "stats.json")["as_of"]["day"]))


def lists_holding(similar: list[dict], arxiv_id: str) -> list[str]:
    return [row["arxiv_id"] for row in similar if any(entry["id"] == arxiv_id for entry in row["similar"])]


def test_reference_lists_equal_the_corpus(small):
    folder = small.build["END"]
    analysed = [paper for paper in rows(folder / "papers.jsonl") if paper["state"] == "analysed"]
    refs = by_id(rows(folder / "text" / "refs.jsonl"))
    assert analysed
    assert sorted(refs) == [paper["arxiv_id"] for paper in analysed]
    wrong = []
    for paper in analysed:
        truth = version(small, paper["arxiv_id"], paper["version"])
        row = refs[paper["arxiv_id"]]
        if (row["cites"], row["other"]) != (truth["cites"], truth["other_refs"]):
            wrong.append((paper["arxiv_id"], row["cites"], row["other"], truth["cites"], truth["other_refs"]))
    assert wrong == []


def test_zoo_decoy_and_textless_papers(small):
    folder = small.build["B1"]
    papers = by_id(rows(folder / "papers.jsonl"))
    refs = by_id(rows(folder / "text" / "refs.jsonl"))

    zoo = small.planted["zoo"]["id"]
    row = refs[zoo]
    assert row["cites"] == version(small, zoo, papers[zoo]["version"])["cites"]
    assert len(row["cites"]) == 8
    assert row["cites"][3:6] == ["cs/9812901", "math/0001901", "0911.9412"]
    assert (row["entries"], row["other"]) == (14, 4)

    decoy = small.planted["contents_decoy"]["id"]
    assert refs[decoy]["entries"] == len(version(small, decoy, papers[decoy]["version"])["refs"])

    textless = small.planted["no_text"]["id"]
    assert refs[textless]["entries"] == 0 and refs[textless]["cites"] == []
    assert refs[textless]["text_sha256"] is not None
    assert refs[textless]["text_sha256"] == papers[textless]["text_sha256"]


def test_unicode_and_tex_titles_give_clean_terms(small):
    folder = small.build["B1"]
    papers = by_id(rows(folder / "papers.jsonl"))
    tokens = by_id(rows(folder / "text" / "tokens.jsonl"))
    clean = re.compile(r"[a-z0-9 -]+")

    unicode_title = small.planted["unicode_title"]["id"]
    assert any(ord(ch) > 127 for ch in papers[unicode_title]["title"])
    for key in ("terms", "first_terms"):
        assert tokens[unicode_title][key], key
        bad = [term for term in tokens[unicode_title][key] if not clean.fullmatch(term)]
        assert bad == [], key

    tex_title = small.planted["tex_title"]["id"]
    commands = {name.lower() for name in re.findall(r"\\([A-Za-z]+)", papers[tex_title]["title"])}
    assert commands
    for key in ("terms", "first_terms"):
        words = {part for term in tokens[tex_title][key] for part in re.split(r"[ -]", term)}
        assert words and not (words & commands), key


def test_twins_have_equal_vectors_and_stand_in_id_order(small):
    folder = small.build["B1"]
    first, second = sorted(small.planted["twins"]["ids"])
    vectors = by_id(rows(folder / "vectors.jsonl"))
    assert vectors[first]["vector"] and vectors[first]["vector"] == vectors[second]["vector"]

    similar = rows(folder / "similar.jsonl")
    lists = by_id(similar)
    for owner, twin in ((first, second), (second, first)):
        entries = lists[owner]["similar"]
        scores = {entry["id"]: entry["score"] for entry in entries}
        assert twin in scores, owner
        assert entries[0]["id"] == twin or scores[twin] == entries[0]["score"], owner
    for row in similar:
        ids = [entry["id"] for entry in row["similar"]]
        if first in ids and second in ids:
            assert ids.index(first) < ids.index(second), row["arxiv_id"]
            assert row["similar"][ids.index(first)]["score"] == row["similar"][ids.index(second)]["score"]


def test_withdrawn_hub_leaves_the_lists_and_keeps_its_citations(small):
    case = small.planted["withdrawn_hub"]
    hub = case["id"]
    others = [arxiv_id for arxiv_id in case["cluster"] if arxiv_id != hub]
    assert len(others) == 6

    before = small.build["B1"]
    holding = lists_holding(rows(before / "similar.jsonl"), hub)
    assert [arxiv_id for arxiv_id in others if arxiv_id not in holding] == []
    assert by_id(rows(before / "citations" / "nodes.jsonl"))[hub]["cited_by"] == 3

    after = small.build["A5"]
    similar = rows(after / "similar.jsonl")
    assert hub not in by_id(similar)
    assert hub not in by_id(rows(after / "assignments.jsonl"))
    assert lists_holding(similar, hub) == []
    assert by_id(rows(after / "citations" / "nodes.jsonl"))[hub]["cited_by"] == 3
    assert [edge for edge in rows(after / "citations" / "edges.jsonl") if edge["src"] == hub] == []


def test_a_late_cited_paper_turns_external_ids_into_edges(small):
    case = small.planted["late_cited"]
    paper, citers = case["id"], case["citers"]
    assert len(citers) == 4

    early = small.build["A2"]
    assert by_id(rows(early / "citations" / "external.jsonl")).get(paper, {}).get("cited_by") == 4
    assert paper not in by_id(rows(early / "papers.jsonl"))

    late = small.build["A3"]
    assert paper not in by_id(rows(late / "citations" / "external.jsonl"))
    assert by_id(rows(late / "citations" / "nodes.jsonl"))[paper]["cited_by"] == 4
    edges = [edge for edge in rows(late / "citations" / "edges.jsonl") if edge["dst"] == paper]
    assert sorted(edge["src"] for edge in edges) == sorted(citers)


def test_papers_cited_together_list_each_other(small):
    ids = small.planted["cocite"]["ids"]
    assert len(ids) == 3
    nodes = by_id(rows(small.build["B1"] / "citations" / "nodes.jsonl"))
    for paper in ids:
        counts = {entry["id"]: entry["count"] for entry in nodes[paper]["cocited"]}
        for other in ids:
            if other != paper:
                assert counts.get(other) == 2, (paper, other)


def test_a_new_title_shows_on_the_card_and_moves_no_fit(small):
    paper = small.planted["prototype"]["id"]
    before, after = small.build["A1"], small.build["A2"]
    places = [(topic["topic"], n) for topic in load(before / "topics.json")["topics"]
              for n, exemplar in enumerate(topic["exemplars"]) if exemplar["arxiv_id"] == paper]
    assert len(places) == 1
    topic, place = places[0]

    old_title = by_id(rows(before / "papers.jsonl"))[paper]["title"]
    new_title = by_id(rows(after / "papers.jsonl"))[paper]["title"]
    assert new_title != old_title
    shown = load(after / "topics.json")["topics"][topic]
    assert shown["topic"] == topic
    assert shown["exemplars"][place] == {"arxiv_id": paper, "title": new_title}
    for name in ("centroids.json", "vocab.json"):
        assert (before / name).read_bytes() == (after / name).read_bytes(), name


def test_the_reference_is_the_whole_library_until_the_month_turns(small):
    before, after = small.build["B1"], small.build["A1"]
    old, new = load(before / "vocab.json"), load(after / "vocab.json")
    assert old["reference"]["whole_library"] is True
    assert old["reference"]["papers"] == len(rows(before / "papers.jsonl"))
    assert new["reference"]["whole_library"] is False
    month = load(after / "stats.json")["as_of"]["month"]
    assert new["reference"]["month"] == month
    turn = day_of(month + "-01")
    earlier = [paper for paper in rows(after / "papers.jsonl") if day_of(paper["announced"]) < turn]
    assert new["reference"]["papers"] == len(earlier)
    assert (before / "vocab.json").read_bytes() != (after / "vocab.json").read_bytes()


def test_trend_weeks_equal_a_recount(small):
    folder = small.build["A5"]
    start = week_start(folder)
    papers = rows(folder / "papers.jsonl")
    published = by_id(papers)
    pair = small.planted["sunday_monday"]["ids"]
    weeks = sorted(monday_of(day_of(published[arxiv_id]["published"])) for arxiv_id in pair)
    assert weeks[0] != weeks[1] and weeks[1] < start

    feeds = load(folder / "stats.json")["inputs"]["feeds"]
    assert feeds
    for feed in feeds:
        counted = [paper for paper in papers if feed in paper["feeds"] and not paper["withdrawn"]
                   and monday_of(day_of(paper["published"])) < start]
        data = load(folder / "trends" / f"{feed}.json")
        if not counted:
            assert (data["weeks"], data["papers"], data["analysed"]) == ([], [], []), feed
            continue
        per_week, analysed = {}, {}
        for paper in counted:
            week = monday_of(day_of(paper["published"]))
            per_week[week] = per_week.get(week, 0) + 1
            if paper["state"] == "analysed":
                analysed[week] = analysed.get(week, 0) + 1
        span = mondays(min(per_week), max(per_week))
        assert data["weeks"] == [week_label(week) for week in span], feed
        assert data["papers"] == [per_week.get(week, 0) for week in span], feed
        assert data["analysed"] == [analysed.get(week, 0) for week in span], feed


def test_two_spellings_are_one_author(small, monkeypatch):
    folder = small.build["B1"]
    papers = rows(folder / "papers.jsonl")
    named = by_id(papers)
    first, second = small.planted["nfd_author"]["ids"]
    pairs = [(a, b) for a in named[first]["authors"] for b in named[second]["authors"]
             if a != b and unicodedata.normalize("NFC", a) == unicodedata.normalize("NFC", b)]
    assert len(pairs) == 1

    monkeypatch.setenv("ARXIVLENS_HOME", str(small.home_at["B1"].path))
    from app.insights import config

    settings = config.load().trends
    listed = sorted((paper for paper in papers if not paper["withdrawn"]),
                    key=lambda paper: (paper["published"], paper["arxiv_id"]))
    seen, per_week, with_new, by_author = set(), {}, {}, {}
    for paper in listed:
        week = monday_of(day_of(paper["published"]))
        names = [unicodedata.normalize("NFC", name) for name in paper["authors"]]
        per_week[week] = per_week.get(week, 0) + 1
        if any(name not in seen for name in names):
            with_new[week] = with_new.get(week, 0) + 1
        seen.update(names)
        for name in sorted(set(names)):
            weeks = by_author.setdefault(name, {})
            weeks[week] = weeks.get(week, 0) + 1
    span = mondays(min(per_week), max(per_week))
    recent = span[-settings.recent_weeks:]
    active = []
    for name in sorted(by_author):
        n = 0
        for week in recent:
            n += by_author[name].get(week, 0)
        if n >= 2:
            active.append((-n, name))
    expected = {
        "weeks": [week_label(week) for week in span],
        "papers": [per_week.get(week, 0) for week in span],
        "with_new_author": [with_new.get(week, 0) for week in span],
        "active": [{"author": name, "papers": -n} for n, name in sorted(active)[:settings.top]],
    }
    assert load(folder / "authors.json") == expected


def test_a_late_link_joins_the_bucket_of_its_announced_week(small):
    paper = small.planted["late_link"]["id"]
    second_feed = feeds_of_corpus()[1]
    folder = small.build["A2"]
    row = by_id(rows(folder / "papers.jsonl"))[paper]
    assert second_feed in row["feeds"]
    monday = monday_of(day_of(row["announced"]))
    assert monday < week_start(folder)
    buckets = {(bucket["feed"], bucket["week"]): bucket for bucket in rows(folder / "ranking.jsonl")}
    members = [entry["arxiv_id"] for entry in buckets[(second_feed, week_label(monday))]["papers"]]
    assert paper in members

    earlier = {(bucket["feed"], bucket["week"]): bucket for bucket in rows(small.build["A1"] / "ranking.jsonl")}
    before = earlier.get((second_feed, week_label(monday)), {"papers": []})["papers"]
    assert paper not in [entry["arxiv_id"] for entry in before]


def test_open_pages_carry_the_day_and_closed_pages_do_not(small):
    folder = small.build["A1"]
    stats = load(folder / "stats.json")
    day, running = stats["as_of"]["day"], stats["as_of"]["week"]
    assert day == str(small.days["A1"]) and day_of(day).isoweekday() == 3
    pages = sorted(name for name in stats["files"] if name.startswith("digests/"))
    open_pages = [name for name in pages if name.endswith(f"/{running}.md")]
    assert open_pages and len(open_pages) < len(pages)
    for name in pages:
        text = (folder / name).read_text(encoding="utf-8")
        third = text.split("\n")[2]
        if name in open_pages:
            assert ", open, day 3 of 7, " in third, name
        else:
            assert ", closed, " in third, name
            assert day not in text, name


def test_names_come_from_the_endpoint_or_fall_back(small, labels, tmp_path):
    folder = small.build["A1"]
    topics = load(folder / "topics.json")
    k = topics["k"]
    assert k > 0
    assert [topic["source"] for topic in topics["topics"]] == ["model"] * k
    assert rows(folder / "runs.jsonl")[-1]["work"]["labels.asked"] == k

    home = small.home_at["A1"].copy(tmp_path / "a1")
    asked, named = tmp_path / "asked", tmp_path / "unnamed"
    before = labels.count("label")
    home.run("insights", "build", "--out", str(asked), env={"AZURE_OPENAI_ENDPOINT": labels.url})
    assert labels.count("label") - before == k
    assert [topic["source"] for topic in load(asked / "topics.json")["topics"]] == ["model"] * k

    home.run("insights", "build", "--out", str(named), env={"AZURE_OPENAI_ENDPOINT": ""})
    text = (named / "topics.json").read_text(encoding="utf-8")
    fallback = json.loads(text)
    assert fallback["k"] == k
    assert [topic["source"] for topic in fallback["topics"]] == ["fallback"] * k
    assert text.count("fallback") == k
    assert rows(named / "runs.jsonl")[-1]["work"]["labels.asked"] == 0
