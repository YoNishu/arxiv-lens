"""The library report and its audit, on copies of the played small corpus right after its build."""
import hashlib
import json
import os
import re

import pytest

HEADINGS = ["R1 Library by feed", "R2 Weekly intake", "R3 Vocabulary", "R4 Rising terms", "R5 Topics",
            "R6 Topics by feed", "R7 Most cited", "R8 Citation flow", "R9 Neighbour lists",
            "R10 Versions and withdrawals", "R11 Digest leaders", "R12 Last runs"]

REFUSED = "error: the report folder must not lie inside the result folder"


def rows(path):
    text = path.read_bytes().decode("utf-8")
    return [json.loads(line) for line in text.split("\n") if line]


def text_lines(path):
    text = path.read_bytes().decode("utf-8")
    assert text.endswith("\n") and not text.endswith("\n\n"), path
    return text[:-1].split("\n")


def files_of(folder):
    found = {}
    for top, _, names in os.walk(folder):
        for name in names:
            path = os.path.join(top, name)
            with open(path, "rb") as f:
                found[os.path.relpath(path, folder)] = f.read()
    return found


def section(lines, heading):
    start = lines.index("## " + heading) + 1
    end = start
    while end < len(lines) and not lines[end].startswith("## "):
        end += 1
    return lines[start:end]


def cells(line):
    return line[2:-2].split(" | ")


def tables_in(lines):
    """(caption or None, header, rows) of every table among the lines."""
    found = []
    i = 0
    while i < len(lines):
        if lines[i].startswith("| ") and i + 1 < len(lines) and lines[i + 1].startswith("|---"):
            caption = lines[i - 2] if i >= 2 and lines[i - 1] == "" and lines[i - 2].endswith(":") else None
            header = cells(lines[i])
            assert lines[i + 1] == "|" + "---|" * len(header), lines[i + 1]
            body = []
            i += 2
            while i < len(lines) and lines[i].startswith("| "):
                body.append(cells(lines[i]))
                assert len(body[-1]) == len(header), lines[i]
                i += 1
            found.append((caption, header, body))
        else:
            i += 1
    return found


def as_cell(text):
    return re.sub(r"[ \t\n\r\x0b\x0c]+", " ", text).replace("|", "\\|")


def audit_lines(home, folder=None):
    lines = text_lines((folder or home.path / "report") / "audit.txt")
    return lines[:-1], lines[-1]


@pytest.fixture(scope="module")
def reported(small, tmp_path_factory):
    """A copy of the last played day with one report written into its home."""
    home = small.home_at["END"].copy(tmp_path_factory.mktemp("report") / "h")
    return home, home.run("insights", "report", check=False)


def test_report_writes_two_files_outside_the_result_folder(small, tmp_path):
    home = small.home_at["END"].copy(tmp_path / "h")
    before = files_of(home.insights)
    elsewhere = tmp_path / "elsewhere"
    nested = home.insights / "sub"
    first, second, inside, same = home.batch(
        ["insights", "report"],
        ["insights", "report", "--to", str(elsewhere)],
        ["insights", "report", "--to", str(nested)],
        ["insights", "report", "--to", str(home.insights)],
    )
    assert first.status == 0, first.stderr
    assert second.status == 0, second.stderr
    assert sorted(os.listdir(home.path / "report")) == ["audit.txt", "report.md"]
    for name in ("report.md", "audit.txt"):
        assert (elsewhere / name).read_bytes() == (home.path / "report" / name).read_bytes(), name
    for refused in (inside, same):
        assert refused.status == 2
        assert REFUSED in refused.stderr.splitlines()
        assert refused.stdout == ""
    assert not nested.exists()
    assert files_of(home.insights) == before


def test_every_table_has_rows(reported):
    home, result = reported
    assert result.status == 0, result.stderr
    stats = json.loads((home.insights / "stats.json").read_bytes())
    lines = text_lines(home.path / "report" / "report.md")
    assert lines[0:2] == ["# Library report", ""]
    as_of, inputs = stats["as_of"], stats["inputs"]
    assert lines[2] == "As of %s (week %s, month %s)." % (as_of["day"], as_of["week"], as_of["month"])
    assert lines[3] == "%d papers: %d analysed, %d failed, %d pending; %d withdrawn. Feeds: %s." % (
        inputs["papers"], inputs["analysed"], inputs["failed"], inputs["pending"], inputs["withdrawn"],
        " ".join(inputs["feeds"]))
    assert lines[4] == "Settings %s." % stats["settings"][:12]
    assert [line[3:] for line in lines if line.startswith("## ")] == HEADINGS + ["Audit"]
    for heading in HEADINGS:
        found = tables_in(section(lines, heading))
        assert found, heading
        if heading == "R4 Rising terms":
            # A short library can have no rising term in any feed; the table then has its head only.
            rising = [entry for feed in inputs["feeds"]
                      for entry in json.loads((home.insights / "trends" / ("%s.json" % feed)).read_bytes())["rising"]]
            assert len(found[0][2]) == len(rising), heading
        else:
            assert any(body for _, _, body in found), heading
    assert str(home.path) not in "\n".join(lines)


