"""Topics: their number, the seeds, the fit, assignment, cards, prompts and names."""
import json
import math
from types import SimpleNamespace

import pytest

from app.insights import labels, topics, vectors, work

R = 0.7071067811865476

# Five fit papers: two near a/b, two near c/d, and one exactly between the first seeds.
EXAMPLE = {
    "2608.90001": [("a", 0.8), ("b", 0.6)],
    "2608.90002": [("a", 0.6), ("b", 0.8)],
    "2608.90003": [("c", 0.8), ("d", 0.6)],
    "2608.90004": [("c", 0.6), ("d", 0.8)],
    "2608.90005": [("a", R), ("c", R)],
}
IDS = sorted(EXAMPLE)


def rounded(weights):
    return {term: round(x, 6) for term, x in weights.items()}


def example_fit(iterations=2):
    return topics.fit(IDS, EXAMPLE, 2, iterations)


def example_card(terms=("a", "b")):
    return {
        "terms": list(terms),
        "exemplars": [{"arxiv_id": "2608.90001", "title": "Title 1"},
                      {"arxiv_id": "2608.90002", "title": "Title 2"}],
    }


def test_number_of_topics():
    table = [(0, 0), (7, 7), (135, 8), (144, 8), (145, 9), (590, 17), (1065, 23), (3200, 40)]
    for n_reference, expected in table:
        assert topics.topic_count(n_reference, n_reference) == expected, n_reference
    # Fewer fit papers than K_MIN, or than the square root asks for: one topic per fit paper.
    for n_reference, n_fit, expected in [(500, 5, 5), (7, 3, 3), (1065, 20, 20), (10, 0, 0)]:
        assert topics.topic_count(n_reference, n_fit) == expected, (n_reference, n_fit)
    assert topics.K_MIN == 8 and topics.K_MAX == 40


def test_seeds_are_the_farthest_papers():
    first = dict(EXAMPLE["2608.90001"])
    near = [round(vectors.dot(EXAMPLE[p], first), 6) for p in IDS]
    assert near == [1.0, 0.96, 0.0, 0.0, 0.565685]

    work.reset()
    assert topics.fit(IDS, EXAMPLE, 2, 1).seeds == ["2608.90001", "2608.90003"]
    # After the third seed, 2608.90002 and 2608.90004 are both 0.96 from their nearest seed.
    assert topics.fit(IDS, EXAMPLE, 3, 1).seeds == ["2608.90001", "2608.90003", "2608.90005"]
    assert topics.fit(IDS, EXAMPLE, 4, 1).seeds == ["2608.90001", "2608.90003", "2608.90005", "2608.90002"]
    assert work.counts()["topics.fits"] == 3

    empty = topics.fit([], {}, 0, 12)
    assert empty == topics.Fit([], [], {}, {})
    assert work.counts()["topics.fits"] == 4


def test_the_topics_of_the_example():
    # The paper between the two seeds is equally near to both and goes to the lower topic.
    j, d = topics.nearest(EXAMPLE["2608.90005"], [dict(EXAMPLE["2608.90001"]), dict(EXAMPLE["2608.90003"])])
    assert (j, round(d, 6)) == (0, 0.565685)

    expected = [{"a": 0.802168, "b": 0.532975, "c": 0.269193}, {"c": 0.707107, "d": 0.707107}]
    for iterations in (1, 2):
        result = example_fit(iterations)
        assert [rounded(c) for c in result.centroids] == expected, iterations
        for centroid in result.centroids:
            assert list(centroid) == sorted(centroid), iterations
    result = example_fit(2)
    assert result.seeds == ["2608.90001", "2608.90003"]
    assert result.topic == {"2608.90001": 0, "2608.90002": 0, "2608.90003": 1, "2608.90004": 1,
                            "2608.90005": 0}
    assert rounded(result.sim) == {"2608.90001": 0.961519, "2608.90002": 0.90768, "2608.90003": 0.989949,
                                   "2608.90004": 0.989949, "2608.90005": 0.757566}
    # Two fits of the same input are the same doubles, not just the same to six places.
    assert example_fit(2) == result


