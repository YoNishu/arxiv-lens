"""Cycles: started in the background, one at a time per process, recorded in the database."""
import json

import pytest

from conftest import feed, new_home, new_server, new_standin, use_test_database, wait_until

A, B, C, D, E = "2401.90001", "2401.90002", "2401.90003", "2401.90004", "2401.90005"


def answer(reply: tuple[int, str]):
    status, body = reply
    assert status in (200, 201, 202), reply
    return json.loads(body)


def paper(server, arxiv_id: str) -> dict:
    return {p["arxiv_id"]: p for p in answer(server.get("/papers"))}.get(arxiv_id) or {}


def wait_idle(server) -> dict:
    return wait_until(lambda: (lambda s: s if not s["running"] else None)(answer(server.get("/status"))),
                      timeout=120, what="the cycle to end")


class Shared:
    def __init__(self, home, world, app, at_start):
        self.home, self.world, self.app, self.at_start = home, world, app, at_start


@pytest.fixture(scope="module")
def shared(tmp_path_factory):
    """One server, scheduler off, for the trigger cases; what it showed right after its start."""
    folder = tmp_path_factory.mktemp("cycles")
    world = new_standin(folder / "standin", scenario={"feeds": {}})
    home = use_test_database(new_home(folder / "home")).join(world).seeds("cs.AI")
    app = new_server(home)
    try:
        yield Shared(home, world, app, (answer(app.get("/status")), world.counts()))
    finally:
        app.stop()
        world.close()


def test_triggers_answer_at_once_and_overlapping_triggers_share_one_cycle(shared):
    app, world = shared.app, shared.world
    # Started with the scheduler off, the server did nothing by itself.
    status, counts = shared.at_start
    assert status["run"] is None and status["running"] is False and counts == {}

    world.scenario(feeds={"cs.AI": feed((A, 1, "new"))})
    world.control(slow=4)
    polled = world.counts().get("rss:cs.AI", 0)
    # POST /process answers while the analysis is still being waited for.
    first = app.post("/process")
    assert first[0] == 202, first
    run_id = json.loads(first[1])["run_id"]
    assert json.loads(first[1])["started"] is True
    assert paper(app, A).get("status") != "done"

    # While it runs: no second cycle, the same run_id, started false.
    status = answer(app.get("/status"))
    assert status["running"] is True
    assert status["run"]["id"] == run_id and status["run"]["finished_at"] is None
    again = answer(app.post("/process"))
    assert (again["run_id"], again["started"]) == (run_id, False)

    # The refresh button does not hold its request either, and starts nothing on top.
    refresh = app.post("/ui/refresh")
    assert refresh[0] == 303 and app.headers["location"] == "/ui"
    assert paper(app, A).get("status") != "done"
    wait_until(lambda: paper(app, A).get("status") == "processing", what="the paper to be in work")
    assert answer(app.get("/status"))["papers"] == {"pending": 0, "processing": 1, "done": 0, "failed": 0}
    third = answer(app.post("/process"))
    assert (third["run_id"], third["started"]) == (run_id, False)
    # the rest of the app answers while the cycle is busy
    assert app.get("/ui")[0] == 200 and app.get("/papers")[0] == 200

    status = wait_idle(app)
    run = status["run"]
    assert run["id"] == run_id and run["finished_at"] is not None
    assert (run["new_papers"], run["processed"], run["failed"]) == (1, 1, 0)
    assert (paper(app, A)["status"], paper(app, A)["attempts"]) == ("done", 1)
    assert world.counts()[f"llm:{A}"] == 1 and world.counts()["rss:cs.AI"] == polled + 1

    # Once it is over, a trigger starts the next cycle, with the next id.
    world.control()
    world.scenario(feeds={"cs.AI": feed((A, 1, "new"), (B, 1, "new"))})
    assert app.post("/ui/refresh")[0] == 303
    status = wait_idle(app)
    assert status["run"]["id"] > run_id and status["run"]["new_papers"] == 1
    assert paper(app, B)["status"] == "done"
    assert f"Paper {B}" in app.get("/ui")[1]

    # Cycles are recorded in the database, one row each.
    if not shared.home.database_url:
        assert shared.home.sql("SELECT id, new_papers, processed, failed, finished_at IS NOT NULL FROM runs "
                               "ORDER BY id")[-2:] == [(run_id, 1, 1, 0, 1), (status["run"]["id"], 1, 1, 0, 1)]


