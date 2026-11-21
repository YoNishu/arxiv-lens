"""The app runs end to end on a SQLite file with no Azure and no arXiv, and what worked keeps working."""
import json
import time

from conftest import feed

A, B = "2401.00001", "2401.00002"

TOOLS_PROBE = r"""
import asyncio, json
from fastmcp import Client
from app.db.database import engine
from app.mcp.server import mcp
from app.services import chat_agent

def text_of(result):
    parts = getattr(result, "content", None) or []
    return "\n".join(getattr(p, "text", "") for p in parts)

async def main():
    out = {"chat": {}, "mcp": {}}
    tool = chat_agent._exec_tool
    out["chat_tool_names"] = sorted(t["function"]["name"] for t in chat_agent.TOOLS)
    out["chat"]["digest"] = await tool("get_daily_digest", {})
    out["chat"]["search"] = await tool("search_papers", {"query": "2401.00001"})
    out["chat"]["search_none"] = await tool("search_papers", {"query": "no-such-thing"})
    out["chat"]["detail"] = await tool("get_paper_detail", {"paper_id": PAPER_ID})
    out["chat"]["compare"] = await tool("compare_papers", {"paper_ids": [PAPER_ID, 9999]})
    out["chat"]["categories"] = await tool("list_categories", {})
    out["chat"]["add"] = await tool("add_category", {"arxiv_code": "math.CO"})
    out["chat"]["add_again"] = await tool("add_category", {"arxiv_code": "math.CO"})
    async with Client(mcp) as client:
        out["mcp_tool_names"] = sorted(t.name for t in await client.list_tools())
        out["mcp"]["digest"] = text_of(await client.call_tool("get_daily_digest", {}))
        out["mcp"]["search"] = text_of(await client.call_tool("search_papers", {"query": "2401.00001"}))
        out["mcp"]["analysis"] = text_of(await client.call_tool("get_paper_analysis", {"paper_id": PAPER_ID}))
        out["mcp"]["categories"] = text_of(await client.call_tool("list_categories", {}))
        out["mcp"]["add"] = text_of(await client.call_tool("add_category", {"arxiv_code": "math.PR"}))
    cats = await chat_agent.crud.get_all_categories()
    removable = [c.id for c in cats if c.arxiv_code == "math.CO"][0]
    out["chat"]["remove"] = await tool("remove_category", {"category_id": removable})
    out["chat"]["categories_after"] = await tool("list_categories", {})
    await engine.dispose()
    print("PROBE" + json.dumps(out))

asyncio.run(main())
"""


MCP_OVER_HTTP = r"""
import asyncio, json, os
from fastmcp import Client

async def main():
    async with Client(os.environ["APP_URL"] + "/mcp/mcp") as client:
        names = sorted(t.name for t in await client.list_tools())
        found = await client.call_tool("search_papers", {"query": "2401.00001"})
    print("PROBE" + json.dumps({"tools": names, "search": found.content[0].text}))

asyncio.run(main())
"""


