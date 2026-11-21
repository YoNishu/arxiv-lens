"""Processing: attempts and retries, one worker per paper, leases, recovery after a kill or a restart."""
import json
import signal

from conftest import feed, totals, wait_until

A, B, C, D, E = "2401.90001", "2401.90002", "2401.90003", "2401.90004", "2401.90005"
T0 = "2026-01-05T08:00:00Z"
KILLED = 137

# Where the cycles of these cases end with commit:N, the N is found by trying copies of the home;
# the search starts at the place last found, so it usually needs a try or two.
_last_found: dict[str, int] = {}


def run_of(result) -> dict[str, int]:
    return totals(result.lines[-1])


def fields(result) -> dict[str, str]:
    """`paper show` as label -> value, for the lines before the timeline."""
    lines = result.lines[:result.lines.index("timeline")]
    return {line[:12].strip(): line[12:] for line in lines}


def timeline(result) -> list[str]:
    return [" ".join(line.split()[1:]) for line in result.lines[result.lines.index("timeline") + 1:]]


def in_order(entries: list[str], expected: list[str]) -> bool:
    """True when the expected entries occur among the entries in this order."""
    position = 0
    for entry in entries:
        if position < len(expected) and entry == expected[position]:
            position += 1
    return position == len(expected)


def paper_row(home, arxiv_id: str) -> tuple:
    rows = home.sql("SELECT status, attempts, error_message, version FROM papers WHERE arxiv_id = ?", (arxiv_id,))
    return rows[0] if rows else (None, 0, None, None)


def has_text(home, arxiv_id: str) -> bool:
    return bool(home.sql("SELECT 1 FROM paper_texts t JOIN papers p ON p.id = t.paper_id WHERE p.arxiv_id = ?",
                         (arxiv_id,)))


def ended_cycle(home, folder, arxiv_id: str, tries_before: int, text_stored: bool, name: str, hint: int):
    """A copy of home in which a cycle, ended by commit:N, left the paper claimed and in work.

    With text_stored false the cycle ended right after the claim, before the text was stored;
    with text_stored true it ended while the model was being asked, after the text was stored.
    """
    def verdict(n: int):
        copy = home.copy(folder / f"{name}-{n}")
        result = copy.run("cycle", env={"ARXIVLENS_KILL_AT": f"commit:{n}"}, check=False)
        if result.status != KILLED:
            return 1, copy
        status, attempts, _error, _version = paper_row(copy, arxiv_id)
        if attempts <= tries_before:
            return -1, copy
        if status != "processing":
            return 1, copy
        if has_text(copy, arxiv_id) != text_stored:
            return (1 if not text_stored else -1), copy
        return 0, copy

    n = _last_found.get(name, hint)
    direction, copy = verdict(n)
    while direction != 0:
        n -= direction
        assert 1 <= n <= 200, f"no commit:N left {arxiv_id} in work"
        step, copy = verdict(n)
        if step == 0:
            break
        assert step == direction, f"commit:{n} and commit:{n + direction} disagree for {arxiv_id}"
    _last_found[name] = n
    return copy