def test_scheduler_is_on_by_default_and_its_cycle_is_a_cycle_like_any_other(home, standin, server):
    world = standin(scenario={"feeds": {"cs.AI": feed((A, 1, "new"))}})
    world.control(slow=3)
    home.join(world).seeds("cs.AI")
    # No SCHEDULER_ENABLED at all: the scheduler runs a cycle at startup, nobody has to ask.
    app = server(home, SCHEDULER_ENABLED=None)
    wait_until(lambda: paper(app, A).get("status") == "processing", what="the startup cycle")
    status = answer(app.get("/status"))
    assert status["running"] is True and status["run"]["id"] == 1

    # A manual trigger on top of the scheduled cycle joins it instead of starting another.
    joined = answer(app.post("/process"))
    assert (joined["run_id"], joined["started"]) == (1, False)
    status = wait_idle(app)
    assert status["run"]["id"] == 1 and (status["run"]["new_papers"], status["run"]["processed"]) == (1, 1)
    assert (paper(app, A)["status"], paper(app, A)["attempts"]) == ("done", 1)
    assert world.counts()[f"llm:{A}"] == 1

    # The next trigger starts a cycle of its own, which does what any cycle does.
    world.control()
    world.scenario(feeds={"cs.AI": feed((A, 1, "new"), (B, 1, "new"))})
    started = answer(app.post("/process"))
    assert (started["run_id"], started["started"]) == (2, True)
    status = wait_idle(app)
    assert status["run"]["id"] == 2 and (status["run"]["new_papers"], status["run"]["processed"]) == (1, 1)
    assert paper(app, B)["status"] == "done" and world.counts()[f"llm:{A}"] == 1


def test_adding_a_category_starts_a_cycle_in_the_background(shared):
    app, world = shared.app, shared.world
    world.scenario(feeds={"cs.AI": [], "cs.LG": feed((C, 1, "new")), "cs.CV": feed((D, 1, "new")),
                          "math.CO": feed((E, 1, "new"))})
    world.control(slow=3)

    # Through the API: answered while the analysis is still being waited for.
    created = app.post("/categories", json={"arxiv_code": "cs.LG", "name": "LG"})
    assert created[0] == 201 and json.loads(created[1])["name"] == "LG"
    assert answer(app.get("/status"))["running"] is True
    assert paper(app, C).get("status") != "done"

    # Through the page, while that cycle is still running: answered at once as well, and the new
    # feed is polled by a cycle that follows the running one.
    added = app.post("/ui/categories/add", form={"arxiv_code": " cs.CV "})
    assert added[0] == 303 and app.headers["location"] == "/ui/categories"
    assert {"cs.LG", "cs.CV"} <= {c["arxiv_code"] for c in answer(app.get("/categories"))}
    assert paper(app, C).get("status") != "done"

    wait_until(lambda: paper(app, D).get("status") == "done", timeout=120, what="the paper of the second feed")
    wait_idle(app)
    assert [(paper(app, i)["status"], paper(app, i)["attempts"]) for i in (C, D)] == [("done", 1)] * 2
    assert world.counts()[f"llm:{C}"] == 1 and world.counts()[f"llm:{D}"] == 1

    # Through the page with nothing running; adding it twice changes nothing.
    world.control()
    assert app.post("/ui/categories/add", form={"arxiv_code": "math.CO"})[0] == 303
    assert app.post("/ui/categories/add", form={"arxiv_code": "math.CO"})[0] == 303
    wait_until(lambda: paper(app, E).get("status") == "done", timeout=120, what="the paper of the third feed")
    wait_idle(app)
    codes = [c["arxiv_code"] for c in answer(app.get("/categories"))]
    assert {"cs.LG", "cs.CV", "math.CO"} <= set(codes) and codes.count("math.CO") == 1
    assert world.counts()[f"llm:{E}"] == 1
