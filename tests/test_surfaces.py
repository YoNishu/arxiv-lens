"""The insights in the REST routes, the pages, the chat tools and the MCP tools."""
import contextlib
import json
import os
from pathlib import Path

import httpx
import pytest
from markupsafe import escape

from conftest import new_server

NO_INSIGHTS_CHAT = "No insights have been built yet."
NO_INSIGHTS_MCP = "No insights have been built yet. Run: python -m app insights build"
UNKNOWN_ID = "2401.99999"


def rows(path: Path) -> list[dict]:
    with open(path, "rb") as f:
        return [json.loads(line) for line in f if line.strip()]


def load(path: Path):
    with open(path, "rb") as f:
        return json.loads(f.read())


def read_folder(folder: Path) -> dict:
    """The files of a result folder the surfaces show, read without the store."""
    return {
        "stats": load(folder / "stats.json"),
        "papers": {r["arxiv_id"]: r for r in rows(folder / "papers.jsonl")},
        "topics": load(folder / "topics.json")["topics"],
        "assignments": rows(folder / "assignments.jsonl"),
        "similar": {r["arxiv_id"]: r["similar"] for r in rows(folder / "similar.jsonl")},
        "edges": rows(folder / "citations" / "edges.jsonl"),
        "nodes": {r["arxiv_id"]: r for r in rows(folder / "citations" / "nodes.jsonl")},
        "ranking": rows(folder / "ranking.jsonl"),
    }


def citers_of(facts: dict, arxiv_id: str) -> list[str]:
    return sorted(e["src"] for e in facts["edges"] if e["dst"] == arxiv_id)


def cites_of(facts: dict, arxiv_id: str) -> list[str]:
    return sorted(e["dst"] for e in facts["edges"] if e["src"] == arxiv_id)


def most_cited(facts: dict) -> str:
    counts: dict[str, int] = {}
    for e in facts["edges"]:
        counts[e["dst"]] = counts.get(e["dst"], 0) + 1
    return min(counts, key=lambda i: (-counts[i], i))


def most_citing(facts: dict) -> str:
    counts: dict[str, int] = {}
    for e in facts["edges"]:
        counts[e["src"]] = counts.get(e["src"], 0) + 1
    return min(counts, key=lambda i: (-counts[i], i))


def members_of(facts: dict, number: int) -> list[dict]:
    found = [r for r in facts["assignments"] if r["topic"] == number]
    return sorted(found, key=lambda r: (-r["sim"], r["arxiv_id"]))


def ranked_feed(facts: dict) -> str:
    """The first feed of the library that has a ranked week."""
    ranked = {r["feed"] for r in facts["ranking"]}
    return next(f for f in facts["stats"]["inputs"]["feeds"] if f in ranked)


def weeks_of(facts: dict, feed: str) -> list[str]:
    return sorted(r["week"] for r in facts["ranking"] if r["feed"] == feed)


def html(text: str) -> str:
    return str(escape(text))


def more(total: int, shown: int) -> list[str]:
    return ["... and %d more" % (total - shown)] if total > shown else []


def get_json(server, path: str):
    status, body = server.get(path)
    assert status == 200, (path, status, body)
    return json.loads(body)


def detail(server, path: str) -> tuple[int, str]:
    status, body = server.get(path)
    return status, json.loads(body).get("detail")


@contextlib.contextmanager
def without_result(home):
    aside = home.path / "insights-aside"
    os.rename(home.insights, aside)
    try:
        yield
    finally:
        os.rename(aside, home.insights)


@pytest.fixture(scope="module")
def site(small, tmp_path_factory):
    home = small.home_at["END"].copy(tmp_path_factory.mktemp("surfaces") / "home")
    facts = read_folder(home.insights)
    server = new_server(home)
    try:
        yield home, server, facts
    finally:
        server.stop()


def test_rest_answers_404_until_a_result_exists(site):
    home, server, facts = site
    feed = facts["stats"]["inputs"]["feeds"][0]
    bucket = facts["ranking"][0]
    paths = [
        "/insights/stats",
        "/insights/topics",
        "/insights/topics/%d" % facts["topics"][0]["topic"],
        "/insights/trends/%s" % feed,
        "/insights/papers/%s/related" % sorted(facts["papers"])[0],
        "/insights/digests/%s/%s" % (bucket["feed"], bucket["week"]),
    ]
    with without_result(home):
        for path in paths:
            assert detail(server, path) == (404, "No insights have been built"), path
    got = get_json(server, "/insights/stats")
    assert got["as_of"] == facts["stats"]["as_of"]


