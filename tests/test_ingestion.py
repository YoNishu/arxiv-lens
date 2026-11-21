"""Ingestion: one row per paper, versions, announce types, categories, big feeds."""
from conftest import feed, totals

A, B, C, D = "2401.90001", "2401.90002", "2401.90003", "2401.90004"


def in_order(lines: list[str], expected: list[str]) -> bool:
    """True when every expected text starts one of the lines, in this order (others may lie between)."""
    position = 0
    for line in lines:
        if position < len(expected) and line.startswith(expected[position]):
            position += 1
    return position == len(expected)


def timeline(result) -> list[str]:
    """The timeline lines of `paper show` without their instants."""
    lines = result.lines[result.lines.index("timeline") + 1:]
    return [" ".join(line.split()[1:]) for line in lines]


def test_stored_papers_new_versions_and_announce_types(home, standin):
    world = standin(scenario={"feeds": {"cs.AI": feed((A, 1, "new"), (B, 3, "replace"), (C, 2, "replace-cross"))}})
    home.join(world)

    # new stores a paper; replace and replace-cross of papers not stored never create one.
    added, first = home.batch(["category", "add", "cs.AI"], ["cycle"])
    assert added.lines == ["added cs.AI"], added
    assert (totals(first.lines[-1])["new"], totals(first.lines[-1])["processed"]) == (1, 1), first
    assert home.sql("SELECT arxiv_id, version, status FROM papers") == [(A, 1, "done")]

    # A feed that lists a stored paper next to a new one: nothing lost, nothing doubled, and cross
    # stores a paper too. The same feed in three more cycles changes nothing and asks the API
    # nothing more.
    world.scenario(feeds={"cs.AI": feed((A, 1, "new"), (D, 1, "cross"), (B, 3, "replace"))})
    api_before = world.count("api")
    cycles = home.batch(["cycle"], ["cycle"], ["cycle"], ["cycle"])
    assert [r.status for r in cycles] == [0, 0, 0, 0], cycles
    runs = [totals(r.lines[-1]) for r in cycles]
    assert [(r["new"], r["updated"], r["skipped"], r["processed"], r["failed"]) for r in runs] == [
        (1, 0, 0, 1, 0), (0, 0, 0, 0, 0), (0, 0, 0, 0, 0), (0, 0, 0, 0, 0)], runs
    assert world.count("api") == api_before + 1, "a feed with nothing new does not go to the API"
    rows = home.sql("SELECT arxiv_id, id, created_at, published_at FROM papers ORDER BY arxiv_id")
    assert [r[0] for r in rows] == [A, D]
    first_id, first_created, first_published = rows[0][1:]

    # A replacement refreshes every field of the version, keeps the row, writes a version row and
    # timeline entries, and the paper is analysed again for its new version.
    world.scenario(feeds={"cs.AI": feed((A, 2, "replace"), (D, 1, "new"))})
    replaced, again, shown = home.batch(["cycle"], ["cycle"], ["paper", "show", A])
    assert (totals(replaced.lines[-1])["updated"], totals(replaced.lines[-1])["processed"]) == (1, 1), replaced
    assert (totals(again.lines[-1])["updated"], totals(again.lines[-1])["processed"]) == (0, 0), again
    paper = home.sql("SELECT id, created_at, published_at, version, title, abstract, pdf_url, status, attempts, "
                     "updated_at FROM papers WHERE arxiv_id = ?", (A,))[0]
    assert paper[:3] == (first_id, first_created, first_published)
    assert paper[3:9] == (2, f"Paper {A} (v2)", f"Abstract of {A}, version 2.", f"https://arxiv.org/pdf/{A}v2",
                          "done", 1)
    assert paper[9].replace("T", " ").startswith("2024-01-02 10:00:00"), paper
    assert home.sql("SELECT version, title FROM paper_versions WHERE paper_id = ? ORDER BY version", (first_id,)) == [
        (1, f"Paper {A} (v1)"), (2, f"Paper {A} (v2)")]
    assert home.sql("SELECT paper_version FROM analyses WHERE paper_id = ?", (first_id,)) == [(2,)]
    assert world.counts()[f"llm:{A}"] == 2
    assert shown.lines[:2] == [f"arxiv_id    {A}", "version     2"], shown
    assert in_order(timeline(shown), ["announced v1 cs.AI", "analysed v1", "replaced v2 cs.AI", "analysed v2"]), shown

    # replace-cross works the same way; an older version listed later is not an update.
    world.scenario(feeds={"cs.AI": feed((A, 4, "replace-cross"))})
    later = totals(home.run("cycle").lines[-1])
    assert (later["updated"], later["processed"]) == (1, 1), later
    world.scenario(feeds={"cs.AI": feed((A, 3, "replace"))})
    older = totals(home.run("cycle").lines[-1])
    assert (older["updated"], older["processed"]) == (0, 0), older
    assert home.sql("SELECT version, title FROM papers WHERE arxiv_id = ?", (A,)) == [(4, f"Paper {A} (v4)")]
    assert home.sql("SELECT paper_version FROM analyses WHERE paper_id = ?", (first_id,)) == [(4,)]
    assert world.counts()[f"llm:{A}"] == 3
    assert home.sql("SELECT count(*) FROM papers") == [(2,)]