def test_failed_papers_are_retried_three_times_then_wait_for_retry(home, standin):
    world = standin(scenario={"feeds": {"cs.AI": feed((A, 1, "new"), (B, 1, "new"), (C, 1, "new"), (D, 1, "new"))},
                              "bad": [B], "junk": [C], "nopdf": [D]})
    home.join(world)
    added, first = home.batch(["category", "add", "cs.AI"], ["cycle"])
    run = run_of(first)
    assert (run["new"], run["processed"], run["failed"]) == (4, 1, 3), first
    assert paper_row(home, A) == ("done", 1, None, 1)
    for arxiv_id in (B, C, D):
        assert paper_row(home, arxiv_id)[:2] == ("failed", 1), arxiv_id
    # The outside world's failures are stored in the application's own words.
    assert paper_row(home, B)[2] == "Model request failed with status 500"
    assert "keywords" in paper_row(home, C)[2]
    assert paper_row(home, D)[2] == "PDF request failed with status 404"
    # An analysis of the wrong shape stores nothing; the text taken before the model was asked stays.
    assert home.sql("SELECT count(*) FROM analyses a JOIN papers p ON p.id = a.paper_id WHERE p.arxiv_id = ?",
                    (C,)) == [(0,)]
    assert has_text(home, B) and has_text(home, C) and not has_text(home, D)

    # Later cycles try the failed papers again, each try counted, until they have 3 attempts.
    second, third = home.batch(["cycle"], ["cycle"])
    assert [(run_of(r)["processed"], run_of(r)["failed"]) for r in (second, third)] == [(0, 3), (0, 3)]
    assert [paper_row(home, i)[1] for i in (A, B, C, D)] == [1, 3, 3, 3]

    # After that they stay failed and are left alone, even once the cause is gone.
    world.scenario(feeds={"cs.AI": feed((A, 1, "new"), (B, 1, "new"), (C, 1, "new"), (D, 1, "new"))})
    counts = world.counts()
    later = home.batch(["cycle"], ["cycle"])
    assert [(run_of(r)["processed"], run_of(r)["failed"]) for r in later] == [(0, 0), (0, 0)]
    assert world.counts() == {**counts, "rss:cs.AI": counts["rss:cs.AI"] + 2}
    assert [paper_row(home, i)[:2] for i in (B, C, D)] == [("failed", 3)] * 3
    # One download per paper whose text was kept, and at least one model request per try; a
    # paper without a PDF never reached the model.
    assert counts[f"pdf:{B}"] == 1 and counts[f"llm:{B}"] >= 3
    assert counts[f"pdf:{C}"] == 1 and counts[f"llm:{C}"] == 3
    assert counts[f"pdf:{D}"] == 3 and f"llm:{D}" not in counts
    assert counts[f"llm:{A}"] == 1

    # By hand: only a failed paper can be retried; it starts again with no attempts.
    done, unknown, retried_b, retried_c, again, shown = home.batch(
        ["paper", "retry", A], ["paper", "retry", "2401.99999"], ["paper", "retry", B], ["paper", "retry", C],
        ["paper", "retry", B], ["paper", "show", B])
    assert (done.status, done.stderr) == (1, f"error: {A} is done: only a failed paper can be retried\n"), done
    assert (unknown.status, unknown.stderr) == (1, "error: no such paper: 2401.99999\n"), unknown
    assert (retried_b.lines, retried_c.lines) == ([f"queued {B}"], [f"queued {C}"])
    assert (again.status, again.stderr) == (1, f"error: {B} is pending: only a failed paper can be retried\n"), again
    assert (fields(shown)["status"], fields(shown)["attempts"], fields(shown)["error"]) == ("pending", "0", "-")

    after, shown = home.batch(["cycle"], ["paper", "show", B])
    assert (run_of(after)["processed"], run_of(after)["failed"]) == (2, 0), after
    assert [paper_row(home, i)[:3] for i in (B, C)] == [("done", 1, None)] * 2
    assert paper_row(home, D)[:2] == ("failed", 3)
    assert json.loads(home.sql("SELECT a.keywords FROM analyses a JOIN papers p ON p.id = a.paper_id "
                               "WHERE p.arxiv_id = ?", (C,))[0][0]) == ["kw", C]
    assert in_order(timeline(shown), ["announced v1 cs.AI", "failed v1", "failed v1", "failed v1", "retried v1",
                                      "analysed v1"]), shown.lines
    assert world.counts()[f"pdf:{B}"] == 1


def test_pdfs_go_through_blob_storage_only_when_it_is_configured(home, standin):
    # The Azure calls are replaced by recorders: what is checked is when they happen.
    world = standin(scenario={"feeds": {"cs.AI": feed((A, 1, "new"))}})
    home.join(world)
    home.run("category", "add", "cs.AI")

    def staged(arxiv_id, **settings):
        return json.loads(home.probe("blob_staging", arxiv_id, env=settings).lines[-1])

    assert staged(A) == {"calls": [], "outcome": "done"}

    world.scenario(feeds={"cs.AI": feed((A, 1, "new"), (B, 1, "new"))})
    outcome = staged(B, AZURE_STORAGE_ACCOUNT_NAME="someaccount")
    b_id = home.sql("SELECT id FROM papers WHERE arxiv_id = ?", (B,))[0][0]
    assert outcome == {"calls": [["upload", f"paper_{b_id}.pdf", True], ["delete", f"paper_{b_id}.pdf"]],
                       "outcome": "done"}

    world.scenario(feeds={"cs.AI": feed((A, 1, "new"), (B, 1, "new"), (C, 1, "new"))}, bad=[C])
    outcome = staged(C, AZURE_BLOB_CONNECTION_STRING="UseDevelopmentStorage=true")
    c_id = home.sql("SELECT id FROM papers WHERE arxiv_id = ?", (C,))[0][0]
    assert outcome == {"calls": [["upload", f"paper_{c_id}.pdf", True], ["delete", f"paper_{c_id}.pdf"]],
                       "outcome": "failed"}


