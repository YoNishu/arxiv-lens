"""The command line: what each base command prints, its exit statuses, and repeated calls of main."""
import re

from conftest import totals

TABLES = ["analyses", "categories", "conversations", "messages", "paper_categories", "paper_events",
          "paper_texts", "paper_versions", "papers", "run_feeds", "runs", "schema_version", "seeded_categories"]
ONE_FEED = "2401.90011"
WITHDRAWN = "2401.90012"
UNKNOWN = "2401.99999"
TIMELINE_LINE = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)  (\S+) *  v(\d+)(?:  (\S+))?$")


def _error(result, status: int, text: str) -> None:
    assert (result.status, result.stdout, result.stderr) == (status, "", f"error: {text}\n"), result


def test_db_and_category_commands_print_what_they_did(home):
    upgrade, again, info, *rest = home.batch(
        ["db", "upgrade"], ["db", "upgrade"], ["db", "info"],
        ["category", "add", "cs.LG"], ["category", "add", " stat.ML ", "--name", "Statistics"],
        ["category", "add", "cs.LG"], ["category", "add", "not a code"], ["category", "add", "CS.LG"],
        ["category", "add", "hep-th", "--name", "High energy physics"], ["category", "add", "astro-ph.CO"],
        ["category", "list"], ["category", "remove", "cs.LG"], ["category", "remove", "cs.LG"],
        ["category", "list"], ["db", "info"],
    )
    add_lg, add_ml, add_twice, add_bad, add_upper, add_hep, add_astro, listed, removed, remove_twice, relisted, info2 = rest

    assert upgrade.status == 0 and upgrade.lines, upgrade
    *steps, schema = upgrade.lines
    assert re.match(r"^schema \d+$", schema), upgrade
    numbers = [int(re.match(r"^step (\d+) [a-z_]+$", line).group(1)) for line in steps]
    assert numbers and numbers == sorted(numbers) and numbers[-1] == int(schema.split(" ")[1]), upgrade
    assert again.lines == [schema], again

    assert info.lines[0] == "schema=" + schema.split(" ")[1], info
    counted = dict(line.split("=") for line in info.lines[1:])
    assert list(counted) == sorted(counted) and set(TABLES) <= set(counted), info
    assert all(value.isdigit() for value in counted.values()) and counted["papers"] == counted["categories"] == "0"

    assert add_lg.lines == ["added cs.LG"] and add_ml.lines == ["added stat.ML"]
    _error(add_twice, 1, "already subscribed: cs.LG")
    _error(add_bad, 2, "not a category code: not a code")
    assert add_upper.status == 2 and add_upper.stdout == "", add_upper
    assert add_hep.lines == ["added hep-th"] and add_astro.lines == ["added astro-ph.CO"]

    rows = [line.split("\t") for line in listed.lines]
    assert [len(row) for row in rows] == [4, 4, 4, 4], listed
    assert [int(row[0]) for row in rows] == sorted(int(row[0]) for row in rows)
    assert [row[1] for row in rows] == ["cs.LG", "stat.ML", "hep-th", "astro-ph.CO"]
    assert [row[2] for row in rows] == ["0", "0", "0", "0"]
    assert rows[1][3] == "Statistics" and rows[2][3] == "High energy physics"

    assert removed.lines == ["removed cs.LG: 0 papers deleted, 0 kept"], removed
    _error(remove_twice, 1, "not subscribed: cs.LG")
    assert [line.split("\t")[1] for line in relisted.lines] == ["stat.ML", "hep-th", "astro-ph.CO"]
    assert info2.lines[0] == info.lines[0] and "categories=3" in info2.lines