def test_a_topic_without_members_keeps_its_seed():
    papers = {
        "2608.90011": [("a", 0.6), ("b", 0.8)],
        "2608.90012": [("a", 0.6), ("b", 0.8)],
        "2608.90013": [("c", 1.0)],
    }
    ids = sorted(papers)
    result = topics.fit(ids, papers, 3, 4)
    assert result.seeds == ["2608.90011", "2608.90013", "2608.90012"]
    assert result.centroids[2] == {"a": 0.6, "b": 0.8}
    assert result.topic == {"2608.90011": 0, "2608.90012": 0, "2608.90013": 1}
    assert 2 not in result.topic.values()


def test_papers_go_to_the_nearest_topic_above_the_floor():
    centroids = example_fit().centroids
    work.reset()
    cases = [
        ([("a", 1.0)], (0, 0.802168)),
        ([("d", 1.0)], (1, 0.707107)),
        ([("x", 1.0)], (None, 0.0)),
        ([], (None, 0.0)),
    ]
    for pairs, expected in cases:
        j, d = topics.assign(pairs, centroids, 0.08)
        assert (j, round(d, 6)) == expected, pairs
        assert isinstance(d, float), pairs
    assert work.counts()["topics.assigned"] == 4

    # The floor is inclusive: a dot equal to it is enough, the next double above it is not.
    _, d = topics.assign([("a", 1.0)], centroids, 0.0)
    assert topics.assign([("a", 1.0)], centroids, d) == (0, d)
    assert topics.assign([("a", 1.0)], centroids, math.nextafter(d, 2.0)) == (None, d)
    assert topics.assign([("a", 1.0)], [], 0.0) == (None, 0.0)
    assert work.counts()["topics.assigned"] == 8


def test_cards_show_terms_and_exemplars():
    centroid = {"a": 0.5, "b": 0.5, "c": 0.7, "d": 0.1}
    fit = topics.Fit(
        seeds=["2608.90021", "2608.90024"],
        centroids=[centroid, {"z": 1.0}],
        topic={"2608.90021": 0, "2608.90022": 0, "2608.90023": 0, "2608.90024": 1, "2608.90025": 0},
        sim={"2608.90021": 0.9, "2608.90022": 0.95, "2608.90023": 0.9, "2608.90024": 0.99, "2608.90025": 0.97},
    )
    titles = {p: "Title " + p[-1] for p in fit.topic}
    found = topics.card(0, centroid, fit, {"2608.90025"}, titles, 3, 3)
    assert found == {
        "terms": ["c", "a", "b"],
        "exemplars": [{"arxiv_id": "2608.90022", "title": "Title 2"},
                      {"arxiv_id": "2608.90021", "title": "Title 1"},
                      {"arxiv_id": "2608.90023", "title": "Title 3"}],
    }
    assert topics.card(0, centroid, fit, set(), titles, 10, 1)["exemplars"] == [
        {"arxiv_id": "2608.90025", "title": "Title 5"}]
    assert topics.card(0, centroid, fit, set(), titles, 10, 0)["exemplars"] == []
    assert topics.card(0, centroid, fit, set(), titles, 10, 9)["terms"] == ["c", "a", "b", "d"]

    # The example with 2608.90001 withdrawn; then 2608.90002 retitled: same exemplars, new title.
    result = example_fit()
    titles = {p: "Title " + p[-1] for p in IDS}
    first = topics.card(0, result.centroids[0], result, {"2608.90001"}, titles, 2, 2)
    assert first == {"terms": ["a", "b"],
                     "exemplars": [{"arxiv_id": "2608.90002", "title": "Title 2"},
                                   {"arxiv_id": "2608.90005", "title": "Title 5"}]}
    titles["2608.90002"] = "A new title"
    again = topics.card(0, result.centroids[0], result, {"2608.90001"}, titles, 2, 2)
    assert [e["arxiv_id"] for e in again["exemplars"]] == ["2608.90002", "2608.90005"]
    assert again["exemplars"][0]["title"] == "A new title"