def test_a_lease_runs_out_by_the_clock(home, standin, tmp_path):
    world = standin(scenario={"feeds": {"cs.AI": feed((A, 1, "new"))}})
    home.join(world)
    home.batch(["clock", "set", T0], ["category", "add", "cs.AI"])
    left = ended_cycle(home, tmp_path, A, tries_before=0, text_stored=False, name="claimed", hint=7)
    counts = world.counts()

    # Another cycle at the same instant leaves the claimed paper alone.
    same = run_of(left.run("cycle"))
    assert (same["processed"], same["failed"]) == (0, 0), same
    assert paper_row(left, A)[:2] == ("processing", 1)
    assert [world.counts().get(f"{kind}:{A}", 0) for kind in ("pdf", "llm")] == [
        counts.get(f"{kind}:{A}", 0) for kind in ("pdf", "llm")]

    # Once the lease has run out by the clock, the next cycle takes it over.
    advanced, later = left.batch(["clock", "advance", "601s"], ["cycle"])
    assert advanced.lines == ["2026-01-05T08:10:01Z"], advanced
    assert (run_of(later)["processed"], run_of(later)["failed"]) == (1, 0), later
    assert paper_row(left, A) == ("done", 2, None, 1)
    assert world.counts()[f"llm:{A}"] == counts.get(f"llm:{A}", 0) + 1


def test_overlapping_cycles_in_one_process_analyse_each_paper_once(home, standin):
    # Four whole cycles at the same moment in one process: every paper is stored once and
    # analysed once.
    ids = [A, B, C, D, E]
    world = standin(scenario={"feeds": {"cs.AI": feed(*[(i, 1, "new") for i in ids])}})
    world.control(slow=0.3)
    home.join(world)
    home.run("category", "add", "cs.AI")
    runs = json.loads(home.probe("overlapping_cycles").lines[-1])
    assert len(runs) == 4
    assert sum(r[0] for r in runs) == 5 and sum(r[1] for r in runs) == 5 and sum(r[2] for r in runs) == 0, runs  # ints
    counts = world.counts()
    assert [counts[f"llm:{i}"] for i in ids] == [1] * 5
    assert home.sql("SELECT p.arxiv_id, p.status, p.attempts, (SELECT count(*) FROM analyses a WHERE a.paper_id = p.id) "
                    "FROM papers p ORDER BY p.arxiv_id") == [(i, "done", 1, 1) for i in ids]
    assert home.sql("SELECT count(*) FROM categories") == [(1,)]


def test_two_processes_on_one_database_analyse_each_paper_once(home, standin):
    ids = [A, B, C, D, E]
    world = standin(scenario={"feeds": {"cs.AI": feed(*[(i, 1, "new") for i in ids])}})
    world.control(slow=0.6)
    home.join(world)
    home.run("category", "add", "cs.AI")

    # Both poll the same feed and reach for the same papers at the same time.
    one, two = home.start("cycle"), home.start("cycle")
    outputs = [proc.communicate(timeout=300) for proc in (one, two)]
    assert [one.returncode, two.returncode] == [0, 0], outputs
    runs = [totals(out.splitlines()[-1]) for out, _err in outputs]
    assert sum(r["new"] for r in runs) == 5 and sum(r["processed"] for r in runs) == 5, runs  # ints
    assert sum(r["failed"] for r in runs) == 0, runs  # ints

    counts = world.counts()
    assert [counts[f"llm:{i}"] for i in ids] == [1] * 5
    assert [counts[f"pdf:{i}"] for i in ids] == [1] * 5
    assert home.sql("SELECT arxiv_id, status, attempts FROM papers ORDER BY arxiv_id") == [(i, "done", 1) for i in ids]
    assert home.sql("SELECT count(*) FROM runs") == [(2,)] and home.sql("SELECT count(*) FROM categories") == [(1,)]