def test_rest_serves_topics_trends_related_papers_and_digests(site):
    home, server, facts = site
    stats = facts["stats"]

    got = get_json(server, "/insights/stats")
    assert got["as_of"] == stats["as_of"]
    assert got["inputs"] == stats["inputs"]
    assert got["settings"] == stats["settings"]
    assert got["stages"] == stats["stages"]
    assert got["files"] == len(stats["files"])

    topics = get_json(server, "/insights/topics")
    by_number = {t["topic"]: t for t in facts["topics"]}
    assert [t["topic"] for t in topics] == sorted(by_number)
    first = by_number[topics[0]["topic"]]
    for key in ("name", "blurb", "source", "size", "recent", "terms"):
        assert topics[0][key] == first[key], key

    members = members_of(facts, first["topic"])
    assert members
    got = get_json(server, "/insights/topics/%d?limit=3" % first["topic"])
    assert got["member_count"] == len(members)
    assert [m["arxiv_id"] for m in got["members"]] == [m["arxiv_id"] for m in members[:3]]
    assert got["members"][0]["title"] == facts["papers"][members[0]["arxiv_id"]]["title"]
    assert got["members"][0]["sim"] == members[0]["sim"]
    assert got["exemplars"] == first["exemplars"]
    assert detail(server, "/insights/topics/9999") == (404, "Topic not found")
    assert server.get("/insights/topics/%d?limit=0" % first["topic"])[0] == 422

    feed = stats["inputs"]["feeds"][0]
    want = load(home.insights / "trends" / (feed + ".json"))
    got = get_json(server, "/insights/trends/" + feed)
    for key in ("feed", "weeks", "papers", "analysed", "rising", "bursts"):
        assert got[key] == want[key], key
    assert [(t["topic"], t["name"], t["counts"], t["slope"]) for t in got["topics"]] == [
        (t["topic"], by_number[t["topic"]]["name"], t["counts"], t["slope"]) for t in want["topics"]
    ]
    assert detail(server, "/insights/trends/xx.YY") == (404, "No trends for xx.YY")

    target = most_cited(facts)
    got = get_json(server, "/insights/papers/%s/related" % target)
    assert got["title"] == facts["papers"][target]["title"]
    assert got["feeds"] == facts["papers"][target]["feeds"]
    assert [p["arxiv_id"] for p in got["cited_by"]] == citers_of(facts, target)
    assert [p["arxiv_id"] for p in got["cites"]] == cites_of(facts, target)
    assert got["cited_by_count"] == facts["nodes"][target]["cited_by"]
    assert got["external"] == facts["nodes"][target]["external"]
    assert [(s["arxiv_id"], s["score"]) for s in got["similar"]] == [
        (s["id"], s["score"]) for s in facts["similar"].get(target, [])
    ]
    # An old-style id has a slash in it and still reaches the route.
    assert "cs/9912999" not in facts["papers"]
    assert detail(server, "/insights/papers/cs/9912999/related") == (404, "Paper not found")
    for arxiv_id in sorted(facts["papers"]):
        if "/" in arxiv_id:
            assert get_json(server, "/insights/papers/%s/related" % arxiv_id)["arxiv_id"] == arxiv_id
            break

    bucket = facts["ranking"][-1]
    got = get_json(server, "/insights/digests/%s/%s" % (bucket["feed"], bucket["week"]))
    ordered = sorted(bucket["papers"], key=lambda p: p["rank"])
    assert got["ranked"] == len(ordered)
    assert got["closed"] == (bucket["week"] < stats["as_of"]["week"])
    assert [p["arxiv_id"] for p in got["papers"]] == [p["arxiv_id"] for p in ordered]
    assert got["papers"][0]["title"] == facts["papers"][ordered[0]["arxiv_id"]]["title"]
    assert got["papers"][0]["score"] == ordered[0]["score"]
    assert "page" not in got
    assert detail(server, "/insights/digests/%s/1999-W01" % bucket["feed"]) == (
        404, "No digest for %s 1999-W01" % bucket["feed"]
    )