def test_prompt_fallback_and_parse():
    messages = labels.prompt(example_card())
    assert messages == [
        {"role": "system", "content": labels.SYSTEM},
        {"role": "user", "content": "Terms: a, b\nPapers:\n- Title 1\n- Title 2\n"},
    ]
    broken = {"terms": ["a"], "exemplars": [{"arxiv_id": "2608.90001", "title": "Two\nlines"}]}
    assert labels.prompt(broken)[1]["content"] == "Terms: a\nPapers:\n- Two lines\n"
    assert labels.prompt({"terms": ["a"], "exemplars": []})[1]["content"] == "Terms: a\nPapers:\n"

    card = {"terms": ["graph", "embed", "node", "kernel"], "exemplars": []}
    assert labels.fallback(card) == {"name": "graph / embed / node", "blurb": "", "source": "fallback"}
    assert labels.fallback({"terms": ["speech"], "exemplars": []})["name"] == "speech"

    good = json.dumps({"name": "  Graph \n  Embeddings ", "blurb": "Papers\tabout  graphs. "})
    assert labels.parse(good) == {"name": "Graph Embeddings", "blurb": "Papers about graphs."}
    assert labels.parse(json.dumps({"name": "x" * 80, "blurb": ""})) == {"name": "x" * 80, "blurb": ""}
    refused = [
        json.dumps({"name": "Graphs"}),
        json.dumps({"blurb": "About graphs."}),
        json.dumps({"name": 5, "blurb": "About graphs."}),
        json.dumps({"name": "Graphs", "blurb": None}),
        json.dumps({"name": "   ", "blurb": "About graphs."}),
        json.dumps({"name": "x" * 81, "blurb": ""}),
        json.dumps({"name": "Graphs", "blurb": "y" * 301}),
        json.dumps(["Graphs", "About graphs."]),
        "not json",
        5,
        None,
    ]
    for content in refused:
        assert labels.parse(content) is None, content


class StubCompletions:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def create(self, **request):
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=reply))])


class StubClient:
    def __init__(self, replies):
        self.completions = StubCompletions(replies)
        self.chat = SimpleNamespace(completions=self.completions)
        self.closed = 0

    def close(self):
        self.closed += 1


def cards_of(n):
    return [{"terms": ["t%d" % j, "u%d" % j], "exemplars": [{"arxiv_id": "2608.9010%d" % j, "title": "T %d" % j}]}
            for j in range(n)]


@pytest.fixture
def app_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("ARXIVLENS_HOME", str(tmp_path / "home"))
    from app.config import settings
    return settings


def test_names_are_asked_one_by_one_in_topic_order(monkeypatch, app_settings):
    client = StubClient([
        json.dumps({"name": "First Topic", "blurb": "One."}),
        RuntimeError("endpoint down"),
        json.dumps({"name": "", "blurb": "Empty name."}),
        json.dumps({"name": "Fourth  Topic", "blurb": "Four."}),
    ])
    monkeypatch.setattr(labels, "connect", lambda: client)
    cards = cards_of(4)
    work.reset()
    names = labels.name_topics(cards)
    assert names == [
        {"name": "First Topic", "blurb": "One.", "source": "model"},
        {"name": "t1 / u1", "blurb": "", "source": "fallback"},
        {"name": "t2 / u2", "blurb": "", "source": "fallback"},
        {"name": "Fourth Topic", "blurb": "Four.", "source": "model"},
    ]
    assert work.counts()["labels.asked"] == 4
    requests = client.completions.requests
    assert [r["messages"] for r in requests] == [labels.prompt(card) for card in cards]
    for request in requests:
        assert request["model"] == app_settings.azure_openai_deployment
        assert request["response_format"] == {"type": "json_object"}
        assert request["temperature"] == 0
    assert client.closed == 1

    # ask on its own: one request, and None instead of an exception.
    failing = StubClient([OSError("connection refused")])
    work.reset()
    assert labels.ask(failing, cards[0]) is None
    assert len(failing.completions.requests) == 1
    assert work.counts()["labels.asked"] == 1


def test_no_endpoint_means_no_request(monkeypatch, app_settings):
    monkeypatch.setattr(app_settings, "azure_openai_endpoint", "")
    assert labels.connect() is None

    def refuse(client, card):
        raise AssertionError("a request was sent")

    monkeypatch.setattr(labels, "ask", refuse)
    cards = cards_of(3)
    work.reset()
    names = labels.name_topics(cards)
    assert [name["source"] for name in names] == ["fallback"] * 3
    assert names == [labels.fallback(card) for card in cards]
    assert work.counts()["labels.asked"] == 0