def test_live_worker_keeps_its_paper_beyond_the_lease(home, standin):
    # On system time: the analysis takes several times the one-second lease. The worker renews
    # it, so another process running cycles all the while never takes the paper over.
    world = standin(scenario={"feeds": {"cs.AI": feed((A, 1, "new"))}})
    world.control(slow=8)
    home.join(world)
    home.run("category", "add", "cs.AI")
    lease = {"PROCESSING_LEASE_SECONDS": 1}

    one = home.start("cycle", env=lease)
    wait_until(lambda: world.counts().get(f"llm:{A}") == 1, timeout=120, what="the model to be asked")
    other_cycles = 0
    while one.poll() is None:
        run = run_of(home.run("cycle", env=lease))
        assert (run["processed"], run["failed"]) == (0, 0), run
        other_cycles += 1
    assert other_cycles >= 2

    out, err = one.communicate(timeout=60)
    assert one.returncode == 0, err
    assert run_of_text(out)["processed"] == 1
    assert paper_row(home, A)[:2] == ("done", 1)
    assert world.counts()[f"llm:{A}"] == 1 and world.counts()[f"pdf:{A}"] == 1


def run_of_text(text: str) -> dict[str, int]:
    return totals(text.splitlines()[-1])


def test_paper_of_a_killed_process_is_taken_over_after_the_lease(home, standin, tmp_path):
    world = standin(scenario={"feeds": {"cs.AI": feed((A, 1, "new"))}})
    home.join(world)
    home.batch(["clock", "set", T0], ["category", "add", "cs.AI"])
    # Ended while the model was being asked: the paper is in work and its text is stored.
    left = ended_cycle(home, tmp_path, A, tries_before=0, text_stored=True, name="asking",
                       hint=_last_found.get("claimed", 7) + 1)
    counts = world.counts()

    # While the lease may still be alive nobody touches the paper ...
    advanced, run, shown = left.batch(["clock", "advance", "600s"], ["cycle"], ["paper", "show", A])
    assert (run_of(run)["processed"], run_of(run)["failed"]) == (0, 0), run
    assert (fields(shown)["status"], fields(shown)["attempts"]) == ("processing", "1")

    # ... and once it was not renewed for longer than the lease, the next cycle of any process
    # takes it, with the text the killed worker stored.
    advanced, run = left.batch(["clock", "advance", "1s"], ["cycle"])
    assert (run_of(run)["processed"], run_of(run)["failed"]) == (1, 0), run
    assert paper_row(left, A) == ("done", 2, None, 1)
    assert world.counts()[f"llm:{A}"] == counts[f"llm:{A}"] + 1
    assert world.counts()[f"pdf:{A}"] == counts[f"pdf:{A}"]


def test_nothing_is_stuck_after_a_restart(home, standin, server):
    world = standin(scenario={"feeds": {"cs.AI": feed((A, 1, "new"), (B, 1, "new"))}})
    world.control(slow=60)
    home.join(world).seeds("cs.AI")
    home.run("clock", "set", T0)
    app = server(home)
    status, body = app.post("/process")
    assert status == 202, body
    first = json.loads(body)["run_id"]
    wait_until(lambda: world.count("llm:") == 1, timeout=120, what="the model to be asked")

    # An ordinary shutdown (SIGTERM) in the middle of a paper.
    app.stop()
    # It ended by itself (uvicorn passes the signal on once it has shut down), not by SIGKILL.
    assert app.proc.returncode in (0, -signal.SIGTERM), app.log()
    world.control()

    # The paper is still marked as in work, and the interrupted cycle is closed in the record.
    app = server(home)
    status = json.loads(app.get("/status")[1])
    assert status["running"] is False
    assert status["papers"] == {"pending": 1, "processing": 1, "done": 0, "failed": 0}
    assert status["run"]["id"] == first and status["run"]["finished_at"] is not None
    assert status["run"]["error_message"] == "Cycle was stopped before it finished"

    # Once its lease has run out by the clock the next cycle takes it, with the one never started.
    home.run("clock", "advance", "601s")
    assert app.post("/process")[0] == 202
    status = wait_until(lambda: (lambda s: s if not s["running"] else None)(json.loads(app.get("/status")[1])),
                        timeout=120, what="the cycle to end")
    run = status["run"]
    assert run["id"] > first and (run["processed"], run["failed"]) == (2, 0), run
    papers = json.loads(app.get("/papers")[1])
    assert sorted((p["status"], p["attempts"]) for p in papers) == [("done", 1), ("done", 2)]


