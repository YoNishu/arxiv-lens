"""The app runs end to end on SQLite with no Azure and no arXiv, and what worked keeps working."""
import json

from conftest import feed, totals, wait_until

A, B, C = "2401.90001", "2401.90002", "2401.90003"


def answer(reply: tuple[int, str]):
    status, body = reply
    assert status == 200, reply
    return json.loads(body)


def wait_idle(server) -> dict:
    return wait_until(lambda: (lambda s: s if not s["running"] else None)(answer(server.get("/status"))),
                      timeout=120, what="the cycle to end")


def by_arxiv_id(server, query: str = "") -> dict[str, dict]:
    return {p["arxiv_id"]: p for p in answer(server.get("/papers" + query))}


def test_runs_end_to_end_offline_and_existing_surface_keeps_working(home, standin, server):
    world = standin(scenario={"feeds": {"cs.AI": feed((A, 1, "new"), (C, 1, "new")), "cs.LG": feed((B, 2, "new"))},
                              "bad": [C]})
    home.join(world).seeds("cs.AI", "cs.LG")
    app = server(home)

    # The scheduler is off: the app is up and nothing has been polled or seeded by itself.
    assert answer(app.get("/status")) == {
        "running": False, "papers": {"pending": 0, "processing": 0, "done": 0, "failed": 0}, "run": None}
    assert world.counts() == {}

    # One cycle: the trigger answers at once, the work shows up in /status.
    status, body = app.post("/process")
    assert status == 202, body
    started = json.loads(body)
    assert started["started"] is True and isinstance(started["run_id"], int)
    assert {"message", "new_papers_found", "papers_processed"} <= started.keys()
    status = wait_idle(app)
    run = status["run"]
    assert run["id"] == started["run_id"]
    assert {"id", "started_at", "finished_at", "new_papers", "updated", "skipped", "processed", "failed",
            "error_message", "feeds"} <= set(run)
    assert (run["new_papers"], run["updated"], run["skipped"], run["processed"], run["failed"]) == (3, 0, 0, 2, 1)
    assert run["error_message"] is None and run["started_at"] <= run["finished_at"]
    assert [(f["feed"], f["listed"], f["new_papers"], f["error_message"]) for f in run["feeds"]] == [
        ("cs.AI", 2, 2, None), ("cs.LG", 1, 1, None)]
    assert status["papers"] == {"pending": 0, "processing": 0, "done": 2, "failed": 1}

    # Categories came from the seed file; papers carry the old fields and the new ones.
    cats = {c["arxiv_code"]: c for c in answer(app.get("/categories"))}
    assert set(cats) == {"cs.AI", "cs.LG"}
    assert {"id", "name", "arxiv_code", "description", "added_at"} <= set(cats["cs.AI"])
    ai, lg = cats["cs.AI"]["id"], cats["cs.LG"]["id"]
    papers = by_arxiv_id(app)
    assert set(papers) == {A, B, C}
    a = papers[A]
    assert {"id", "category_id", "arxiv_id", "title", "authors", "abstract", "pdf_url", "published_at", "categories",
            "status", "created_at", "category_ids", "version", "attempts", "error_message", "updated_at", "comment",
            "withdrawn_at"} <= set(a)
    assert (a["version"], a["status"], a["attempts"], a["error_message"], a["withdrawn_at"]) == (1, "done", 1, None, None)
    assert a["category_id"] == ai and a["category_ids"] == [ai]
    assert a["title"] == f"Paper {A} (v1)" and a["pdf_url"] == f"https://arxiv.org/pdf/{A}v1"
    assert a["authors"] == ["A. Author", "B. Author"] and a["categories"] == ["cs.AI"]
    assert a["published_at"].startswith("2024-01-01T10:00:00")
    assert papers[B]["version"] == 2 and papers[B]["pdf_url"] == f"https://arxiv.org/pdf/{B}v2"
    assert (papers[C]["status"], papers[C]["attempts"]) == ("failed", 1)
    assert papers[C]["error_message"] == "Model request failed with status 500"

    # Each paper went to the PDF host and to the model once.
    counts = world.counts()
    assert [counts[f"{kind}:{i}"] for i in (A, B) for kind in ("pdf", "llm")] == [1, 1, 1, 1]

    # The other existing routes.
    assert [p["arxiv_id"] for p in answer(app.get(f"/papers?status=done&category_id={lg}"))] == [B]
    assert answer(app.get("/papers?status=pending")) == []
    assert app.get("/papers?status=bogus")[0] == 422
    body = answer(app.get(f"/papers/{a['id']}/analysis"))
    assert {"id", "paper_id", "paper_version", "summary", "key_contributions", "methodology", "limitations",
            "keywords", "novelty_score", "created_at"} <= set(body)
    assert (body["summary"], body["keywords"], body["novelty_score"], body["paper_version"]) == (
        f"Summary of {A}.", ["kw", A], 2, 1)
    assert app.get("/papers/99999/analysis")[0] == 404
    digest = answer(app.get("/daily-digest"))
    assert {d["paper"]["arxiv_id"] for d in digest} == {A, B}
    assert {"paper", "category_name", "analysis"} <= set(digest[0])
    assert answer(app.get("/last-updated"))["last_updated"] is not None
    assert app.post("/categories", json={"arxiv_code": "not a code"})[0] == 422
    assert app.post("/ui/categories/add", form={"arxiv_code": "Not/Valid"})[0] == 303
    assert set(c["arxiv_code"] for c in answer(app.get("/categories"))) == {"cs.AI", "cs.LG"}

    # The pages.
    for path, needle in [("/ui", f"Paper {A}"), ("/ui/papers", f"Paper {B}"), ("/ui/categories", "cs.LG"),
                         (f"/ui/paper/{a['id']}", f"Summary of {A}."), (f"/ui/paper/{a['id']}", "announced"),
                         (f"/ui/paper/{a['id']}", f"Full text of paper {A}v1"), ("/ui/chat", "<html"),
                         (f"/ui/papers?category={lg}", f"Paper {B}"), ("/docs", "<html")]:
        status, page = app.get(path)
        assert status == 200 and needle in page, (path, needle)
    assert f"Paper {A}" not in app.get(f"/ui/papers?category={lg}")[1]
    assert app.get("/ui/paper/99999")[0] == 404
    assert app.get("/static/style.css")[0] == 200

    # Retrying: 404 for an unknown id, 409 unless the paper is failed, 202 and pending with no
    # attempts for a failed one; the page's button goes the same way and comes back to the page.
    c_id = papers[C]["id"]
    assert app.post("/papers/99999/retry")[0] == 404
    assert app.post(f"/papers/{a['id']}/retry")[0] == 409
    status, body = app.post(f"/papers/{c_id}/retry")
    assert status == 202, body
    assert [json.loads(body)[k] for k in ("status", "attempts", "error_message")] == ["pending", 0, None]
    assert app.post(f"/papers/{c_id}/retry")[0] == 409, "pending now, not failed"
    assert app.post(f"/ui/paper/{c_id}/retry")[0] == 303 and app.headers["location"] == f"/ui/paper/{c_id}"
    assert answer(app.get("/status"))["papers"] == {"pending": 1, "processing": 0, "done": 2, "failed": 0}

    # Conversations (the chat's storage) work on SQLite too.
    status, body = app.post("/conversations", json={"title": "t", "research_interest": "ri"})
    assert status == 201, body
    conv_id = json.loads(body)["id"]
    assert answer(app.get(f"/conversations/{conv_id}/messages")) == []
    assert answer(app.request("PUT", f"/conversations/{conv_id}/research-interest",
                              json_body={"research_interest": "x"})) == {"ok": True}
    assert [c["research_interest"] for c in answer(app.get("/conversations"))] == ["x"]
    assert app.request("DELETE", f"/conversations/{conv_id}")[0] == 204

    # The chat tools and the MCP tools, through the command line.
    query = json.dumps({"query": A})
    chat = home.batch(
        ["tool", "get_daily_digest"], ["tool", "search_papers", query],
        ["tool", "search_papers", json.dumps({"query": "no-such-thing"})],
        ["tool", "get_paper_detail", json.dumps({"paper_id": a["id"]})],
        ["tool", "compare_papers", json.dumps({"paper_ids": [a["id"], 99999]})],
        ["tool", "list_categories"], ["tool", "add_category", json.dumps({"arxiv_code": "math.CO"})],
        ["tool", "add_category", json.dumps({"arxiv_code": "math.CO"})],
    )
    assert [r.status for r in chat] == [0] * 8, chat
    digest, search, none, detail, compare, categories, add, add_again = [r.stdout for r in chat]
    assert f"Paper {A}" in digest and f"Paper {B}" in digest
    assert f"[ID:{a['id']}]" in search and "Score: 2/10" in search
    assert none == "No papers found matching 'no-such-thing'.\n"
    assert f"**Summary**: Summary of {A}." in detail and "Categories: cs.AI" in detail
    assert f"--- Paper ID:{a['id']} ---" in compare
    assert "(cs.AI)" in categories and "(cs.LG)" in categories
    assert (add, add_again) == ("Subscribed to math.CO.\n", "Already subscribed to math.CO.\n")
    math_co = [c["id"] for c in answer(app.get("/categories")) if c["arxiv_code"] == "math.CO"][0]
    rest = home.batch(
        ["tool", "remove_category", json.dumps({"category_id": math_co})], ["tool", "list_categories"],
        ["tool", "--mcp", "get_daily_digest"], ["tool", "--mcp", "search_papers", query],
        ["tool", "--mcp", "get_paper_analysis", json.dumps({"paper_id": a["id"]})],
        ["tool", "--mcp", "list_categories"], ["tool", "--mcp", "add_category", json.dumps({"arxiv_code": "math.PR"})],
        ["tool", "no_such_tool"], ["tool", "search_papers", "[1]"],
    )
    assert [r.status for r in rest] == [0] * 7 + [1, 2], rest
    removed, categories_after, m_digest, m_search, m_analysis, m_categories, m_add = [r.stdout for r in rest[:7]]
    assert removed == "Category removed.\n" and "math.CO" not in categories_after
    assert f"**Paper {A} (v1)** (Score: 2/10)" in m_digest
    assert f"[ID:{a['id']}]" in m_search
    assert f"**Summary**: Summary of {A}." in m_analysis
    assert "- Category cs.AI (cs.AI)" in m_categories
    assert m_add == "Subscribed to math.PR. Papers will appear after the next poll.\n"
    assert "math.PR" in {c["arxiv_code"] for c in answer(app.get("/categories"))}

    # The MCP server as mounted in the app, over HTTP.
    served = home.run("tool", "--mcp", "--url", f"{app.url}/mcp/mcp", "search_papers", query)
    assert f"[ID:{a['id']}]" in served.stdout and f"Summary of {A}." in served.stdout

    # The chat route itself: it streams, and the conversation is stored. (The stand-in is no chat
    # model, so what the assistant says is not checked.)
    status, stream = app.post("/chat", json={"message": "what is new?", "research_interest": "graphs"})
    assert status == 200 and app.headers["content-type"].startswith("text/event-stream")
    events = [line[len("data: "):] for line in stream.splitlines() if line.startswith("data: ")]
    assert json.loads(events[0])["type"] == "meta" and events[-1] == "[DONE]"
    conv_id = json.loads(events[0])["conversation_id"]
    messages = answer(app.get(f"/conversations/{conv_id}/messages"))
    assert (messages[0]["role"], messages[0]["content"]) == ("user", "what is new?")
    stored = [c for c in answer(app.get("/conversations")) if c["id"] == conv_id][0]
    assert (stored["title"], stored["research_interest"]) == ("what is new?", "graphs")

    # Removing a category over REST deletes the papers only it lists; an unknown id is 404, and
    # the page's delete button goes the same way.
    assert app.request("DELETE", f"/categories/{ai}")[0] == 204
    assert set(by_arxiv_id(app)) == {B}
    assert answer(app.get(f"/papers?category_id={ai}")) == []
    assert app.request("DELETE", f"/categories/{ai}")[0] == 404
    assert app.post(f"/ui/categories/{lg}/delete")[0] == 303
    assert answer(app.get("/papers")) == [] and answer(app.get("/daily-digest")) == []


