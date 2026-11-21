"""A database written by the version before this one is upgraded in place when the app starts."""
from datetime import datetime, timedelta, timezone

import legacy_models as old
from conftest import feed

T0 = datetime(2025, 3, 1, 12, 0, tzinfo=timezone.utc)


def build_legacy_database(env) -> None:
    """Categories 1 (cs.AI) and 2 (cs.LG), and papers the way the old code left them."""

    def paper(pid, arxiv_id, category_id, status, error=None):
        base, version = arxiv_id.split("v")
        return {
            "id": pid, "category_id": category_id, "arxiv_id": arxiv_id,
            "title": f"Paper {base} (v{version})", "authors": ["A. Author"],
            "abstract": f"Abstract of {base}, version {version}.",
            "pdf_url": env.pdf_url(base, int(version)), "published_at": T0,
            "categories": ["cs.AI"], "status": status, "error_message": error,
            "created_at": T0 + timedelta(minutes=pid),
        }

    def analysis(aid, paper_id, label, minutes):
        return {
            "id": aid, "paper_id": paper_id, "full_text": f"text {label}", "summary": f"Summary {label}",
            "key_contributions": [f"c {label}"], "methodology": "m", "limitations": None,
            "keywords": ["kw", label], "novelty_score": 5, "created_at": T0 + timedelta(minutes=minutes),
        }

    def build(conn):
        old.LegacyBase.metadata.create_all(conn)
        conn.execute(old.Category.__table__.insert(), [
            {"id": 1, "name": "Artificial Intelligence", "arxiv_code": "cs.AI", "added_at": T0},
            {"id": 2, "name": "Machine Learning", "arxiv_code": "cs.LG", "added_at": T0},
        ])
        conn.execute(old.Paper.__table__.insert(), [
            # one paper stored three times, as "new" papers of two categories; the newest row
            # has no analysis, the two older rows have one each
            paper(1, "2401.00001v1", 2, "done"),
            paper(2, "2401.00001v2", 1, "done"),
            paper(3, "2401.00001v3", 2, "failed", "duplicate key value violates unique constraint"),
            # two rows, the newest has its own analysis
            paper(4, "2401.00002v1", 1, "done"),
            paper(5, "2401.00002v4", 1, "done"),
            # single rows in every state the old code could leave behind
            paper(6, "2401.00003v1", 1, "failed", "boom"),
            paper(7, "2401.00004v2", 2, "processing"),
            paper(8, "2401.00005v1", 1, "pending"),
            paper(9, "2401.00006v1", 1, "failed", "analysed twice"),  # failed, but analysed
            # two rows without any analysis
            paper(10, "2401.00007v1", 1, "failed", "boom"),
            paper(11, "2401.00007v2", 1, "processing"),
        ])
        conn.execute(old.Analysis.__table__.insert(), [
            analysis(1, 1, "1-v1", 10),
            analysis(2, 2, "1-v2", 20),
            analysis(3, 4, "2-v1", 30),
            analysis(4, 5, "2-v4", 5),
            analysis(5, 9, "6-v1", 40),
        ])
        conn.execute(old.Conversation.__table__.insert(), [
            {"id": 1, "title": "Old chat", "research_interest": "graphs", "created_at": T0, "updated_at": T0},
        ])
        conn.execute(old.Message.__table__.insert(), [
            {"id": 1, "conversation_id": 1, "role": old.MessageRole.user, "content": "hello", "created_at": T0},
        ])
        if conn.dialect.name == "postgresql":
            for table in ("categories", "papers", "analyses", "conversations", "messages"):
                conn.exec_driver_sql(
                    f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), (SELECT max(id) FROM {table}))"
                )

    env.db(build)