def test_paper_show_prints_fields_then_the_timeline(home, standin):
    world = standin(scenario={"feeds": {"cs.LG": [[ONE_FEED, 1, "new"]]}})
    home.join(world).seeds("cs.LG")
    home.batch(["clock", "set", "2026-08-10T08:00Z"], ["cycle"])
    world.scenario(feeds={"cs.LG": [[ONE_FEED, 2, "replace"], [WITHDRAWN, 1, "new"]]}, withdrawn=[WITHDRAWN])
    _, cycle, shown, other, unknown, retry_done, retry_unknown = home.batch(
        ["clock", "advance", "1d"], ["cycle"], ["paper", "show", ONE_FEED], ["paper", "show", WITHDRAWN],
        ["paper", "show", UNKNOWN], ["paper", "retry", ONE_FEED], ["paper", "retry", UNKNOWN])
    assert cycle.status == 0, cycle
    chars = home.sql("SELECT t.chars FROM paper_texts t JOIN papers p ON p.id = t.paper_id WHERE p.arxiv_id = ?",
                     (ONE_FEED,))[0][0]

    assert shown.status == 0, shown
    fields = [
        ("arxiv_id", ONE_FEED), ("version", "2"), ("title", f"Paper {ONE_FEED} (v2)"),
        ("authors", "A. Author; B. Author"), ("categories", "cs.LG"), ("feeds", "cs.LG"),
        ("published", "2024-01-01T10:00:00Z"), ("updated", "2024-01-02T10:00:00Z"),
        ("stored", "2026-08-10T08:00:00Z"), ("status", "done"), ("attempts", "1"), ("withdrawn", "-"),
        ("error", "-"), ("analysis", "v2 novelty 2"), ("text", f"v2 {chars} chars pdf"),
    ]
    assert shown.lines[:16] == [f"{label:<12}{value}" for label, value in fields] + ["timeline"], shown
    timeline = [TIMELINE_LINE.match(line) for line in shown.lines[16:]]
    assert timeline and all(timeline), shown
    assert all(line == line.rstrip() and line.split("  ")[1].ljust(9) == line[22:31] for line in shown.lines[16:])
    entries = [(m.group(1), m.group(2), int(m.group(3)), m.group(4)) for m in timeline]
    wanted = iter([("2026-08-10T08:00:00Z", "announced", 1, "cs.LG"), ("2026-08-10T08:00:00Z", "analysed", 1, None),
                   ("2026-08-11T08:00:00Z", "replaced", 2, "cs.LG"), ("2026-08-11T08:00:00Z", "analysed", 2, None)])
    rest = iter(entries)
    assert all(any(entry == want for entry in rest) for want in wanted), entries

    assert other.status == 0, other
    labelled = dict((line[:12].rstrip(), line[12:]) for line in other.lines[:other.lines.index("timeline")])
    assert (labelled["withdrawn"], labelled["status"], labelled["attempts"]) == ("2026-08-11T08:00:00Z", "pending", "0")
    assert (labelled["analysis"], labelled["text"], labelled["updated"]) == ("-", "-", "2024-01-01T10:00:00Z")
    tail = other.lines[other.lines.index("timeline") + 1:]
    assert tail.index("2026-08-11T08:00:00Z  announced  v1  cs.LG") < tail.index(
        "2026-08-11T08:00:00Z  withdrawn  v1  cs.LG"), other

    _error(unknown, 1, f"no such paper: {UNKNOWN}")
    _error(retry_done, 1, f"{ONE_FEED} is done: only a failed paper can be retried")
    _error(retry_unknown, 1, f"no such paper: {UNKNOWN}")


