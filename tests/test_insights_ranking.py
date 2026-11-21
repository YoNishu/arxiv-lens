"""Stage 9 of the insights build: buckets of a feed and a week, and their ranking as of the week's end."""
import dataclasses
import json
from datetime import date, timedelta

import pytest

from app.insights import asof, config, pipeline, ranking, work

A = "2608.90001"
B = "2608.90002"
C = "2608.90003"
D = "2608.90004"
E = "2608.90005"
F = "2608.90006"

W35 = date(2026, 8, 24)
W36 = date(2026, 8, 31)

# The citations of the worked example of the citation stage.
EXAMPLE_CITES = {C: [A, B], D: [A, B], E: [A, C], F: []}
EXAMPLE_ANNOUNCED = {A: "2026-08-24", B: "2026-08-25", C: "2026-08-26", D: "2026-08-27", F: "2026-08-28", E: "2026-09-01"}
EXAMPLE_NOVELTY = {C: 7, D: 7, F: 9, E: 5}


def paper(arxiv_id: str, announced: str, state: str = "analysed", withdrawn: bool = False, feeds=("cs.LG",),
          novelty: int | None = 7, published: str | None = None) -> dict:
    return {
        "arxiv_id": arxiv_id,
        "announced": announced,
        "published": published or announced + "T12:00:00Z",
        "state": state,
        "withdrawn": withdrawn,
        "feeds": list(feeds),
        "novelty": novelty if state == "analysed" else None,
    }


def citers_of(cites: dict[str, list[str]]) -> dict[str, list[str]]:
    citers = {}
    for source in sorted(cites):
        for cited in cites[source]:
            citers.setdefault(cited, []).append(source)
    return citers


def days(announced: dict[str, str]) -> dict[str, date]:
    return {paper_id: asof.day_of(day) for paper_id, day in announced.items()}


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("ARXIVLENS_HOME", str(tmp_path / "home"))
    loaded = config.load()
    return dataclasses.replace(loaded, ranking=dataclasses.replace(loaded.ranking, novelty=0.6, grounding=0.25, heat=0.15))


@pytest.fixture
def make_ctx(tmp_path, settings):
    def make(papers: list[dict], cites: dict[str, list[str]]):
        ctx = pipeline.Context(asof.of(date(2026, 9, 10)), settings, tmp_path / "out")
        ctx.papers = sorted(papers, key=lambda row: row["arxiv_id"])
        ctx.cites = cites
        ctx.citers = citers_of(cites)
        return ctx

    return make


