"""Rows of papers.jsonl projected from the facts of one paper."""
from datetime import datetime, timedelta, timezone

from app.insights import snapshot, work

KEYS = {
    "abstract", "announced", "arxiv_id", "authors", "categories", "feeds", "first_abstract", "first_title",
    "keywords", "novelty", "published", "state", "text_chars", "text_sha256", "title", "updated", "version",
    "withdrawn",
}
SHA = "6b274a7c582d78f81f72937961e64748216cd72aca87dd5c28c93d71cdb91d0c"
OTHER_SHA = "0" * 64


def _at(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def _analysis(version: int) -> dict:
    return {"paper_version": version, "keywords": ["Sparse routing", "graph embeddings"], "novelty_score": 7}


def _text(version: int) -> dict:
    return {"version": version, "sha256": SHA, "chars": 485}


def _facts(**changes) -> dict:
    facts = {
        "arxiv_id": "2608.90105",
        "version": 2,
        "title": "Sparse Routing for Graph Embeddings",
        "authors": ["K. Maren", "T. Soval"],
        "abstract": "We study routing in graph embeddings.",
        "categories": ["cs.LG", "stat.ML"],
        "published_at": _at("2026-08-12T17:43:09"),
        "updated_at": _at("2026-08-20T09:12:44"),
        "created_at": _at("2026-08-13T06:00:00"),
        "withdrawn_at": None,
        "error_message": None,
        "feeds": ["cs.LG", "stat.ML"],
        "analysis": _analysis(2),
        "text": _text(2),
        "first": {"title": "Sparse Routes for Graph Embeddings", "abstract": "We study routing in graph embeddings."},
    }
    facts.update(changes)
    return facts


def test_state_comes_from_durable_facts():
    work.reset()
    rows = [
        ("no analysis, no error", {"analysis": None}, "pending"),
        ("analysis of the current version", {}, "analysed"),
        ("analysis of version 1 on a paper at version 2", {"analysis": _analysis(1)}, "pending"),
        ("the same with an error text", {"analysis": _analysis(1), "error_message": "PDF fetch failed"}, "failed"),
        ("an error text and no analysis", {"analysis": None, "error_message": "model error"}, "failed"),
        ("an error text with an analysis of the current version", {"error_message": "model error"}, "analysed"),
        ("a withdrawn paper keeps its state", {"withdrawn_at": _at("2026-08-21T08:00:00")}, "analysed"),
    ]
    for name, changes, state in rows:
        row = snapshot.project(_facts(**changes))
        assert row["state"] == state, name
    assert work.counts()["snapshot.rows"] == len(rows)
    assert snapshot.project(_facts(withdrawn_at=_at("2026-08-21T08:00:00")))["withdrawn"] is True
    assert snapshot.project(_facts())["withdrawn"] is False


def test_analysis_and_text_count_only_for_the_current_version():
    keywords = ["Sparse routing", "graph embeddings"]
    rows = [
        ("analysis and text of the current version", {}, keywords, 7, SHA, 485),
        ("analysis of an older version", {"analysis": _analysis(1)}, None, None, None, None),
        ("text of an older version", {"text": _text(1)}, keywords, 7, None, None),
        ("no text row", {"text": None}, keywords, 7, None, None),
        ("a current text without a current analysis", {"analysis": _analysis(1), "error_message": "x"},
         None, None, None, None),
        ("no analysis at all", {"analysis": None}, None, None, None, None),
    ]
    for name, changes, kept, novelty, sha, chars in rows:
        row = snapshot.project(_facts(**changes))
        assert row["keywords"] == kept, name
        assert row["novelty"] == novelty, name
        assert row["text_sha256"] == sha, name
        assert row["text_chars"] == chars, name
    other = snapshot.project(_facts(text={"version": 2, "sha256": OTHER_SHA, "chars": 12}))
    assert (other["text_sha256"], other["text_chars"]) == (OTHER_SHA, 12)


def test_first_stored_title_and_abstract():
    row = snapshot.project(_facts())
    assert row["first_title"] == "Sparse Routes for Graph Embeddings"
    assert row["title"] == "Sparse Routing for Graph Embeddings"
    assert row["first_abstract"] == "We study routing in graph embeddings."
    row = snapshot.project(_facts(first={"title": "Old title", "abstract": "Old abstract."}))
    assert (row["first_title"], row["first_abstract"]) == ("Old title", "Old abstract.")
    row = snapshot.project(_facts(first=None, abstract="Current abstract."))
    assert row["first_title"] == "Sparse Routing for Graph Embeddings"
    assert row["first_abstract"] == "Current abstract."


def test_days_and_instants_of_a_row():
    rows = [
        ("last second of a day", _at("2026-08-13T23:59:59"), "2026-08-13"),
        ("first second of the next day", _at("2026-08-14T00:00:00"), "2026-08-14"),
        ("a time given in another zone", datetime(2026, 8, 14, 1, 30, tzinfo=timezone(timedelta(hours=2))),
         "2026-08-13"),
        ("a time without a zone is UTC", datetime(2026, 8, 13, 23, 59, 59), "2026-08-13"),
    ]
    for name, created, day in rows:
        assert snapshot.project(_facts(created_at=created))["announced"] == day, name
    row = snapshot.project(_facts(published_at=_at("2026-08-12T17:43:09") + timedelta(microseconds=900000),
                                  updated_at=None))
    assert row["published"] == "2026-08-12T17:43:09Z"
    assert row["updated"] is None
    row = snapshot.project(_facts())
    assert row["updated"] == "2026-08-20T09:12:44Z"


def test_a_row_has_exactly_its_keys():
    facts = _facts(
        abstract="We study routing in graph embeddings. ...",
        first={"title": "Sparse Routes for Graph Embeddings", "abstract": "We study routing in graph embeddings. ..."},
        analysis={"paper_version": 2, "keywords": ["Sparse routing", "graph embeddings", "Mixture of experts"],
                  "novelty_score": 7},
        feeds=["stat.ML", "cs.LG"],
    )
    row = snapshot.project(facts)
    assert set(row) == KEYS
    assert len(row) == 18
    assert row == {
        "abstract": "We study routing in graph embeddings. ...", "announced": "2026-08-13", "arxiv_id": "2608.90105",
        "authors": ["K. Maren", "T. Soval"], "categories": ["cs.LG", "stat.ML"], "feeds": ["cs.LG", "stat.ML"],
        "first_abstract": "We study routing in graph embeddings. ...",
        "first_title": "Sparse Routes for Graph Embeddings",
        "keywords": ["Sparse routing", "graph embeddings", "Mixture of experts"], "novelty": 7,
        "published": "2026-08-12T17:43:09Z", "state": "analysed", "text_chars": 485, "text_sha256": SHA,
        "title": "Sparse Routing for Graph Embeddings", "updated": "2026-08-20T09:12:44Z", "version": 2,
        "withdrawn": False,
    }
    pending = snapshot.project(_facts(analysis=None, text=None, first=None))
    assert set(pending) == KEYS
    for key in ("keywords", "novelty", "text_sha256", "text_chars"):
        assert pending[key] is None, key
    # Facts beyond the row's own never leak into it.
    extra = snapshot.project(_facts(error_message="model error", analysis=None))
    assert set(extra) == KEYS
    assert "model error" not in repr(extra)
