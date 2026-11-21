"""The vocabulary of the reference papers, TF-IDF vectors and the vector helpers."""
import math
import random
from datetime import date

from app.insights import canon, vectors, vocab, work

SEPTEMBER = date(2026, 9, 1)

# Five small docs: graph is in four of them, speech in three, rare in one, the others in two.
DOCS = [
    {"graph": 2, "embed": 1, "node": 1, "rare": 3},
    {"graph": 1, "node": 2, "kernel": 1},
    {"graph": 1, "embed": 1, "speech": 1},
    {"speech": 2, "audio": 1, "kernel": 1},
    {"speech": 1, "audio": 1, "graph": 1},
]


def paper(arxiv_id, announced, state="analysed", withdrawn=False):
    return {"arxiv_id": arxiv_id, "announced": announced, "state": state, "withdrawn": withdrawn}


def example_idf():
    return {term: idf for term, (_, idf) in vocab.fit(DOCS, 2, 0.6).items()}


def rounded(pairs):
    return [(term, round(weight, 6)) for term, weight in pairs]


def test_reference_papers_are_those_announced_before_the_month():
    papers = [
        paper("2608.90101", "2026-09-01"),
        paper("2608.90102", "2026-08-31", state="pending"),
        paper("2608.90103", "2026-08-03", state="failed", withdrawn=True),
        paper("2608.90104", "2026-09-02"),
        paper("2608.90105", "2026-08-31"),
    ]
    ids, whole_library = vocab.reference_ids(papers, SEPTEMBER, 3)
    assert ids == ["2608.90102", "2608.90103", "2608.90105"]
    assert whole_library is False


def test_a_small_library_is_its_own_reference():
    papers = [
        paper("2608.90204", "2026-09-03"),
        paper("2608.90201", "2026-08-20"),
        paper("2608.90203", "2026-09-01"),
        paper("2608.90202", "2026-08-31"),
    ]
    ids, whole_library = vocab.reference_ids(papers, SEPTEMBER, 3)
    assert ids == ["2608.90201", "2608.90202", "2608.90203", "2608.90204"]
    assert whole_library is True

    papers.append(paper("2608.90205", "2026-08-02", withdrawn=True))
    ids, whole_library = vocab.reference_ids(papers, SEPTEMBER, 3)
    assert ids == ["2608.90201", "2608.90202", "2608.90205"]
    assert whole_library is False


def test_the_vocabulary_of_the_example():
    work.reset()
    fitted = vocab.fit(DOCS, 2, 0.6)
    assert list(fitted) == ["audio", "embed", "kernel", "node", "speech"]
    assert "graph" not in fitted          # in 4 of 5 docs, above 0.6 * 5
    assert "rare" not in fitted           # in one doc only
    assert {term: (df, round(idf, 6)) for term, (df, idf) in fitted.items()} == {
        "audio": (2, 1.693147), "embed": (2, 1.693147), "kernel": (2, 1.693147),
        "node": (2, 1.693147), "speech": (3, 1.405465),
    }
    for _, idf in fitted.values():
        assert isinstance(idf, float)
    assert work.counts()["vocab.fits"] == 1


def test_an_empty_fit_still_counts():
    work.reset()
    assert vocab.fit([], 3, 0.4) == {}
    assert work.counts()["vocab.fits"] == 1


def test_vectors_of_the_example():
    idf = example_idf()
    work.reset()
    row = vectors.build_row("2608.90105", {"embed": 1, "node": 2, "graph": 3}, idf)
    assert row["arxiv_id"] == "2608.90105"
    assert [term for term, _ in row["vector"]] == ["embed", "node"]
    assert rounded(row["vector"]) == [("embed", 0.508542), ("node", 0.861037)]
    assert work.counts()["vectors.built"] == 1

    pairs = vectors.weigh({"speech": 1, "audio": 4, "kernel": 1, "zzz": 9}, idf)
    assert rounded(pairs) == [("audio", 0.878202), ("kernel", 0.368019), ("speech", 0.305489)]
    assert vectors.weigh({"graph": 5}, idf) == []
    assert vectors.build_row("2608.90106", {"graph": 5}, idf)["vector"] == []
    assert work.counts()["vectors.built"] == 2


def test_a_vector_has_length_one():
    idf = example_idf()
    for terms in ({"embed": 1, "node": 2, "graph": 3}, {"speech": 1, "audio": 4, "kernel": 1, "zzz": 9}):
        pairs = vectors.weigh(terms, idf)
        assert abs(math.fsum(weight * weight for _, weight in pairs) - 1.0) < 1e-12


def test_dot_is_the_same_from_both_sides():
    chance = random.Random(7)
    words = ["t%02d" % i for i in range(40)]
    # idf as the fit makes it for a library of 60 docs and a df between 1 and 30
    idf = {word: (canon.ln(61) - canon.ln(chance.randint(1, 30) + 1)) + 1.0 for word in words}
    shared = 0
    for _ in range(50):
        a = vectors.weigh({word: chance.randint(1, 9) for word in chance.sample(words, 12)}, idf)
        b = vectors.weigh({word: chance.randint(1, 9) for word in chance.sample(words, 12)}, idf)
        forward = vectors.dot(a, dict(b))
        assert forward == vectors.dot(b, dict(a))
        if forward > 0.0:
            shared += 1
    assert shared > 25


def test_probe_takes_the_heaviest_terms():
    pairs = [("alpha", 0.2), ("beta", 0.5), ("delta", 0.7), ("gamma", 0.5)]
    assert vectors.probe(pairs, 3) == ["delta", "beta", "gamma"]
    assert vectors.probe(pairs, 1) == ["delta"]
    assert vectors.probe(pairs, 10) == ["delta", "beta", "gamma", "alpha"]
    assert vectors.probe([], 4) == []

    pairs = vectors.weigh({"speech": 1, "audio": 4, "kernel": 1, "zzz": 9}, example_idf())
    assert vectors.probe(pairs, 2) == ["audio", "kernel"]