def test_abandoned_paper_is_taken_over_even_on_its_third_try(home, standin, tmp_path):
    # A paper that failed twice and whose worker then died on the third try is in work, not
    # failed: once the lease is out the next cycle takes it over like any other.
    world = standin(scenario={"feeds": {"cs.AI": feed((A, 1, "new"))}, "bad": [A]})
    home.join(world)
    results = home.batch(["clock", "set", T0], ["category", "add", "cs.AI"], ["cycle"], ["cycle"])
    assert [r.status for r in results] == [0] * 4, results
    assert paper_row(home, A)[:2] == ("failed", 2)

    world.scenario(feeds={"cs.AI": feed((A, 1, "new"))})
    left = ended_cycle(home, tmp_path, A, tries_before=2, text_stored=True, name="third",
                       hint=_last_found.get("claimed", 7))
    assert paper_row(left, A)[:2] == ("processing", 3)
    results = left.batch(["clock", "advance", "601s"], ["cycle"])
    assert (run_of(results[1])["processed"], run_of(results[1])["failed"]) == (1, 0), results
    assert paper_row(left, A) == ("done", 4, None, 1)


def test_a_failing_feed_keeps_its_counts_and_the_other_feeds_are_polled(home, standin):
    many = [f"2402.{n}" for n in range(90001, 90151)]
    world = standin(scenario={"feeds": {"cs.LG": feed((A, 1, "new")), "cs.AI": feed(*[(i, 1, "new") for i in many]),
                                        "stat.ML": feed((A, 1, "cross"))},
                              "nopdf": many})
    home.join(world)
    home.batch(["category", "add", "cs.LG"], ["category", "add", "cs.AI"], ["category", "add", "stat.ML"])
    # The API answers its first two requests (cs.LG, then the first hundred of cs.AI) and fails from then on.
    world.control(api_fail_from=3)
    result = home.run("cycle")
    run = run_of(result)
    assert (run["new"], run["skipped"], run["processed"], run["failed"]) == (101, 0, 1, 100), result
    assert [line for line in result.stderr.splitlines() if line.startswith("feed ")][0].startswith("feed cs.AI: ")
    assert home.sql("SELECT feed, listed, new_papers, updated, skipped, error_message IS NOT NULL FROM run_feeds "
                    "WHERE run_id = ? ORDER BY feed", (run["run"],)) == [
        ("cs.AI", 150, 100, 0, 0, 1), ("cs.LG", 1, 1, 0, 0, 0), ("stat.ML", 1, 0, 0, 0, 0)]
    assert home.sql("SELECT new_papers, error_message FROM runs WHERE id = ?", (run["run"],)) == [(101, None)]
    # The feed polled after the failing one did its work: the paper it lists is linked to it.
    assert world.counts()["rss:stat.ML"] == 1
    assert [line.split("\t")[1:3] for line in home.run("category", "list").lines] == [
        ["cs.LG", "1"], ["cs.AI", "100"], ["stat.ML", "1"]]


def test_an_open_run_of_a_killed_cycle_is_closed_after_the_lease_and_not_before(home, standin):
    world = standin(scenario={"feeds": {"cs.AI": feed((A, 1, "new"))}})
    home.join(world)
    home.batch(["clock", "set", T0], ["category", "add", "cs.AI"])
    # Ended at its first file write, the text of the paper it had claimed.
    killed = home.run("cycle", env={"ARXIVLENS_KILL_AT": "fs:1"}, check=False)
    assert (killed.status, killed.stdout) == (KILLED, ""), killed
    assert home.sql("SELECT id, finished_at, error_message FROM runs") == [(1, None, None)]

    same, boundary = home.batch(["cycle"], ["clock", "advance", "600s"])
    after_boundary = home.run("cycle")
    assert home.sql("SELECT finished_at, error_message FROM runs WHERE id = 1") == [(None, None)]
    assert run_of(same)["run"] == 2 and run_of(after_boundary)["run"] == 3

    home.batch(["clock", "advance", "1s"], ["cycle"])
    finished, error = home.sql("SELECT finished_at, error_message FROM runs WHERE id = 1")[0]
    heartbeat = home.sql("SELECT heartbeat_at FROM runs WHERE id = 1")[0][0]
    assert error == "Cycle did not finish"
    assert finished == heartbeat and finished.replace("T", " ").startswith("2026-01-05 08:00:00"), finished
    assert home.sql("SELECT count(*) FROM runs WHERE finished_at IS NULL") == [(0,)]