def test_runs_end_to_end_offline_and_existing_surface_keeps_working(env):
    env.set_yaml_categories(["cs.AI", "cs.LG"])
    env.set_scenario(feeds={"cs.AI": feed((A, 1, "new")), "cs.LG": feed((B, 2, "new"))})
    app = env.start_app()

    # SCHEDULER_ENABLED=false: the app is up, and nothing has polled or seeded anything.
    status = app.status()
    assert status == {
        "running": False,
        "papers": {"pending": 0, "processing": 0, "done": 0, "failed": 0},
        "run": None,
    }
    time.sleep(1.0)
    assert app.status()["run"] is None and env.counts() == {}

    # One cycle: the trigger answers at once, the work shows up in /status.
    answer = app.start_cycle()
    assert answer["started"] is True and isinstance(answer["run_id"], int)
    assert {"message", "new_papers_found", "papers_processed"} <= answer.keys()  # the old fields
    status = app.wait_idle()
    run = status["run"]
    assert run["id"] == answer["run_id"]
    assert set(run) == {"id", "started_at", "finished_at", "new_papers", "updated", "skipped", "processed", "failed"}
    assert (run["new_papers"], run["updated"], run["skipped"], run["processed"], run["failed"]) == (2, 0, 0, 2, 0)
    assert run["started_at"] <= run["finished_at"]
    assert status["papers"] == {"pending": 0, "processing": 0, "done": 2, "failed": 0}

    # Categories came from the seed file; papers carry the old fields and the new ones.
    cats = app.categories()
    assert set(cats) == {"cs.AI", "cs.LG"}
    assert set(cats["cs.AI"]) == {"id", "name", "arxiv_code", "description", "added_at"}
    papers = app.by_arxiv_id()
    assert set(papers) == {A, B}
    a = papers[A]
    old_fields = {"id", "category_id", "arxiv_id", "title", "authors", "abstract", "pdf_url",
                  "published_at", "categories", "status", "created_at"}
    assert old_fields | {"category_ids", "version", "attempts", "error_message"} == set(a)
    assert (a["version"], a["status"], a["attempts"], a["error_message"]) == (1, "done", 1, None)
    assert a["category_id"] == cats["cs.AI"]["id"] and a["category_ids"] == [cats["cs.AI"]["id"]]
    assert a["title"] == f"Paper {A} (v1)" and a["pdf_url"] == env.pdf_url(A, 1)
    assert a["authors"] == ["A. Author", "B. Author"] and a["categories"] == ["cs.AI"]
    assert a["published_at"].startswith("2024-01-01T10:00:00") and a["created_at"].endswith(("Z", "+00:00"))
    assert papers[B]["version"] == 2 and papers[B]["pdf_url"] == env.pdf_url(B, 2)

    # Each paper went to the PDF host and to the model exactly once; no blob storage was needed.
    counts = env.counts()
    assert {k: counts[k] for k in (f"pdf:{A}", f"llm:{A}", f"pdf:{B}", f"llm:{B}")} == {
        f"pdf:{A}": 1, f"llm:{A}": 1, f"pdf:{B}": 1, f"llm:{B}": 1}
    assert not list((env.tmp / "data").glob("*.pdf")), "downloaded PDFs are cleaned up"

    # The other existing routes.
    assert [p["arxiv_id"] for p in app.papers(status="done", category_id=cats["cs.LG"]["id"])] == [B]
    assert app.papers(status="pending") == []
    analysis = app.get(f"/papers/{a['id']}/analysis")
    assert analysis.status_code == 200
    body = analysis.json()
    assert set(body) == {"id", "paper_id", "summary", "key_contributions", "methodology",
                         "limitations", "keywords", "novelty_score", "created_at"}
    assert body["summary"] == f"Summary of {A}." and body["keywords"] == ["kw", A] and body["novelty_score"] == 2
    assert app.get("/papers/9999/analysis").status_code == 404
    digest = app.get("/daily-digest").json()
    assert {d["paper"]["arxiv_id"] for d in digest} == {A, B}
    assert set(digest[0]) == {"paper", "category_name", "analysis"}
    assert app.get("/last-updated").json()["last_updated"] is not None

    # The UI pages.
    for path, needle in [("/ui", f"Paper {A}"), ("/ui/papers", f"Paper {B}"), ("/ui/categories", "cs.LG"),
                         (f"/ui/paper/{a['id']}", f"Summary of {A}."), ("/ui/chat", "<html"),
                         (f"/ui/papers?category={cats['cs.LG']['id']}", f"Paper {B}"), ("/docs", "<html")]:
        page = app.get(path)
        assert page.status_code == 200 and needle in page.text, path
    assert f"Paper {A}" not in app.get(f"/ui/papers?category={cats['cs.LG']['id']}").text
    assert app.get("/ui/paper/9999").status_code == 404
    assert app.get("/static/style.css").status_code == 200

    # Conversations (the chat's storage) work on SQLite too.
    conv = app.post("/conversations", json={"title": "t", "research_interest": "ri"})
    assert conv.status_code == 201
    conv_id = conv.json()["id"]
    assert app.get(f"/conversations/{conv_id}/messages").json() == []
    assert app.http.put(f"/conversations/{conv_id}/research-interest", json={"research_interest": "x"}).json() == {"ok": True}
    assert [c["research_interest"] for c in app.get("/conversations").json()] == ["x"]
    assert app.delete(f"/conversations/{conv_id}").status_code == 204

    # The chat tools and the MCP tools, called the way the chat agent and an MCP client call them.
    out = env.run_python(TOOLS_PROBE.replace("PAPER_ID", str(a["id"])))
    probe = json.loads(out[out.index("PROBE") + len("PROBE"):])
    assert probe["chat_tool_names"] == sorted(["get_daily_digest", "search_papers", "get_paper_detail",
                                               "compare_papers", "list_categories", "add_category", "remove_category"])
    assert probe["mcp_tool_names"] == sorted(["get_daily_digest", "search_papers", "get_paper_analysis",
                                              "add_category", "list_categories"])
    chat, mcp = probe["chat"], probe["mcp"]
    assert f"Paper {A}" in chat["digest"] and f"Paper {B}" in chat["digest"]
    assert f"[ID:{a['id']}]" in chat["search"] and "Score: 2/10" in chat["search"]
    assert chat["search_none"] == "No papers found matching 'no-such-thing'."
    assert f"**Summary**: Summary of {A}." in chat["detail"] and "Categories: cs.AI" in chat["detail"]
    assert f"--- Paper ID:{a['id']} ---" in chat["compare"]
    assert "(cs.AI)" in chat["categories"] and "(cs.LG)" in chat["categories"]
    assert chat["add"] == "Subscribed to math.CO." and chat["add_again"] == "Already subscribed to math.CO."
    assert chat["remove"] == "Category removed." and "math.CO" not in chat["categories_after"]
    assert f"**Paper {A} (v1)** (Score: 2/10)" in mcp["digest"]
    assert f"[ID:{a['id']}]" in mcp["search"]
    assert f"**Summary**: Summary of {A}." in mcp["analysis"]
    assert "- Category cs.AI (cs.AI)" in mcp["categories"]
    assert mcp["add"] == "Subscribed to math.PR. Papers will appear after the next poll."
    assert "math.PR" in app.categories()

    # The MCP server as mounted in the app, over HTTP.
    out = env.run_python(MCP_OVER_HTTP, APP_URL=f"http://127.0.0.1:{app.port}")
    served = json.loads(out[out.index("PROBE") + len("PROBE"):])
    assert served["tools"] == probe["mcp_tool_names"]
    assert f"[ID:{a['id']}]" in served["search"] and f"Summary of {A}." in served["search"]

    # The chat route itself: it streams, and the conversation is stored. (The stand-in is no chat
    # model, so what the assistant says is not checked.)
    chat = app.post("/chat", json={"message": "what is new?", "research_interest": "graphs"})
    assert chat.status_code == 200 and chat.headers["content-type"].startswith("text/event-stream")
    events = [line[len("data: "):] for line in chat.text.splitlines() if line.startswith("data: ")]
    assert json.loads(events[0])["type"] == "meta" and events[-1] == "[DONE]"
    conv_id = json.loads(events[0])["conversation_id"]
    messages = app.get(f"/conversations/{conv_id}/messages").json()
    assert (messages[0]["role"], messages[0]["content"]) == ("user", "what is new?")
    stored = [c for c in app.get("/conversations").json() if c["id"] == conv_id][0]
    assert (stored["title"], stored["research_interest"]) == ("what is new?", "graphs")