def test_pages_render_topics_trends_and_weekly_digests(site):
    home, server, facts = site
    topic = facts["topics"][0]

    status, page = server.get("/ui/topics")
    assert status == 200
    assert html(topic["name"]) in page
    places = [page.index('href="/ui/%s"' % name) for name in ("papers", "topics", "trends", "weekly", "categories")]
    assert places == sorted(places)
    assert '<a href="/ui/topics" class="active">Topics</a>' in page

    status, page = server.get("/ui/topics/%d" % topic["topic"])
    assert status == 200
    assert html(topic["name"]) in page
    first_member = members_of(facts, topic["topic"])[0]["arxiv_id"]
    assert 'href="/ui/arxiv/%s"' % first_member in page

    feed = facts["stats"]["inputs"]["feeds"][0]
    trends = load(home.insights / "trends" / (feed + ".json"))
    for path in ("/ui/trends", "/ui/trends/" + feed):
        status, page = server.get(path)
        assert status == 200, path
        assert '<a href="/ui/trends" class="active">Trends</a>' in page
        assert "Trends in %s" % feed in page
        for entry in trends["rising"][:1] + trends["bursts"][:1]:
            assert html(entry["term"]) in page, path
        names = {t["topic"]: t["name"] for t in facts["topics"]}
        for entry in trends["topics"][:1]:
            assert html(names[entry["topic"]]) in page, path

    ranked = ranked_feed(facts)
    weeks = weeks_of(facts, ranked)
    buckets = {(r["feed"], r["week"]): r for r in facts["ranking"]}
    # Without a feed in the path the page shows the first feed and its last week.
    latest = "/ui/weekly" if ranked == feed else "/ui/weekly/" + ranked
    for path, week in ((latest, weeks[-1]), ("/ui/weekly/%s/%s" % (ranked, weeks[0]), weeks[0])):
        status, page = server.get(path)
        assert status == 200, path
        top = min(buckets[(ranked, week)]["papers"], key=lambda p: p["rank"])
        assert html(facts["papers"][top["arxiv_id"]]["title"]) in page, path
        state = "closed" if week < facts["stats"]["as_of"]["week"] else "open"
        assert 'class="week-status %s"' % state in page, path
    if len(weeks) > 1:
        assert 'href="/ui/weekly/%s/%s"' % (ranked, weeks[1]) in page

    assert server.get("/ui/topics/9999") == (404, "Topic not found")
    assert server.get("/ui/trends/xx.YY") == (404, "No trends for xx.YY")
    assert server.get("/ui/weekly/%s/1999-W01" % ranked) == (404, "No digest for %s 1999-W01" % ranked)

    with without_result(home):
        for path in ("/ui/topics", "/ui/topics/%d" % topic["topic"], "/ui/trends", "/ui/weekly"):
            status, page = server.get(path)
            assert status == 200, path
            assert 'class="empty-state"' in page, path
            assert "No insights have been built yet." in page, path


def test_paper_page_shows_related_papers_and_arxiv_links_redirect(site):
    home, server, facts = site
    ids = {arxiv_id: number for number, arxiv_id in home.sql("SELECT id, arxiv_id FROM papers")}
    target = most_cited(facts)

    answer = httpx.get(server.url + "/ui/arxiv/" + target, follow_redirects=False)
    assert answer.status_code == 303
    assert answer.headers["location"] == "/ui/paper/%d" % ids[target]

    status, page = server.get("/ui/paper/%d" % ids[target])
    assert status == 200
    assert "Related papers" in page
    citers = citers_of(facts, target)
    assert "Cited by (%d)" % len(citers) in page
    for arxiv_id in citers:
        assert 'href="/ui/arxiv/%s"' % arxiv_id in page
    similar = facts["similar"].get(target, [])
    for entry in similar:
        assert 'href="/ui/arxiv/%s"' % entry["id"] in page
        assert html(facts["papers"][entry["id"]]["title"]) in page
    if similar:
        assert "Similar" in page
    placed = {r["arxiv_id"]: r["topic"] for r in facts["assignments"]}
    if placed.get(target) is not None:
        assert 'href="/ui/topics/%d"' % placed[target] in page

    citing = most_citing(facts)
    status, page = server.get("/ui/paper/%d" % ids[citing])
    assert status == 200
    assert "Cites (%d)" % len(cites_of(facts, citing)) in page
    for arxiv_id in cites_of(facts, citing):
        assert 'href="/ui/arxiv/%s"' % arxiv_id in page

    with without_result(home):
        status, page = server.get("/ui/paper/%d" % ids[target])
        assert status == 200
        assert "Related papers" not in page

    assert UNKNOWN_ID not in ids
    answer = httpx.get(server.url + "/ui/arxiv/" + UNKNOWN_ID, follow_redirects=False)
    assert (answer.status_code, answer.text) == (404, "Paper not found")