def test_a_withdrawn_paper_is_never_claimed(home, standin):
    world = standin(scenario={"feeds": {"cs.AI": feed((A, 1, "new"), (B, 1, "new"))}, "withdrawn": [A]})
    home.join(world)
    results = home.batch(["category", "add", "cs.AI"], ["cycle"], ["cycle"], ["paper", "show", A])
    assert [r.status for r in results] == [0] * 4, results
    shown = fields(results[3])
    assert (shown["status"], shown["attempts"]) == ("pending", "0") and shown["withdrawn"] != "-"
    assert in_order(timeline(results[3]), ["announced v1 cs.AI", "withdrawn v1 cs.AI"]), results[3].lines
    assert paper_row(home, B)[:2] == ("done", 1)

    # A withdrawal that comes as a replacement: replaced, then withdrawn; pending and left alone.
    world.scenario(feeds={"cs.AI": feed((A, 1, "new"), (B, 2, "replace"))}, withdrawn=[A, B])
    results = home.batch(["cycle"], ["cycle"], ["paper", "show", B])
    assert [run_of(r)["updated"] for r in results[:2]] == [1, 0], results
    assert [(run_of(r)["processed"], run_of(r)["failed"]) for r in results[:2]] == [(0, 0), (0, 0)]
    shown = fields(results[2])
    assert (shown["version"], shown["status"], shown["attempts"]) == ("2", "pending", "0") and shown["withdrawn"] != "-"
    assert in_order(timeline(results[2]), ["announced v1 cs.AI", "analysed v1", "replaced v2 cs.AI",
                                           "withdrawn v2 cs.AI"]), results[2].lines
    counts = world.counts()
    assert f"pdf:{A}" not in counts and f"llm:{A}" not in counts
    assert (counts[f"pdf:{B}"], counts[f"llm:{B}"]) == (1, 1)
    assert home.sql("SELECT arxiv_id, attempts, withdrawn_at IS NOT NULL FROM papers ORDER BY arxiv_id") == [
        (A, 0, 1), (B, 0, 1)]

    # A later version that is not withdrawn clears the flag, and the paper is analysed again.
    world.scenario(feeds={"cs.AI": feed((A, 1, "new"), (B, 3, "replace"))}, withdrawn=[A])
    run = run_of(home.run("cycle"))
    assert (run["updated"], run["processed"]) == (1, 1), run
    assert home.sql("SELECT version, status, attempts, withdrawn_at FROM papers WHERE arxiv_id = ?", (B,)) == [
        (3, "done", 1, None)]
    assert paper_row(home, A)[:2] == ("pending", 0)


def test_an_analysis_made_for_an_older_version_is_refused(home, standin):
    world = standin(scenario={"feeds": {"cs.AI": feed((A, 1, "new"))}, "nopdf": [A]})
    home.join(world)
    home.batch(["category", "add", "cs.AI"], ["cycle"])
    assert paper_row(home, A)[:2] == ("failed", 1)

    # Claimed at version 1; version 2 arrives while the worker is on it; the worker's analysis of
    # version 1 is refused and nothing is stored.
    outcome = json.loads(home.probe("stale_analysis", A, "cs.AI").lines[-1])
    assert outcome == {"claimed": 1, "replaced": True, "completed": False}
    assert home.sql("SELECT count(*) FROM analyses") == [(0,)]
    assert paper_row(home, A) == ("pending", 0, None, 2)

    # The next try analyses the version the paper now has.
    world.scenario(feeds={"cs.AI": feed((A, 2, "replace"))})
    run = run_of(home.run("cycle"))
    assert (run["processed"], run["failed"]) == (1, 0), run
    assert home.sql("SELECT paper_version FROM analyses") == [(2,)]
    assert paper_row(home, A) == ("done", 1, None, 2)