def test_cycle_prints_its_counts_and_names_a_failing_feed(home, standin):
    first, second, third = "2401.90021", "2401.90022", "2401.90023"
    world = standin(scenario={"feeds": {"cs.LG": [[first, 1, "new"], [second, 1, "new"]], "stat.ML": [[third, 1, "new"]]},
                              "bad": [second]})
    world.control(feed_fail=["stat.ML"])
    home.join(world).seeds("cs.LG", "stat.ML")
    _, cycle, listed, failed, queued = home.batch(
        ["clock", "set", "2026-08-10T08:00Z"], ["cycle"], ["category", "list"], ["paper", "show", second],
        ["paper", "retry", second])

    assert cycle.status == 0 and len(cycle.lines) == 1, cycle
    assert {k: v for k, v in totals(cycle.lines[0]).items() if k != "run"} == {
        "new": 2, "updated": 0, "skipped": 0, "processed": 1, "failed": 1}, cycle
    assert re.match(r"^run \d+: new=2 updated=0 skipped=0 processed=1 failed=1$", cycle.lines[0])
    feed_lines = [line for line in cycle.stderr.splitlines() if line.startswith("feed ")]
    assert len(feed_lines) == 1 and re.match(r"^feed stat\.ML: \S", feed_lines[0]), cycle.stderr

    assert [line.split("\t")[1:3] for line in listed.lines] == [["cs.LG", "2"], ["stat.ML", "0"]], listed
    labelled = dict((line[:12].rstrip(), line[12:]) for line in failed.lines[:failed.lines.index("timeline")])
    assert (labelled["status"], labelled["attempts"]) == ("failed", "1"), failed
    assert labelled["error"] == "Model request failed with status 500", failed
    assert queued.lines == [f"queued {second}"], queued

    world.control()
    again = home.run("cycle")
    assert re.match(r"^run \d+: new=1 updated=0 skipped=0 processed=1 failed=1$", again.lines[0]), again
    assert totals(again.lines[0])["run"] == totals(cycle.lines[0])["run"] + 1
    assert not [line for line in again.stderr.splitlines() if line.startswith("feed ")], again.stderr


def test_tool_calls_chat_and_mcp_tools(home):
    home.batch(["clock", "set", "2026-08-10T08:00Z"], ["category", "add", "cs.LG", "--name", "Machine Learning"],
               ["category", "add", "stat.ML", "--name", "Statistics"])
    chat, chat_args, added, mcp, unknown, mcp_unknown, not_object, not_json, listed = home.batch(
        ["tool", "list_categories"], ["tool", "list_categories", "{}"],
        ["tool", "add_category", '{"arxiv_code": "cs.CL"}'], ["tool", "--mcp", "list_categories"],
        ["tool", "no_such_tool"], ["tool", "--mcp", "no_such_tool"], ["tool", "list_categories", "[1, 2]"],
        ["tool", "list_categories", "not json"], ["category", "list"])

    # categories added at one instant come newest id first
    assert (chat.status, chat.stdout) == (0, "[ID:2] Statistics (stat.ML)\n[ID:1] Machine Learning (cs.LG)\n"), chat
    assert chat_args.stdout == chat.stdout, chat_args
    assert (added.status, added.stdout) == (0, "Subscribed to cs.CL.\n"), added
    assert (mcp.status, mcp.stdout) == (0, "- cs.CL (cs.CL)\n- Statistics (stat.ML)\n- Machine Learning (cs.LG)\n"), mcp
    for result in (unknown, mcp_unknown):
        assert (result.status, result.stdout) == (1, ""), result
    for result in (not_object, not_json):
        assert (result.status, result.stdout) == (2, ""), result
    assert [line.split("\t")[1] for line in listed.lines] == ["cs.LG", "stat.ML", "cs.CL"], listed


def test_usage_errors_end_with_status_2_and_main_can_be_called_twice(home):
    for args in ((), ("frobnicate",), ("clock", "set")):
        result = home.run(*args, check=False)
        assert (result.status, result.stdout) == (2, ""), (args, result)
        assert "usage" in result.stderr, (args, result)

    bad, good, unknown, missing, shown, verbose = home.batch(
        ["clock", "set", "not-a-time"], ["clock", "set", "2026-08-10T08:00Z"], ["frobnicate"],
        ["category", "remove", "cs.LG"], ["clock", "show"], ["-v", "clock", "show"])
    assert (bad.status, bad.stdout) == (2, "") and "error" in bad.stderr.splitlines()[-1], bad
    assert (good.status, good.stdout, good.stderr) == (0, "2026-08-10T08:00:00Z\n", ""), good
    assert (unknown.status, unknown.stdout) == (2, "") and "usage" in unknown.stderr, unknown
    _error(missing, 1, "not subscribed: cs.LG")
    assert (shown.status, shown.stdout) == (0, "2026-08-10T08:00:00Z file\n"), shown
    assert (verbose.status, verbose.stdout) == (0, shown.stdout), verbose