def _chat_ref(facts: dict, ids: dict, arxiv_id: str) -> str:
    name = "[ID:%d]" % ids[arxiv_id] if arxiv_id in ids else "[arXiv:%s]" % arxiv_id
    return "%s **%s**" % (name, facts["papers"][arxiv_id]["title"])


def test_chat_tools_answer_from_the_result(site):
    home, server, facts = site
    ids = {arxiv_id: number for number, arxiv_id in home.sql("SELECT id, arxiv_id FROM papers")}
    target = most_cited(facts)
    topic = facts["topics"][0]
    feed = ranked_feed(facts)
    weeks = weeks_of(facts, feed)
    results = home.batch(
        ["tool", "get_related_papers", json.dumps({"paper_id": ids[target]})],
        ["tool", "get_topic", "{}"],
        ["tool", "get_topic", json.dumps({"topic": topic["topic"]})],
        ["tool", "get_trends", json.dumps({"feed": feed})],
        ["tool", "get_weekly_digest", json.dumps({"feed": feed})],
        ["tool", "get_weekly_digest", json.dumps({"feed": feed, "week": weeks[0]})],
        ["tool", "get_weekly_digest", json.dumps({"feed": feed, "week": "1999-W01"})],
        ["tool", "get_trends", json.dumps({"feed": "xx.YY"})],
        ["tool", "get_topic", json.dumps({"topic": 9999})],
        ["tool", "get_related_papers", json.dumps({"paper_id": 999999})],
    )
    for result in results:
        assert result.status == 0, result.stderr

    names = {t["topic"]: t["name"] for t in facts["topics"]}
    placed = {r["arxiv_id"]: r["topic"] for r in facts["assignments"]}
    head = "Related to **%s** (arXiv:%s)" % (facts["papers"][target]["title"], target)
    if placed.get(target) is not None:
        head += "\nTopic: %s" % names[placed[target]]
    sections = [head]
    similar = facts["similar"].get(target, [])
    if similar:
        lines = ["%s (similarity %s)" % (_chat_ref(facts, ids, s["id"]), format(s["score"], ".2f")) for s in similar]
        sections.append("\n".join(["Similar papers:"] + lines[:10] + more(len(lines), 10)))
    for heading, listed in (("Cites:", cites_of(facts, target)), ("Cited by:", citers_of(facts, target))):
        if listed:
            lines = [_chat_ref(facts, ids, i) for i in listed]
            sections.append("\n".join([heading] + lines[:10] + more(len(lines), 10)))
    assert results[0].stdout == "\n\n".join(sections) + "\n"

    lines = [
        "[Topic %d] %s: %d papers, %d in the last four weeks. Terms: %s"
        % (t["topic"], t["name"], t["size"], t["recent"], ", ".join(t["terms"][:5]))
        for t in facts["topics"]
    ]
    assert results[1].stdout == "\n".join(lines[:10] + more(len(lines), 10)) + "\n"

    lines = results[2].stdout.splitlines()
    assert lines[0] == "**%s** (topic %d, %d papers, %d in the last four weeks)" % (
        topic["name"], topic["topic"], topic["size"], topic["recent"]
    )
    assert "Terms: " + ", ".join(topic["terms"]) in lines
    closest = members_of(facts, topic["topic"])
    at = lines.index("Closest papers:")
    assert lines[at + 1:at + 1 + min(5, len(closest))] == [
        "%s (similarity %s)" % (_chat_ref(facts, ids, m["arxiv_id"]), format(m["sim"], ".2f")) for m in closest[:5]
    ]

    trends = load(home.insights / "trends" / (feed + ".json"))
    lines = ["Trends in %s (weeks %s to %s)" % (feed, trends["weeks"][0], trends["weeks"][-1])]
    if trends["rising"]:
        lines.append("Rising terms:")
        lines += [
            "- %s: %d recent, %d before (z %s)" % (r["term"], r["recent"], r["baseline"], format(r["z"], ".2f"))
            for r in trends["rising"][:10]
        ]
        lines += more(len(trends["rising"]), 10)
    if trends["bursts"]:
        lines.append("Bursts:")
        lines += [
            "- %s: %d in the last week, %d in the eight weeks before" % (b["term"], b["count"], b["before"])
            for b in trends["bursts"][:10]
        ]
        lines += more(len(trends["bursts"]), 10)
    momentum = sorted(trends["topics"], key=lambda t: (-t["slope"][0], t["topic"]))
    if momentum:
        lines.append("Topics:")
        lines += [
            "- %s: %s (%s per week)"
            % (names[t["topic"]], " ".join(str(c) for c in t["counts"]), format(t["slope"][0] / 35, "+.2f"))
            for t in momentum[:5]
        ]
        lines += more(len(momentum), 5)
    assert results[3].stdout == "\n".join(lines) + "\n"

    for result, week in ((results[4], weeks[-1]), (results[5], weeks[0])):
        with open(home.insights / "digests" / feed / (week + ".md"), "rb") as f:
            assert result.stdout == f.read().decode("utf-8")
    assert results[6].stdout == "No weekly digest for %s 1999-W01.\n" % feed
    assert results[7].stdout == "No trends for xx.YY.\n"
    assert results[8].stdout == "Topic 9999 not found.\n"
    assert results[9].stdout == "Paper 999999 not found.\n"

    with without_result(home):
        results = home.batch(
            ["tool", "get_related_papers", json.dumps({"paper_id": ids[target]})],
            ["tool", "get_topic", "{}"],
            ["tool", "get_trends", json.dumps({"feed": feed})],
            ["tool", "get_weekly_digest", json.dumps({"feed": feed})],
        )
    for result in results:
        assert (result.status, result.stdout) == (0, NO_INSIGHTS_CHAT + "\n")


