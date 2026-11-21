"""Processing: attempts and retries, one worker per paper, leases, recovery after a kill or a restart."""
import json
import time

from conftest import feed, wait_until

A, B, C, D, E = "2401.00001", "2401.00002", "2401.00003", "2401.00004", "2401.00005"


def test_failed_papers_are_retried_three_times_then_wait_for_retry(env):
    feeds = {"cs.AI": feed((A, 1, "new"), (B, 1, "new"), (C, 1, "new"), (D, 1, "new"))}
    env.set_scenario(feeds=feeds, bad=[B], junk=[C], nopdf=[D])
    app = env.start_app()
    app.add_category("cs.AI")
    status = app.wait_idle()
    assert status["papers"] == {"pending": 0, "processing": 0, "done": 1, "failed": 3}
    assert (status["run"]["processed"], status["run"]["failed"]) == (1, 3)

    papers = app.by_arxiv_id()
    assert (papers[A]["status"], papers[A]["attempts"], papers[A]["error_message"]) == ("done", 1, None)
    for arxiv_id in (B, C, D):
        assert (papers[arxiv_id]["status"], papers[arxiv_id]["attempts"]) == ("failed", 1)
        assert papers[arxiv_id]["error_message"]
    # An analysis with a field of the wrong type (keywords as a string) fails the paper and
    # stores nothing.
    assert "keywords" in papers[C]["error_message"]
    assert app.get(f"/papers/{papers[C]['id']}/analysis").status_code == 404
    assert "404" in papers[D]["error_message"]
    assert "Processing Failed" in app.get(f"/ui/paper/{papers[C]['id']}").text

    # Later cycles try the failed papers again, each try counted, until they have 3 attempts.
    run = app.cycle()
    assert (run["processed"], run["failed"]) == (0, 3)
    assert [app.paper(i)["attempts"] for i in (A, B, C, D)] == [1, 2, 2, 2]
    run = app.cycle()
    assert (run["processed"], run["failed"]) == (0, 3)
    assert [app.paper(i)["attempts"] for i in (B, C, D)] == [3, 3, 3]

    # After that they stay failed and are left alone, even once the cause is gone.
    env.set_scenario(feeds=feeds)
    counts = env.counts()
    for _ in range(2):
        run = app.cycle()
        assert (run["processed"], run["failed"]) == (0, 0)
    assert env.counts() == {**counts, "rss:cs.AI": counts["rss:cs.AI"] + 2}
    assert [(app.paper(i)["status"], app.paper(i)["attempts"]) for i in (B, C, D)] == [("failed", 3)] * 3
    # one model request per try for an answer that arrived (the SDK may repeat a request that
    # got an HTTP 500), one download per try
    assert counts[f"llm:{B}"] >= 3 and counts[f"llm:{C}"] == 3 and counts[f"pdf:{D}"] == 3
    assert counts[f"llm:{A}"] == 1 and f"llm:{D}" not in counts

    # POST /papers/{id}/retry: 404 for an unknown id, 409 unless the paper is failed,
    # 202 and pending with 0 attempts for a failed one.
    assert app.post("/papers/9999/retry").status_code == 404
    assert app.post(f"/papers/{papers[A]['id']}/retry").status_code == 409
    for arxiv_id in (B, C):
        answer = app.post(f"/papers/{papers[arxiv_id]['id']}/retry")
        assert answer.status_code == 202
        assert (answer.json()["status"], answer.json()["attempts"], answer.json()["error_message"]) == ("pending", 0, None)
    assert app.post(f"/papers/{papers[B]['id']}/retry").status_code == 409, "pending now, not failed"
    assert app.status()["papers"] == {"pending": 2, "processing": 0, "done": 1, "failed": 1}

    run = app.cycle()
    assert (run["processed"], run["failed"]) == (2, 0)
    papers = app.by_arxiv_id()
    assert [(papers[i]["status"], papers[i]["attempts"], papers[i]["error_message"]) for i in (B, C)] == [("done", 1, None)] * 2
    assert (papers[D]["status"], papers[D]["attempts"]) == ("failed", 3)
    assert app.get(f"/papers/{papers[C]['id']}/analysis").json()["keywords"] == ["kw", C]


BLOB_STAGING = r"""
import asyncio, json
from app.db import migrate
from app.db.database import engine
from app.services import feed_poller, pdf_extractor, processor

calls = []

async def upload_to_blob(local_path, blob_name):
    calls.append(["upload", blob_name, local_path.exists()])
    return "blob-url"

async def delete_blob(blob_name):
    calls.append(["delete", blob_name])

pdf_extractor.upload_to_blob = upload_to_blob
pdf_extractor.delete_blob = delete_blob

async def main():
    async with engine.begin() as conn:
        await conn.run_sync(migrate.upgrade)
    await feed_poller.sync_categories_from_yaml()
    await feed_poller.poll_all()
    worked = await processor.process_pending_papers()
    await engine.dispose()
    print("PROBE" + json.dumps({"calls": calls, "processed": worked.processed, "failed": worked.failed}))

asyncio.run(main())
"""


