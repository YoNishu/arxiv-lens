"""Ingestion: one row per paper, versions, announce types, categories, big feeds."""
from conftest import feed

A, B, C, D = "2401.00001", "2401.00002", "2401.00003", "2401.00004"


def test_stored_papers_new_versions_and_announce_types(env):
    env.set_scenario(feeds={"cs.AI": feed((A, 1, "new"), (B, 3, "replace"), (C, 2, "replace-cross"))})
    app = env.start_app()
    cs_ai = app.add_category("cs.AI")["id"]
    app.wait_idle()

    # new stores a paper; replace and replace-cross of papers not stored never create one.
    papers = app.by_arxiv_id()
    assert set(papers) == {A}
    assert (papers[A]["arxiv_id"], papers[A]["version"], papers[A]["status"]) == (A, 1, "done")

    # A feed that lists a stored paper next to a new one: no unique-key error, nothing lost,
    # nothing doubled. cross stores a paper too.
    env.set_scenario(feeds={"cs.AI": feed((A, 1, "new"), (D, 1, "cross"), (B, 3, "replace"))})
    run = app.cycle()
    assert (run["new_papers"], run["updated"], run["skipped"], run["processed"], run["failed"]) == (1, 0, 0, 1, 0)
    papers = app.by_arxiv_id()
    assert set(papers) == {A, D} and len(app.papers()) == 2
    first_id, first_created = papers[A]["id"], papers[A]["created_at"]

    # The same feed again changes nothing.
    run = app.cycle()
    assert (run["new_papers"], run["updated"], run["processed"]) == (0, 0, 0)
    assert len(app.papers()) == 2

    # A replacement of a stored paper updates version, title, abstract and pdf_url in place and
    # keeps the analysis: the paper is not analysed again.
    analysis_before = app.get(f"/papers/{first_id}/analysis").json()
    env.set_scenario(feeds={"cs.AI": feed((A, 2, "replace"), (D, 1, "new"))})
    run = app.cycle()
    assert (run["new_papers"], run["updated"], run["processed"], run["failed"]) == (0, 1, 0, 0)
    papers = app.by_arxiv_id()
    assert set(papers) == {A, D} and len(app.papers()) == 2
    a = papers[A]
    assert (a["id"], a["created_at"], a["arxiv_id"]) == (first_id, first_created, A)
    assert (a["version"], a["title"], a["abstract"], a["pdf_url"]) == (
        2, f"Paper {A} (v2)", f"Abstract of {A}, version 2.", env.pdf_url(A, 2))
    assert (a["status"], a["attempts"], a["category_ids"]) == ("done", 1, [cs_ai])
    assert app.get(f"/papers/{first_id}/analysis").json() == analysis_before
    assert env.counts()[f"llm:{A}"] == 1

    # The same replacement again, or an older one, is not an update.
    run = app.cycle()
    assert run["updated"] == 0 and app.paper(A)["version"] == 2
    api_calls = env.counts()["api"]
    app.cycle()
    assert env.counts()["api"] == api_calls, "a feed with nothing new does not go to the API"

    # replace-cross works the same way.
    env.set_scenario(feeds={"cs.AI": feed((A, 4, "replace-cross"))})
    run = app.cycle()
    assert run["updated"] == 1 and app.paper(A)["version"] == 4
    assert app.paper(A)["title"] == f"Paper {A} (v4)" and env.counts()[f"llm:{A}"] == 1


def test_paper_belongs_to_every_category_that_lists_it(env):
    env.set_scenario(feeds={
        "cs.AI": feed((A, 1, "new"), (B, 1, "new")),
        "cs.LG": feed((A, 1, "cross"), (C, 1, "new")),
        "stat.ML": feed((A, 2, "replace-cross")),
    })
    app = env.start_app()
    ai = app.add_category("cs.AI")["id"]
    lg = app.add_category("cs.LG")["id"]
    ml = app.add_category("stat.ML")["id"]
    assert ai < lg < ml
    app.wait_idle()
    app.cycle()

    papers = app.by_arxiv_id()
    assert set(papers) == {A, B, C} and len(app.papers()) == 3
    # category_ids ascending, category_id the first of them
    assert papers[A]["category_ids"] == [ai, lg, ml] and papers[A]["category_id"] == ai
    assert papers[B]["category_ids"] == [ai] and papers[C]["category_ids"] == [lg]
    assert papers[C]["category_id"] == lg
    assert set(app.by_arxiv_id(category_id=ai)) == {A, B}
    assert set(app.by_arxiv_id(category_id=lg)) == {A, C}
    assert set(app.by_arxiv_id(category_id=ml)) == {A}
    assert env.counts()[f"llm:{A}"] == 1, "one paper, one analysis, however many categories"
    # the browse page filters the same way
    page = app.get(f"/ui/papers?category={lg}").text
    assert f"Paper {A}" in page and f"Paper {C}" in page and f"Paper {B}" not in page

    # Deleting a category deletes only the papers no other category lists.
    assert app.delete(f"/categories/{ai}").status_code == 204
    papers = app.by_arxiv_id()
    assert set(papers) == {A, C}
    assert papers[A]["category_ids"] == [lg, ml] and papers[A]["category_id"] == lg
    assert papers[A]["status"] == "done" and app.get(f"/papers/{papers[A]['id']}/analysis").status_code == 200
    assert app.papers(category_id=ai) == []
    assert app.status()["papers"]["done"] == 2
    assert f"Paper {A}" in app.get(f"/ui/paper/{papers[A]['id']}").text

    # The UI's delete button goes the same way.
    gone = app.post(f"/ui/categories/{lg}/delete", follow_redirects=False)
    assert gone.status_code == 303
    papers = app.by_arxiv_id()
    assert set(papers) == {A} and papers[A]["category_ids"] == [ml]
    assert app.delete(f"/categories/{ml}").status_code == 204
    assert app.papers() == [] and app.categories() == {}
    assert app.get("/daily-digest").json() == []


def test_whole_feed_is_stored_in_one_cycle_and_missing_ids_are_skipped(env):
    ids = [f"2402.{n:05d}" for n in range(1, 251)]
    gone = [ids[4], ids[120], ids[249]]
    env.set_scenario(feeds={"cs.AI": feed(*[(i, 1, "new") for i in ids])}, gone=gone)
    app = env.start_app()
    app.add_category("cs.AI")
    status = app.wait_idle(timeout=300)

    run = status["run"]
    assert (run["new_papers"], run["skipped"], run["processed"], run["failed"]) == (247, 3, 247, 0)
    papers = app.by_arxiv_id()
    assert set(papers) == set(ids) - set(gone)
    assert status["papers"]["done"] == 247

    # The ids the API does not have are asked for again by the next cycle, and counted again.
    run = app.cycle()
    assert (run["new_papers"], run["skipped"], run["processed"]) == (0, 3, 0)
    # ... and stored once the API has them.
    env.set_scenario(feeds={"cs.AI": feed(*[(i, 1, "new") for i in ids])}, gone=gone[:1])
    run = app.cycle()
    assert (run["new_papers"], run["skipped"], run["processed"]) == (2, 1, 2)
    assert len(app.papers()) == 249