def test_mcp_tools_answer_from_the_result(site):
    home, server, facts = site
    target = most_cited(facts)
    feed = facts["stats"]["inputs"]["feeds"][0]
    results = home.batch(
        ["tool", "--mcp", "related_papers", json.dumps({"arxiv_id": target})],
        ["tool", "--mcp", "topics"],
        ["tool", "--mcp", "trends", json.dumps({"feed": feed})],
        ["tool", "--mcp", "related_papers", json.dumps({"arxiv_id": UNKNOWN_ID})],
        ["tool", "--mcp", "trends", json.dumps({"feed": "xx.YY"})],
    )
    for result in results:
        assert result.status == 0, result.stderr

    row = facts["papers"][target]
    lines = ["# " + row["title"], "arXiv:%s | %s | feeds: %s" % (target, row["state"], ", ".join(row["feeds"])), ""]
    lines.append("## Similar")
    for s in facts["similar"].get(target, []):
        lines.append("- %s %s (%s)" % (s["id"], facts["papers"][s["id"]]["title"], format(s["score"], ".3f")))
    lines.append("## Cites")
    lines += ["- %s %s" % (i, facts["papers"][i]["title"]) for i in cites_of(facts, target)]
    lines.append("## Cited by (%d)" % facts["nodes"][target]["cited_by"])
    lines += ["- %s %s" % (i, facts["papers"][i]["title"]) for i in citers_of(facts, target)]
    assert results[0].stdout == "\n".join(lines) + "\n"

    lines = [
        "- %d: %s (%d papers; %s)" % (t["topic"], t["name"], t["size"], ", ".join(t["terms"][:3]))
        for t in facts["topics"]
    ]
    assert results[1].stdout == "\n".join(lines) + "\n"

    rising = load(home.insights / "trends" / (feed + ".json"))["rising"]
    if rising:
        lines = ["Rising in %s:" % feed] + [
            "**%s** %d/%d z=%s" % (r["term"], r["recent"], r["baseline"], format(r["z"], ".2f")) for r in rising
        ]
    else:
        lines = ["Nothing is rising in %s." % feed]
    assert results[2].stdout == "\n".join(lines) + "\n"
    assert results[3].stdout == "Paper %s is not in the insights.\n" % UNKNOWN_ID
    assert results[4].stdout == "No trends for xx.YY.\n"

    with without_result(home):
        results = home.batch(
            ["tool", "--mcp", "related_papers", json.dumps({"arxiv_id": target})],
            ["tool", "--mcp", "topics"],
            ["tool", "--mcp", "trends", json.dumps({"feed": feed})],
        )
    for result in results:
        assert (result.status, result.stdout) == (0, NO_INSIGHTS_MCP + "\n")