def test_pdfs_go_through_blob_storage_only_when_it_is_configured(env):
    # The Azure calls themselves are replaced by recorders: what is checked is when they happen.
    env.set_yaml_categories(["cs.AI"])

    def run(**settings):
        out = env.run_python(BLOB_STAGING, **settings)
        return json.loads(out[out.index("PROBE") + len("PROBE"):])

    env.set_scenario(feeds={"cs.AI": feed((A, 1, "new"))})
    assert run() == {"calls": [], "processed": 1, "failed": 0}

    env.set_scenario(feeds={"cs.AI": feed((A, 1, "new"), (B, 1, "new"))})
    assert run(AZURE_STORAGE_ACCOUNT_NAME="someaccount") == {
        "calls": [["upload", "paper_2.pdf", True], ["delete", "paper_2.pdf"]], "processed": 1, "failed": 0}

    env.set_scenario(feeds={"cs.AI": feed((A, 1, "new"), (B, 1, "new"), (C, 1, "new"))}, bad=[C])
    assert run(AZURE_BLOB_CONNECTION_STRING="UseDevelopmentStorage=true") == {
        "calls": [["upload", "paper_3.pdf", True], ["delete", "paper_3.pdf"]], "processed": 0, "failed": 1}


LEASE_CLOCKS = r"""
import asyncio, json, time
from datetime import datetime, timedelta, timezone
from sqlalchemy import update
from app.db import crud, migrate
from app.db.database import engine, async_session
from app.db.models import Paper, PaperStatus

HOUR = 3600

async def main():
    async with engine.begin() as conn:
        await conn.run_sync(migrate.upgrade)
    cat = await crud.create_category("c", "cs.AI")
    now = datetime.now(timezone.utc)
    base = dict(title="t", authors=[], abstract="a", pdf_url="u", published_at=now, categories=[])
    rows = [dict(arxiv_id=f"2401.0000{n}", version=1, **base) for n in range(1, 7)]
    await crud.store_new_papers(rows, cat.id)

    here, elsewhere = crud._HOST, "another-machine/boot"
    fresh_wall, old_wall = now - timedelta(seconds=1), now - timedelta(seconds=HOUR)
    fresh_mono, old_mono = time.monotonic() - 1, time.monotonic() - HOUR
    stamps = {
        "2401.00001": dict(lease_host=here, lease_mono=fresh_mono, lease_at=old_wall),
        "2401.00002": dict(lease_host=here, lease_mono=old_mono, lease_at=fresh_wall),
        "2401.00003": dict(lease_host=elsewhere, lease_mono=old_mono, lease_at=fresh_wall),
        "2401.00004": dict(lease_host=elsewhere, lease_mono=fresh_mono, lease_at=old_wall),
        "2401.00005": dict(lease_host=None, lease_mono=None, lease_at=old_wall),
        "2401.00006": dict(lease_host=None, lease_mono=None, lease_at=None),
    }
    async with async_session() as s:
        for arxiv_id, stamp in stamps.items():
            await s.execute(update(Paper.__table__).where(Paper.__table__.c.arxiv_id == arxiv_id).values(
                status=PaperStatus.processing, attempts=1, lease_token="t", **stamp))
        await s.commit()
    due = await crud.get_claimable_paper_ids()
    papers = {p.id: p.arxiv_id for p in await crud.get_papers()}
    await engine.dispose()
    print("PROBE" + json.dumps({"host_known": here is not None, "due": sorted(papers[i] for i in due)}))

asyncio.run(main())
"""


def test_lease_is_judged_by_the_monotonic_clock_on_one_machine_and_by_utc_across_machines(env):
    # Six papers in work with a 10 minute lease, renewed a second ago or an hour ago on either
    # clock. A wall clock that jumps must not cost a live worker on this machine its paper (1)
    # nor keep a dead one's paper (2); a worker on another machine is judged by UTC time (3, 4).
    out = env.run_python(LEASE_CLOCKS, PROCESSING_LEASE_SECONDS=600)
    result = json.loads(out[out.index("PROBE") + len("PROBE"):])
    if result["host_known"]:
        assert result["due"] == ["2401.00002", "2401.00004", "2401.00005", "2401.00006"]
    else:  # no way to tell machines apart here: UTC time decides everywhere
        assert result["due"] == ["2401.00001", "2401.00004", "2401.00005", "2401.00006"]