def category_lines(result) -> dict[str, tuple[int, int]]:
    """`category list` as code -> (id, linked papers)."""
    found = {}
    for line in result.lines:
        category_id, code, papers, _name = line.split("\t")
        found[code] = (int(category_id), int(papers))
    return found


def feeds_line(result) -> str:
    return [line for line in result.lines if line.startswith("feeds ")][0][12:]


def test_paper_belongs_to_every_category_that_lists_it(home, standin):
    world = standin(scenario={"feeds": {
        "cs.AI": feed((A, 1, "new"), (B, 1, "new")),
        "cs.LG": feed((A, 1, "cross"), (C, 1, "new")),
        "stat.ML": feed((A, 2, "replace-cross")),
    }})
    home.join(world)
    results = home.batch(
        ["category", "add", "cs.AI"], ["category", "add", "cs.LG"], ["category", "add", "stat.ML"],
        ["cycle"], ["category", "list"], ["paper", "show", A], ["paper", "show", C],
    )
    assert [r.status for r in results] == [0] * 7, results
    listed = category_lines(results[4])
    ai, lg, ml = listed["cs.AI"][0], listed["cs.LG"][0], listed["stat.ML"][0]
    assert ai < lg < ml
    assert [line.split("\t")[1] for line in results[4].lines] == ["cs.AI", "cs.LG", "stat.ML"]
    assert {code: papers for code, (_id, papers) in listed.items()} == {"cs.AI": 2, "cs.LG": 2, "stat.ML": 1}

    # One paper, one row, one analysis, however many categories list it; category_id is the
    # lowest linked category.
    assert home.sql("SELECT arxiv_id, category_id FROM papers ORDER BY arxiv_id") == [(A, ai), (B, ai), (C, lg)]
    assert home.sql("SELECT pc.category_id FROM paper_categories pc JOIN papers p ON p.id = pc.paper_id "
                    "WHERE p.arxiv_id = ? ORDER BY pc.category_id", (A,)) == [(ai,), (lg,), (ml,)]
    assert feeds_line(results[5]) == "cs.AI cs.LG stat.ML"
    assert feeds_line(results[6]) == "cs.LG"
    assert world.counts()[f"llm:{A}"] == 1

    # Removing a category deletes only the papers no other category lists; the papers that stay
    # lose the feed and move to their lowest remaining category.
    removed, listed_after, shown = home.batch(["category", "remove", "cs.AI"], ["category", "list"],
                                              ["paper", "show", A])
    assert removed.lines == ["removed cs.AI: 1 papers deleted, 1 kept"], removed
    assert {code: papers for code, (_id, papers) in category_lines(listed_after).items()} == {"cs.LG": 2, "stat.ML": 1}
    assert feeds_line(shown) == "cs.LG stat.ML"
    assert [line for line in shown.lines if line.startswith("status ")] == ["status      done"]
    assert home.sql("SELECT category_id FROM papers WHERE arxiv_id = ?", (A,)) == [(lg,)]

    removed, again, listed_after, shown = home.batch(
        ["category", "remove", "cs.LG"], ["category", "remove", "cs.LG"], ["category", "list"], ["paper", "show", A])
    assert removed.lines == ["removed cs.LG: 1 papers deleted, 1 kept"], removed
    assert (again.status, again.stdout, again.stderr) == (1, "", "error: not subscribed: cs.LG\n"), again
    assert {code: papers for code, (_id, papers) in category_lines(listed_after).items()} == {"stat.ML": 1}
    assert feeds_line(shown) == "stat.ML"

    removed, listed_after = home.batch(["category", "remove", "stat.ML"], ["category", "list"])
    assert removed.lines == ["removed stat.ML: 1 papers deleted, 0 kept"], removed
    assert listed_after.lines == []


def test_whole_feed_is_stored_in_one_cycle_and_missing_ids_are_skipped(home, standin):
    ids = [f"2402.{n}" for n in range(90001, 90251)]
    gone = [ids[4], ids[120], ids[249]]
    stored = [i for i in ids if i not in gone]
    # Most papers have no PDF and fail at once: the cycle is about storing, not analysing.
    world = standin(scenario={"feeds": {"cs.AI": feed(*[(i, 1, "new") for i in ids])},
                              "gone": gone, "nopdf": stored[7:]})
    home.join(world)
    added, first = home.batch(["category", "add", "cs.AI"], ["cycle"])
    run = totals(first.lines[-1])
    assert (run["new"], run["updated"], run["skipped"], run["processed"], run["failed"]) == (247, 0, 3, 7, 240), first
    assert world.count("api") == 3, "250 ids are asked for in three batches"
    assert [r[0] for r in home.sql("SELECT arxiv_id FROM papers ORDER BY id")] == stored, "ids follow feed order"
    assert home.sql("SELECT feed, listed, new_papers, updated, skipped, error_message FROM run_feeds "
                    "WHERE run_id = ?", (run["run"],)) == [("cs.AI", 250, 247, 0, 3, None)]

    # The ids the API does not have are asked for again by the next cycle and counted again, and
    # stored once the API has them.
    world.scenario(feeds={"cs.AI": feed(*[(i, 1, "new") for i in ids])}, gone=gone[:1], nopdf=stored[7:])
    run = totals(home.run("cycle").lines[-1])
    assert (run["new"], run["skipped"], run["processed"]) == (2, 1, 2), run
    assert world.count("api") == 4
    assert home.sql("SELECT count(*) FROM papers") == [(249,)]