def rows(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def members_of(row: dict) -> list[str]:
    return sorted(entry["arxiv_id"] for entry in row["papers"])


def test_members_of_a_bucket(make_ctx):
    work.reset()
    papers = [
        # Published in the week before, announced on the Monday of 2026-W35: it counts in W35.
        paper(A, "2026-08-24", feeds=("cs.LG", "stat.ML"), published="2026-08-21T17:00:00Z"),
        paper(B, "2026-08-25", state="pending"),
        paper(C, "2026-08-25", state="failed"),
        paper(D, "2026-08-26", withdrawn=True),
        paper(E, "2026-08-30"),
        paper(F, "2026-08-31", published="2026-08-28T09:00:00Z"),
    ]
    ctx = make_ctx(papers, {})
    units = ranking.run(ctx)
    found = rows(ctx.out / "ranking.jsonl")
    assert [(row["feed"], row["week"]) for row in found] == [
        ("cs.LG", "2026-W35"),
        ("cs.LG", "2026-W36"),
        ("stat.ML", "2026-W35"),
    ]
    assert [members_of(row) for row in found] == [[A, E], [F], [A]]
    assert ctx.buckets == found
    assert units == {"buckets": 3, "ranked": 4}
    assert work.counts()["ranking.buckets"] == 3
    assert sorted(ctx.files) == ["ranking.jsonl"]


def test_citations_count_as_of_the_week_end():
    target = A
    citers = {target: [B, C, D]}
    announced = days({A: "2026-08-20", B: "2026-08-24", C: "2026-08-30", D: "2026-08-31"})
    sunday = date(2026, 8, 30)
    # Announced on the Sunday that ends the week: counted; on the Monday after: not.
    assert ranking.cites_as_of(target, sunday, citers, announced) == 2
    assert ranking.cites_as_of(target, sunday + timedelta(days=1), citers, announced) == 3
    assert ranking.cites_as_of(target, date(2026, 8, 23), citers, announced) == 0
    assert ranking.cites_as_of(B, sunday, citers, announced) == 0


def test_references_count_as_of_the_week_end(settings):
    member, early, late = "2608.90010", "2608.90011", "2608.90012"
    x, y, z = "2608.90013", "2608.90014", "2608.90015"
    cites = {member: [early, late], x: [late], y: [early], z: [early]}
    citers = citers_of(cites)
    announced = {member: "2026-08-26", early: "2026-08-20", x: "2026-08-25", y: "2026-08-27", z: "2026-09-02"}

    # The late paper is announced on the Monday after the week: it neither grounds nor heats.
    after = days(dict(announced, **{late: "2026-08-31"}))
    row = ranking.rank_bucket("cs.LG", W35, [member], {member: 7}, cites, citers, after, settings.ranking)
    entry = row["papers"][0]
    assert (entry["grounding"], entry["heat"]) == (1, 2)

    # Announced on the Sunday instead, it counts with its two citers of the week.
    within = days(dict(announced, **{late: "2026-08-30"}))
    row = ranking.rank_bucket("cs.LG", W35, [member], {member: 7}, cites, citers, within, settings.ranking)
    entry = row["papers"][0]
    assert (entry["grounding"], entry["heat"]) == (2, 4)


def test_ranking_of_the_example(make_ctx, settings):
    work.reset()
    announced = days(EXAMPLE_ANNOUNCED)
    citers = citers_of(EXAMPLE_CITES)
    row = ranking.rank_bucket("cs.LG", W35, [C, D, F], EXAMPLE_NOVELTY, EXAMPLE_CITES, citers, announced, settings.ranking)
    assert row["feed"] == "cs.LG"
    assert row["week"] == "2026-W35"
    assert sorted(row) == ["feed", "papers", "week"]
    got = [dict(entry, score=round(entry["score"], 6)) for entry in row["papers"]]
    assert got == [
        {"arxiv_id": C, "grounding": 2, "heat": 4, "novelty": 7, "percentile": 100, "rank": 1, "score": 0.5439},
        {"arxiv_id": D, "grounding": 2, "heat": 4, "novelty": 7, "percentile": 50, "rank": 2, "score": 0.5439},
        {"arxiv_id": F, "grounding": 0, "heat": 0, "novelty": 9, "percentile": 0, "rank": 3, "score": 0.54},
    ]
    assert row["papers"][0]["score"] == row["papers"][1]["score"]
    assert all(isinstance(entry["score"], float) for entry in row["papers"])

    # Ranked with the end of the next week, the citer announced on 09-01 counts too.
    next_week = ranking.rank_bucket("cs.LG", W36, [C, D], EXAMPLE_NOVELTY, EXAMPLE_CITES, citers, announced, settings.ranking)
    assert [(entry["heat"], round(entry["score"], 6)) for entry in next_week["papers"]] == [(5, 0.550856), (5, 0.550856)]
    assert work.counts()["ranking.buckets"] == 2

    papers = [paper(A, EXAMPLE_ANNOUNCED[A], state="pending"), paper(B, EXAMPLE_ANNOUNCED[B], state="pending")]
    papers += [paper(p, EXAMPLE_ANNOUNCED[p], novelty=EXAMPLE_NOVELTY[p]) for p in (C, D, E, F)]
    ctx = make_ctx(papers, EXAMPLE_CITES)
    ranking.run(ctx)
    found = rows(ctx.out / "ranking.jsonl")
    assert [row["week"] for row in found] == ["2026-W35", "2026-W36"]
    assert [entry["arxiv_id"] for entry in found[0]["papers"]] == [C, D, F]
    assert [entry["arxiv_id"] for entry in found[1]["papers"]] == [E]
    assert found[1]["papers"][0]["percentile"] == 100


def test_equal_scores_percentiles_and_caps(settings):
    plain = settings.ranking

    def bucket(members, novelty, cites=None, announced=None, section=plain):
        cites = cites or {}
        announced = announced or {}
        known = days({p: "2026-08-25" for p in members})
        known.update(days(announced))
        return ranking.rank_bucket("cs.LG", W35, members, novelty, cites, citers_of(cites), known, section)

    # Equal scores: the smaller id ranks first, whatever order the members come in.
    row = bucket(["2608.90012", "2608.90011"], {"2608.90012": 6, "2608.90011": 6})
    assert [entry["arxiv_id"] for entry in row["papers"]] == ["2608.90011", "2608.90012"]

    expected = {1: [100], 2: [100, 0], 3: [100, 50, 0], 4: [100, 66, 33, 0], 10: [100, 88, 77, 66, 55, 44, 33, 22, 11, 0]}
    for m, percentiles in expected.items():
        members = [f"2608.{90100 + i}" for i in range(m)]
        row = bucket(members, {p: 5 for p in members})
        assert [entry["percentile"] for entry in row["papers"]] == percentiles, m
        assert [entry["rank"] for entry in row["papers"]] == list(range(1, m + 1)), m

    # Nine references ground like eight; seven ground less. Heat is left out to see the part alone.
    no_heat = dataclasses.replace(plain, heat=0.0)
    nine, eight, seven = "2608.90021", "2608.90022", "2608.90023"
    cites = {
        nine: [f"2608.{90200 + i}" for i in range(9)],
        eight: [f"2608.{90300 + i}" for i in range(8)],
        seven: [f"2608.{90400 + i}" for i in range(7)],
    }
    announced = {cited: "2026-08-20" for listed in cites.values() for cited in listed}
    row = bucket([nine, eight, seven], {nine: 5, eight: 5, seven: 5}, cites, announced, no_heat)
    by_id = {entry["arxiv_id"]: entry for entry in row["papers"]}
    assert by_id[nine]["grounding"] == 9
    assert by_id[nine]["score"] == by_id[eight]["score"]
    assert by_id[seven]["score"] < by_id[eight]["score"]

    # Heat 60 and heat 50 give the full heat part; 49 gives less.
    only_heat = dataclasses.replace(plain, novelty=0.0, grounding=0.0, heat=0.15)
    scores = {}
    for heat in (60, 50, 49):
        member, target = "2608.90030", "2608.90031"
        others = [f"2608.{90500 + i}" for i in range(heat - 1)]
        cites = {member: [target]}
        for other in others:
            cites[other] = [target]
        announced = {target: "2026-08-20"}
        announced.update({other: "2026-08-21" for other in others})
        row = bucket([member], {member: 5}, cites, announced, only_heat)
        assert row["papers"][0]["heat"] == heat
        scores[heat] = row["papers"][0]["score"]
    assert scores[60] == scores[50]
    assert round(scores[50], 6) == 0.15
    assert scores[49] < scores[50]