OVERLAPPING_CYCLES = r"""
import asyncio, json
from app.db import migrate
from app.db.database import engine
from app.services import feed_poller, processor

async def one_cycle():
    await feed_poller.sync_categories_from_yaml()
    polled = await feed_poller.poll_all()
    worked = await processor.process_pending_papers()
    return [polled.stats.new_papers, worked.processed, worked.failed]

async def main():
    async with engine.begin() as conn:
        await conn.run_sync(migrate.upgrade)
    results = await asyncio.gather(*[one_cycle() for _ in range(4)])
    await engine.dispose()
    print("PROBE" + json.dumps(results))

asyncio.run(main())
"""


def test_overlapping_cycles_in_one_process_analyse_each_paper_once(env):
    # Four whole cycles at the same moment in one process, the way a refresh on top of a
    # running cycle used to overlap: every paper is stored once and analysed once.
    ids = [A, B, C, D, E]
    env.set_scenario(feeds={"cs.AI": feed(*[(i, 1, "new") for i in ids])}, slow=0.3)
    env.set_yaml_categories(["cs.AI"])
    out = env.run_python(OVERLAPPING_CYCLES)
    results = json.loads(out[out.index("PROBE") + len("PROBE"):])

    assert sum(r[0] for r in results) == 5, results  # stored once
    assert sum(r[1] for r in results) == 5 and sum(r[2] for r in results) == 0, results
    counts = env.counts()
    assert [counts[f"llm:{i}"] for i in ids] == [1] * 5
    rows = env.db(lambda c: c.exec_driver_sql(
        "SELECT p.arxiv_id, p.status, p.attempts, (SELECT count(*) FROM analyses a WHERE a.paper_id = p.id) "
        "FROM papers p ORDER BY p.arxiv_id").fetchall())
    assert [(r[0], str(r[1]), r[2], r[3]) for r in rows] == [(i, "done", 1, 1) for i in ids]
    assert env.db(lambda c: c.exec_driver_sql("SELECT count(*) FROM categories").scalar()) == 1


def test_two_processes_on_one_database_analyse_each_paper_once(env):
    ids = [A, B, C, D, E]
    env.set_scenario(feeds={"cs.AI": feed(*[(i, 1, "new") for i in ids])}, slow=0.6)
    env.set_yaml_categories(["cs.AI"])
    one = env.start_app(wait=False)
    two = env.start_app(wait=False)
    one.wait_ready()
    two.wait_ready()

    # Both poll the same feed and reach for the same papers at the same time.
    assert one.start_cycle()["started"] is True
    assert two.start_cycle()["started"] is True
    one.wait_idle()
    two.wait_idle()

    papers = one.by_arxiv_id()
    assert sorted(papers) == ids and len(one.papers()) == 5
    assert [(papers[i]["status"], papers[i]["attempts"]) for i in ids] == [("done", 1)] * 5
    counts = env.counts()
    assert [counts[f"llm:{i}"] for i in ids] == [1] * 5
    assert [counts[f"pdf:{i}"] for i in ids] == [1] * 5
    assert two.status()["papers"] == {"pending": 0, "processing": 0, "done": 5, "failed": 0}
    assert len(one.categories()) == 1

    # Each process recorded its own cycle; together they did the five papers, none failed.
    def runs(conn):
        return conn.exec_driver_sql("SELECT processed, failed, new_papers FROM runs ORDER BY id").fetchall()

    rows = env.db(runs)
    assert len(rows) == 2
    assert sum(r[0] for r in rows) == 5 and sum(r[1] for r in rows) == 0 and sum(r[2] for r in rows) == 5


def test_live_worker_keeps_its_paper_beyond_the_lease(env):
    # The analysis takes several times the lease. The worker renews it, so another process
    # running cycles all the while never takes the paper over.
    env.set_scenario(feeds={"cs.AI": feed((A, 1, "new"))}, slow=5)
    env.set_yaml_categories(["cs.AI"])
    one = env.start_app(wait=False, PROCESSING_LEASE_SECONDS=1)
    two = env.start_app(wait=False, PROCESSING_LEASE_SECONDS=1)
    one.wait_ready()
    two.wait_ready()

    one.start_cycle()
    wait_until(lambda: (one.paper(A) or {}).get("status") == "processing", what="the paper to be in work")
    other_cycles = 0
    while one.status()["running"]:
        run = two.cycle()
        assert (run["processed"], run["failed"]) == (0, 0)
        other_cycles += 1
        time.sleep(0.4)
    assert other_cycles >= 3

    paper = one.paper(A)
    assert (paper["status"], paper["attempts"]) == ("done", 1)
    assert env.counts()[f"llm:{A}"] == 1 and env.counts()[f"pdf:{A}"] == 1