def test_audit_is_clean_on_a_fresh_build(reported):
    home, result = reported
    assert result.status == 0, result.stderr
    recounts, closing = audit_lines(home)
    assert result.lines == [closing]
    assert closing == "audit: %d recounts, 0 differ" % len(recounts)
    for line in recounts:
        assert not line.startswith("differ"), line
        assert line.split("\t")[0] == "ok" and len(line.split("\t")) == 4, line
    names = [line.split("\t")[1] for line in recounts]
    stats = json.loads((home.insights / "stats.json").read_bytes())
    assert ["file %s" % rel for rel in sorted(stats["files"])] == [name for name in names if name.startswith("file ")]
    n = len(rows(home.insights / "papers.jsonl"))
    assert "ok\tpapers\t%d\t%d" % (n, n) in recounts
    report = text_lines(home.path / "report" / "report.md")
    assert report[-1] == "%d recounts, 0 differ (audit.txt)." % len(recounts)


def test_audit_names_a_file_edited_by_hand(small, tmp_path):
    home = small.home_at["END"].copy(tmp_path / "h")
    path = home.insights / "similar.jsonl"
    recorded = json.loads((home.insights / "stats.json").read_bytes())["files"]["similar.jsonl"]
    with open(path, "ab") as f:
        f.write(b"\n")
    edited = path.read_bytes()
    result = home.run("insights", "report", check=False)
    assert result.status == 1
    recounts, closing = audit_lines(home)
    assert result.lines == [closing]
    assert closing == "audit: %d recounts, 1 differ" % len(recounts)
    differing = [line for line in recounts if line.startswith("differ")]
    assert differing == ["differ\tfile similar.jsonl\t%s %d\t%s %d" % (
        hashlib.sha256(edited).hexdigest()[:12], len(edited), recorded["sha256"][:12], recorded["bytes"])]


def test_audit_recounts_papers_from_the_database(small, tmp_path):
    home = small.home_at["END"].copy(tmp_path / "h")
    path = home.insights / "papers.jsonl"
    papers = rows(path)
    removed = papers[-1]
    kept = path.read_bytes().split(b"\n")[:-2]
    with open(path, "wb") as f:
        f.write(b"".join(line + b"\n" for line in kept))
    result = home.run("insights", "report", check=False)
    assert result.status == 1
    recounts, closing = audit_lines(home)
    assert result.lines == [closing]
    assert "differ\tpapers\t%d\t%d" % (len(papers) - 1, len(papers)) in recounts
    assert removed["feeds"]
    for feed in removed["feeds"]:
        n = len([row for row in papers if feed in row["feeds"]])
        assert "differ\tpapers in %s\t%d\t%d" % (feed, n - 1, n) in recounts, feed


def test_two_reports_of_one_state_are_byte_equal(reported, tmp_path):
    home, first = reported
    assert first.status == 0, first.stderr
    for seed in ("0", "4242"):
        to = tmp_path / seed
        result = home.run("insights", "report", "--to", str(to), env={"PYTHONHASHSEED": seed})
        assert result.lines == first.lines
        for name in ("report.md", "audit.txt"):
            assert (to / name).read_bytes() == (home.path / "report" / name).read_bytes(), (seed, name)


def test_tables_agree_with_the_files(reported):
    home, result = reported
    assert result.status == 0, result.stderr
    lines = text_lines(home.path / "report" / "report.md")
    stats = json.loads((home.insights / "stats.json").read_bytes())
    papers = rows(home.insights / "papers.jsonl")

    library = tables_in(section(lines, "R1 Library by feed"))[0][2]
    inputs = stats["inputs"]
    assert library[-1][:6] == ["all", str(inputs["papers"]), str(inputs["analysed"]), str(inputs["failed"]),
                               str(inputs["pending"]), str(inputs["withdrawn"])]

    nodes = rows(home.insights / "citations" / "nodes.jsonl")
    top = sorted((node for node in nodes if node["cited_by"] >= 1),
                 key=lambda node: (-node["cited_by"], node["arxiv_id"]))
    caption, _, cited = tables_in(section(lines, "R7 Most cited"))[0]
    assert caption == "Papers of the library:"
    titles = {row["arxiv_id"]: row["title"] for row in papers}
    assert cited[0][:3] == [top[0]["arxiv_id"], as_cell(titles[top[0]["arxiv_id"]]), str(top[0]["cited_by"])]

    ranking = rows(home.insights / "ranking.jsonl")
    feeds = sorted({feed for row in papers for feed in row["feeds"]})
    expected = []
    for feed in feeds:
        closed = sorted((row for row in ranking if row["feed"] == feed and row["week"] < stats["as_of"]["week"]),
                        key=lambda row: row["week"])[-4:]
        for bucket in closed:
            for paper in bucket["papers"][:3]:
                expected.append([feed, bucket["week"], str(paper["rank"]), paper["arxiv_id"],
                                 format(paper["score"], ".2f")])
    caption, _, leaders = tables_in(section(lines, "R11 Digest leaders"))[0]
    assert caption == "First three of the last four closed weeks:"
    assert [row[:4] + row[5:] for row in leaders] == expected
    assert expected

    runs = rows(home.insights / "runs.jsonl")
    caption, header, seconds = tables_in(section(lines, "R12 Last runs"))[0]
    assert caption == "Seconds:"
    assert header[:5] == ["run", "started", "kind", "as of", "total"]
    last = runs[-5:]
    assert [row[0] for row in seconds] == [str(n) for n in range(len(runs) - len(last) + 1, len(runs) + 1)]
    assert [row[1:4] for row in seconds] == [[run["started"], run["kind"], run["as_of"]] for run in last]