def category_codes(result) -> list[str]:
    return [line.split("\t")[1] for line in result.lines]


def test_removed_seed_category_is_not_created_again(home, standin):
    world = standin(scenario={"feeds": {"cs.AI": feed((A, 1, "new")), "cs.LG": feed((B, 1, "new"))}})
    home.join(world).seeds("cs.AI", "cs.LG")
    first, listed = home.batch(["cycle"], ["category", "list"])
    assert totals(first.lines[-1])["new"] == 2, first
    assert category_codes(listed) == ["cs.AI", "cs.LG"]

    results = home.batch(["category", "remove", "cs.LG"], ["cycle"], ["cycle"], ["category", "list"])
    assert [r.status for r in results] == [0] * 4, results
    assert results[0].lines == ["removed cs.LG: 1 papers deleted, 0 kept"]
    assert [line.split("\t")[1:3] for line in results[3].lines] == [["cs.AI", "1"]]

    # Not in a later process either, and a code added to the file later is still picked up.
    home.seeds("cs.AI", "cs.LG", "cs.CV")
    home.run("cycle")
    assert category_codes(home.run("category", "list")) == ["cs.AI", "cs.CV"]

    # Subscribing to it again by hand works as before.
    added, again, cycle, listed = home.batch(["category", "add", "cs.LG"], ["category", "add", "cs.LG"], ["cycle"],
                                             ["category", "list"])
    assert added.lines == ["added cs.LG"]
    assert (again.status, again.stderr) == (1, "error: already subscribed: cs.LG\n"), again
    assert totals(cycle.lines[-1])["new"] == 1, cycle
    assert [line.split("\t")[1:3] for line in listed.lines] == [["cs.AI", "1"], ["cs.CV", "0"], ["cs.LG", "1"]]
    assert home.sql("SELECT p.arxiv_id FROM papers p JOIN paper_categories pc ON pc.paper_id = p.id "
                    "JOIN categories c ON c.id = pc.category_id WHERE c.arxiv_code = 'cs.LG'") == [(B,)]