def test_paper_of_a_killed_process_is_taken_over_after_the_lease(env):
    env.set_scenario(feeds={"cs.AI": feed((A, 1, "new"))}, slow=60)
    env.set_yaml_categories(["cs.AI"])
    lease = 6
    one = env.start_app(wait=False, PROCESSING_LEASE_SECONDS=lease)
    two = env.start_app(wait=False, PROCESSING_LEASE_SECONDS=lease)
    one.wait_ready()
    two.wait_ready()

    one.start_cycle()
    wait_until(lambda: (two.paper(A) or {}).get("status") == "processing", what="the paper to be in work")
    wait_until(lambda: env.counts().get(f"llm:{A}") == 1, what="the model call")
    one.kill()
    killed_at = time.monotonic()
    env.set_scenario(feeds={"cs.AI": feed((A, 1, "new"))})

    # While the lease may still be alive nobody touches the paper ...
    run = two.cycle()
    assert time.monotonic() - killed_at < lease - 2, "machine too slow for this check"
    assert (run["processed"], run["failed"]) == (0, 0)
    paper = two.paper(A)
    assert (paper["status"], paper["attempts"]) == ("processing", 1)

    # ... and once it was not renewed for the lease time, the next cycle of any process takes it.
    time.sleep(max(0.0, lease + 1 - (time.monotonic() - killed_at)))
    run = two.cycle()
    assert (run["processed"], run["failed"]) == (1, 0)
    paper = two.paper(A)
    assert (paper["status"], paper["attempts"], paper["error_message"]) == ("done", 2, None)
    assert env.counts()[f"llm:{A}"] == 2
    assert two.status()["papers"] == {"pending": 0, "processing": 0, "done": 1, "failed": 0}


def test_nothing_is_stuck_after_a_restart(env):
    env.set_scenario(feeds={"cs.AI": feed((A, 1, "new"), (B, 1, "new"))}, slow=60)
    env.set_yaml_categories(["cs.AI"])
    lease = 3
    app = env.start_app(PROCESSING_LEASE_SECONDS=lease)
    first = app.start_cycle()
    wait_until(lambda: app.status()["papers"]["processing"] == 1, what="a paper to be in work")
    wait_until(lambda: sum(v for k, v in env.counts().items() if k.startswith("llm:")) == 1, what="the model call")

    # An ordinary shutdown (SIGTERM) in the middle of a paper.
    app.stop()
    stopped_at = time.monotonic()
    assert "Application shutdown complete" in app.log_path.read_text(), app.log_tail()
    env.set_scenario(feeds={"cs.AI": feed((A, 1, "new"), (B, 1, "new"))})

    # The paper is still marked as in work, and the interrupted cycle is closed in the record.
    app = env.start_app(PROCESSING_LEASE_SECONDS=lease)
    status = app.status()
    assert status["running"] is False
    assert status["papers"] == {"pending": 1, "processing": 1, "done": 0, "failed": 0}
    assert status["run"]["id"] == first["run_id"] and status["run"]["finished_at"] is not None

    # Once its lease has run out the next cycle takes it, together with the one never started.
    time.sleep(max(0.0, lease + 1 - (time.monotonic() - stopped_at)))
    run = app.cycle()
    assert run["id"] > first["run_id"] and (run["processed"], run["failed"]) == (2, 0)
    papers = app.by_arxiv_id()
    assert sorted((p["status"], p["attempts"]) for p in papers.values()) == [("done", 1), ("done", 2)]
    assert len(app.papers()) == 2


def test_abandoned_paper_is_taken_over_even_on_its_third_try(env):
    # A paper that failed twice and whose worker then died on the third try is still in work,
    # not failed: once the lease is out the next cycle takes it over like any other.
    env.set_scenario(feeds={"cs.AI": feed((A, 1, "new"))}, bad=[A])
    env.set_yaml_categories(["cs.AI"])
    lease = 3
    app = env.start_app(PROCESSING_LEASE_SECONDS=lease)
    app.cycle()
    app.cycle()
    assert (app.paper(A)["status"], app.paper(A)["attempts"]) == ("failed", 2)

    env.set_scenario(feeds={"cs.AI": feed((A, 1, "new"))}, slow=60)
    calls_before = env.counts()[f"llm:{A}"]
    app.start_cycle()
    wait_until(lambda: app.paper(A)["status"] == "processing", what="the third try")
    wait_until(lambda: env.counts()[f"llm:{A}"] > calls_before, what="the model call")
    app.kill()
    killed_at = time.monotonic()
    env.set_scenario(feeds={"cs.AI": feed((A, 1, "new"))})

    app = env.start_app(PROCESSING_LEASE_SECONDS=lease)
    assert (app.paper(A)["status"], app.paper(A)["attempts"]) == ("processing", 3)
    time.sleep(max(0.0, lease + 1 - (time.monotonic() - killed_at)))
    run = app.cycle()
    assert (run["processed"], run["failed"]) == (1, 0)
    paper = app.paper(A)
    assert (paper["status"], paper["attempts"], paper["error_message"]) == ("done", 4, None)