def test_legacy_database_is_upgraded_in_place(env):
    build_legacy_database(env)
    app = env.start_app()

    def check_upgraded():
        papers = app.by_arxiv_id()
        # one row per paper, the id without its version, the version in its own field
        assert sorted(papers) == [f"2401.0000{n}" for n in range(1, 8)]
        assert len(app.papers()) == 7
        return papers

    papers = check_upgraded()
    summary = lambda p: app.get(f"/papers/{p['id']}/analysis").json()["summary"]

    # Highest version wins, joins the categories of all rows, takes the newest analysis.
    p = papers["2401.00001"]
    assert (p["id"], p["version"], p["category_ids"], p["category_id"]) == (3, 3, [1, 2], 1)
    assert (p["status"], p["attempts"], p["error_message"]) == ("done", 0, None)
    assert p["title"] == "Paper 2401.00001 (v3)" and summary(p) == "Summary 1-v2"
    # ... or keeps its own, even if another row's is newer.
    p = papers["2401.00002"]
    assert (p["id"], p["version"], p["category_ids"], p["status"]) == (5, 4, [1], "done")
    assert summary(p) == "Summary 2-v4"
    # A paper with an analysis is done, whatever the old code said.
    p = papers["2401.00006"]
    assert (p["version"], p["status"], p["attempts"], p["error_message"]) == (1, "done", 0, None)
    assert summary(p) == "Summary 6-v1"
    # Other failed and processing papers are pending with 0 attempts.
    for arxiv_id, pid, version, cats in [("2401.00003", 6, 1, [1]), ("2401.00004", 7, 2, [2]),
                                         ("2401.00005", 8, 1, [1]), ("2401.00007", 11, 2, [1])]:
        p = papers[arxiv_id]
        assert (p["id"], p["version"], p["category_ids"]) == (pid, version, cats), arxiv_id
        assert (p["status"], p["attempts"], p["error_message"]) == ("pending", 0, None), arxiv_id
        assert app.get(f"/papers/{p['id']}/analysis").status_code == 404
    assert app.status() == {"running": False, "run": None,
                            "papers": {"pending": 4, "processing": 0, "done": 3, "failed": 0}}
    assert set(app.by_arxiv_id(category_id=2)) == {"2401.00001", "2401.00004"}

    # Three analyses are left of five, and the rest of the data is untouched.
    assert env.db(lambda c: c.exec_driver_sql("SELECT count(*) FROM analyses").scalar()) == 3
    assert [c["title"] for c in app.get("/conversations").json()] == ["Old chat"]
    assert [m["content"] for m in app.get("/conversations/1/messages").json()] == ["hello"]
    assert {c["id"]: c["arxiv_code"] for c in app.categories().values()} == {1: "cs.AI", 2: "cs.LG"}

    # The upgraded database works: a feed that lists a stored paper, a new version of another
    # one and a new paper; the pending papers get analysed.
    env.set_scenario(feeds={"cs.AI": feed(("2401.00001", 3, "new"), ("2401.00002", 5, "replace"),
                                          ("2401.00008", 1, "new"))})
    run = app.cycle()
    assert (run["new_papers"], run["updated"], run["skipped"], run["processed"], run["failed"]) == (1, 1, 0, 5, 0)
    papers = app.by_arxiv_id()
    assert len(papers) == 8 and all(p["status"] == "done" for p in papers.values())
    assert papers["2401.00002"]["version"] == 5 and summary(papers["2401.00002"]) == "Summary 2-v4"
    assert papers["2401.00003"]["attempts"] == 1 and summary(papers["2401.00003"]) == "Summary of 2401.00003."
    assert env.counts().get("llm:2401.00001") is None, "already analysed papers are not analysed again"

    # Starting again on the upgraded database changes nothing.
    before = app.papers()
    app.stop()
    app = env.start_app()
    assert app.papers() == before
    assert app.status()["papers"] == {"pending": 0, "processing": 0, "done": 8, "failed": 0}


def test_current_database_is_left_as_it_is_on_restart(env):
    """The upgrade only happens to an old database: failed papers of a current one stay failed."""
    env.set_scenario(feeds={"cs.AI": feed(("2401.00001", 1, "new"))}, bad=["2401.00001"])
    env.set_yaml_categories(["cs.AI"])
    app = env.start_app()
    app.cycle()
    before = app.papers()
    assert [(p["status"], p["attempts"]) for p in before] == [("failed", 1)]
    app.stop()
    app = env.start_app()
    assert app.papers() == before