def test_removed_seed_category_is_not_created_again(env):
    env.set_yaml_categories(["cs.AI", "cs.LG"])
    env.set_scenario(feeds={"cs.AI": feed((A, 1, "new")), "cs.LG": feed((B, 1, "new"))})
    app = env.start_app()
    app.cycle()
    cats = app.categories()
    assert set(cats) == {"cs.AI", "cs.LG"}

    assert app.delete(f"/categories/{cats['cs.LG']['id']}").status_code == 204
    assert set(app.categories()) == {"cs.AI"}
    for _ in range(2):
        app.cycle()
        assert set(app.categories()) == {"cs.AI"}
    assert set(app.by_arxiv_id()) == {A}

    # Not after a restart either, and a code added to the file later is still picked up.
    app.stop()
    env.set_yaml_categories(["cs.AI", "cs.LG", "cs.CV"])
    app = env.start_app()
    app.cycle()
    assert set(app.categories()) == {"cs.AI", "cs.CV"}

    # Subscribing to it again by hand works as before.
    assert app.add_category("cs.LG")["arxiv_code"] == "cs.LG"
    assert app.post("/categories", json={"arxiv_code": "cs.LG"}).status_code == 409
    app.wait_idle()
    assert set(app.by_arxiv_id()) == {A, B}
