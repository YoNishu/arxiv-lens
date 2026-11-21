"""Cycles: started in the background, one at a time per process, recorded in the database."""
import time

from conftest import feed, wait_until

A, B, C = "2401.00001", "2401.00002", "2401.00003"


def test_triggers_answer_at_once_and_overlapping_triggers_share_one_cycle(env):
    env.set_scenario(feeds={"cs.AI": feed((A, 1, "new"))}, slow=3)
    env.set_yaml_categories(["cs.AI"])
    app = env.start_app()

    # POST /process: 202 at once, although the cycle takes seconds.
    began = time.monotonic()
    first = app.post("/process")
    assert time.monotonic() - began < 1.5
    assert first.status_code == 202
    assert first.json()["started"] is True
    run_id = first.json()["run_id"]

    # While it runs: no second cycle, the same run_id, started false.
    status = app.status()
    assert status["running"] is True
    assert status["run"]["id"] == run_id and status["run"]["finished_at"] is None
    again = app.post("/process")
    assert again.status_code == 202
    assert (again.json()["run_id"], again.json()["started"]) == (run_id, False)

    # The refresh button does not hold its request either, and starts nothing on top.
    began = time.monotonic()
    refresh = app.post("/ui/refresh", follow_redirects=False)
    assert time.monotonic() - began < 1.5
    assert (refresh.status_code, refresh.headers["location"]) == (303, "/ui")
    wait_until(lambda: (app.paper(A) or {}).get("status") == "processing", what="the paper to be in work")
    assert app.status()["papers"] == {"pending": 0, "processing": 1, "done": 0, "failed": 0}
    third = app.post("/process").json()
    assert (third["run_id"], third["started"]) == (run_id, False)
    # the rest of the app answers while the cycle is busy
    assert app.get("/ui").status_code == 200 and app.get("/papers").status_code == 200

    status = app.wait_idle()
    run = status["run"]
    assert run["id"] == run_id and run["finished_at"] is not None
    assert (run["new_papers"], run["processed"], run["failed"]) == (1, 1, 0)
    paper = app.paper(A)
    assert (paper["status"], paper["attempts"]) == ("done", 1)
    assert env.counts()[f"llm:{A}"] == 1 and env.counts()["rss:cs.AI"] == 1

    # Once it is over, a trigger starts the next cycle, with the next id.
    env.set_scenario(feeds={"cs.AI": feed((A, 1, "new"), (B, 1, "new"))})
    refresh = app.post("/ui/refresh", follow_redirects=False)
    assert refresh.status_code == 303
    status = app.wait_idle()
    assert status["run"]["id"] > run_id and status["run"]["new_papers"] == 1
    assert app.paper(B)["status"] == "done"
    assert f"Paper {B}" in app.get("/ui").text

    # Cycles are recorded in the database, one row each.
    def runs(conn):
        return conn.exec_driver_sql(
            "SELECT id, new_papers, processed, failed, finished_at IS NOT NULL FROM runs ORDER BY id"
        ).fetchall()

    assert [tuple(r) for r in env.db(runs)] == [(run_id, 1, 1, 0, True), (status["run"]["id"], 1, 1, 0, True)]


def test_scheduler_is_on_by_default_and_its_cycle_is_a_cycle_like_any_other(env):
    # No SCHEDULER_ENABLED at all: the scheduler runs a cycle at startup, nobody has to ask.
    env.set_scenario(feeds={"cs.AI": feed((A, 1, "new"))}, slow=3)
    env.set_yaml_categories(["cs.AI"])
    app = env.start_app(SCHEDULER_ENABLED=None)
    wait_until(lambda: (app.paper(A) or {}).get("status") == "processing", what="the startup cycle")
    status = app.status()
    assert status["running"] is True and status["run"]["id"] == 1

    # A manual trigger on top of the scheduled cycle joins it instead of starting another.
    answer = app.start_cycle()
    assert (answer["run_id"], answer["started"]) == (1, False)
    status = app.wait_idle()
    assert status["run"]["id"] == 1 and (status["run"]["new_papers"], status["run"]["processed"]) == (1, 1)
    assert (app.paper(A)["status"], app.paper(A)["attempts"]) == ("done", 1)
    assert env.counts()[f"llm:{A}"] == 1

    # With SCHEDULER_ENABLED=false the same start does nothing by itself.
    app.stop()
    env.set_scenario(feeds={"cs.AI": feed((A, 1, "new"), (B, 1, "new"))})
    app = env.start_app()
    time.sleep(1.5)
    assert app.status()["run"]["id"] == 1 and app.paper(B) is None
    assert app.cycle()["id"] == 2 and app.paper(B)["status"] == "done"


def test_adding_a_category_starts_a_cycle_in_the_background(env):
    env.set_scenario(feeds={"cs.AI": feed((A, 1, "new")), "cs.LG": feed((B, 1, "new")),
                            "cs.CV": feed((C, 1, "new"))}, slow=2)
    app = env.start_app()

    # Through the API
    began = time.monotonic()
    created = app.post("/categories", json={"arxiv_code": "cs.AI", "name": "AI"})
    assert time.monotonic() - began < 1.5
    assert created.status_code == 201 and created.json()["name"] == "AI"
    assert app.status()["running"] is True

    # Through the UI, while that cycle is still running: answered at once as well, and the new
    # feed is polled by a cycle that follows the running one.
    began = time.monotonic()
    added = app.post("/ui/categories/add", data={"arxiv_code": " cs.LG "}, follow_redirects=False)
    assert time.monotonic() - began < 1.5
    assert (added.status_code, added.headers["location"]) == (303, "/ui/categories")
    assert set(app.categories()) == {"cs.AI", "cs.LG"}

    status = app.wait_idle()
    papers = app.by_arxiv_id()
    assert set(papers) == {A, B}
    assert [(papers[i]["status"], papers[i]["attempts"]) for i in (A, B)] == [("done", 1)] * 2
    assert status["papers"]["done"] == 2
    assert env.counts()[f"llm:{A}"] == 1 and env.counts()[f"llm:{B}"] == 1

    # Through the UI with nothing running; adding it twice changes nothing.
    env.set_scenario(feeds={"cs.CV": feed((C, 1, "new"))})
    added = app.post("/ui/categories/add", data={"arxiv_code": "cs.CV"}, follow_redirects=False)
    assert added.status_code == 303
    app.post("/ui/categories/add", data={"arxiv_code": "cs.CV"}, follow_redirects=False)
    app.wait_idle()
    assert set(app.categories()) == {"cs.AI", "cs.LG", "cs.CV"}
    assert app.paper(C)["status"] == "done" and len(app.papers()) == 3
