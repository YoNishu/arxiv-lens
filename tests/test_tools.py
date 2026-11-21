"""The five tools under tools/, run as scripts on a played and built home."""
import csv
import io
import json
import os
import re
import subprocess
import sys

from conftest import ROOT, new_home

PAPER = re.compile(r"^(\d+)\. (.*) \((\S+)\)$")
DETAIL = re.compile(r"^   (.*) \| score (\S+) \| (.*)$")
ARXIV_ID = re.compile(r"^(\d{4}\.\d{4,5}|[a-z-]+/\d{7})$")
STAGES = ["snapshot", "text", "vocab", "vectors", "similar", "citations", "topics", "trends", "ranking", "digests"]


def rows(path) -> list[dict]:
    with open(path, "rb") as f:
        return [json.loads(line) for line in f if line.strip()]


def read_json(path):
    with open(path, "rb") as f:
        return json.loads(f.read())


def page_lines(folder, feed: str, week: str) -> list[str]:
    with open(folder / "digests" / feed / f"{week}.md", "rb") as f:
        return f.read().decode("utf-8").split("\n")


def by_id(path) -> dict[str, dict]:
    return {r["arxiv_id"]: r for r in rows(path)}


def ids_in(value) -> list[str]:
    """Every arXiv id named anywhere inside a planted entry of the manifest."""
    if isinstance(value, str):
        return [value] if ARXIV_ID.match(value) else []
    found = []
    items = value.values() if isinstance(value, dict) else value if isinstance(value, list) else []
    for item in items:
        found += ids_in(item)
    return found


def bare_python(script, *args, home_path, cwd):
    """Run a tool with only the standard library: isolated mode, no site, no PYTHONPATH."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ARXIVLENS_", "AZURE_", "ARXIV_"))}
    env["ARXIVLENS_HOME"] = str(home_path)
    env["PYTHONPATH"] = ""
    proc = subprocess.run([sys.executable, "-I", "-S", str(script), *args], cwd=cwd, env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=120)
    return proc.returncode, proc.stdout, proc.stderr


def test_weekly_mail_renders_a_closed_week(small, tmp_path):
    home = small.home_at["END"]
    folder = small.build["END"]
    feeds = read_json(folder / "stats.json")["inputs"]["feeds"]
    closed = {}
    for row in rows(folder / "ranking.jsonl"):
        if page_lines(folder, row["feed"], row["week"])[2].split(", ")[1] == "closed":
            closed.setdefault(row["feed"], []).append(row)
    feed = next(f for f in feeds if f in closed)
    row = max(closed[feed], key=lambda r: r["week"])
    week = row["week"]
    page = page_lines(folder, feed, week)
    monday, _, sunday = page[2].split(", ")[0].split(" ")
    shown = [(PAPER.match(page[i]), DETAIL.match(page[i + 1])) for i in range(4, len(page) - 1, 2)]
    assert shown and all(p and d for p, d in shown), page
    ranked = {p["arxiv_id"]: p for p in row["papers"]}
    members = len(row["papers"])

    script = ROOT / "tools" / "weekly_mail.py"
    status, out, err = bare_python(script, feed, home_path=home.path, cwd=tmp_path)
    assert status == 0, err
    lines = out.split("\n")
    assert out.endswith("\n") and lines[-1] == ""
    lines = lines[:-1]
    assert lines[0] == f"Subject: {feed} week {week}: {members} papers ranked"
    assert lines[1] == ""
    assert lines[2] == f"{feed}, {monday} to {sunday}"
    assert lines[3] == ""
    names = []
    for n, (p, d) in enumerate(shown):
        block = lines[4 + 5 * n: 9 + 5 * n]
        arxiv_id = p.group(3)
        topic = d.group(3)
        said = "no topic" if topic == "-" else f"topic: {topic}"
        if topic != "-" and topic not in names:
            names.append(topic)
        assert block == [
            f"{p.group(1)}. {p.group(2)}",
            f"   {d.group(1)}",
            f"   https://arxiv.org/abs/{arxiv_id}",
            f"   score {d.group(2)}, novelty {ranked[arxiv_id]['novelty']}/10, "
            f"{ranked[arxiv_id]['grounding']} references in the library, {said}",
            "",
        ], (n, block)
    rest = lines[4 + 5 * len(shown):]
    if names:
        blurbs = {}
        for t in read_json(folder / "topics.json")["topics"]:
            blurbs.setdefault(t["name"], t["blurb"])
        assert rest[0] == "Topics"
        assert rest[1:1 + len(names)] == [
            f"- {name}: {blurbs[name]}" if blurbs.get(name) else f"- {name}" for name in names]
        assert rest[1 + len(names)] == ""
    else:
        assert "Topics" not in rest
    assert lines[-1] == f"{members} papers ranked, {len(shown)} shown."

    # The week named, and the folder named instead of the home, give the same mail.
    assert bare_python(script, feed, week, home_path=home.path, cwd=tmp_path) == (0, out, "")
    elsewhere = tmp_path / "nowhere"
    assert bare_python(script, feed, week, "--folder", str(folder), home_path=elsewhere, cwd=tmp_path) == (0, out, "")

    status, out, err = bare_python(script, feed, "1999-W01", home_path=home.path, cwd=tmp_path)
    assert (status, out, err.strip()) == (1, "", f"error: no digest for {feed} 1999-W01")
    status, out, err = bare_python(script, feed, "W36", home_path=home.path, cwd=tmp_path)
    assert status == 2 and out == ""
    status, out, err = bare_python(script, feed, home_path=elsewhere, cwd=tmp_path)
    assert status == 1 and err.strip() == f"error: no result in {os.path.abspath(elsewhere / 'insights')}"


def test_related_ends_with_the_number_of_citing_papers(small, tmp_path):
    home = small.home_at["END"]
    folder = small.build["END"]
    citers: dict[str, list[str]] = {}
    cited: dict[str, list[str]] = {}
    for edge in rows(folder / "citations" / "edges.jsonl"):
        citers.setdefault(edge["dst"], []).append(edge["src"])
        cited.setdefault(edge["src"], []).append(edge["dst"])
    planted = sorted(set(ids_in(small.planted["late_cited"])))
    late = [x for x in planted
            if len(citers.get(x, [])) == 4 and all(o in citers[x] for o in planted if o != x)]
    assert len(late) == 1, (planted, small.planted["late_cited"])
    x = late[0]
    papers = by_id(folder / "papers.jsonl")
    nodes = by_id(folder / "citations" / "nodes.jsonl")
    assert nodes[x]["cited_by"] == 4

    result = home.tool("related", x)
    lines = result.lines
    assert lines[0] == f"{x}  {papers[x]['title']}"
    second = lines[1].split(", ", 2)
    assert second[0] == f"state {papers[x]['state']}" + (" (withdrawn)" if papers[x]["withdrawn"] else "")
    assert second[1] == "feeds " + (" ".join(papers[x]["feeds"]) or "-")
    assignment = by_id(folder / "assignments.jsonl").get(x)
    if assignment is None or assignment["topic"] is None:
        assert second[2] == "topic -"
    else:
        name = read_json(folder / "topics.json")["topics"][assignment["topic"]]["name"]
        assert second[2] == f"topic {assignment['topic']} {name}"
    assert lines[2] == ""

    heads = [lines.index("similar"), lines.index("cites"), lines.index("citing papers")]
    assert heads[0] == 3 and heads == sorted(heads)
    similar = by_id(folder / "similar.jsonl").get(x, {"similar": []})["similar"]
    assert lines[heads[0] + 1:heads[1]] == [
        f"  {format(s['score'], '.2f')}  {s['id']}  {papers[s['id']]['title']}" for s in similar]
    assert lines[heads[1] + 1:heads[2]] == [f"  {d}  {papers[d]['title']}" for d in sorted(cited.get(x, []))]
    assert lines[heads[2] + 1:-1] == [f"  {c}  {papers[c]['title']}" for c in sorted(citers[x])]
    assert lines[-1] == "cited by 4"

    unknown = home.tool("related", "2401.99999", check=False)
    assert (unknown.status, unknown.stdout, unknown.stderr.strip()) == (1, "", "error: no such paper: 2401.99999")
    empty = new_home(tmp_path / "empty")
    none = empty.tool("related", x, check=False)
    assert (none.status, none.stdout) == (1, "")
    assert none.stderr.strip() == f"error: no result in {os.path.abspath(empty.path / 'insights')}"


def test_topic_export_is_a_csv_of_the_members(small):
    home = small.home_at["END"]
    folder = small.build["END"]
    papers = by_id(folder / "papers.jsonl")
    topics = read_json(folder / "topics.json")["topics"]
    members: dict[int, list[tuple[float, str]]] = {}
    for row in rows(folder / "assignments.jsonl"):
        if row["topic"] is not None:
            members.setdefault(row["topic"], []).append((-row["sim"], row["arxiv_id"]))
    number = max(members, key=lambda t: (len(members[t]), -t))
    expected = [["arxiv_id", "title", "similarity"]]
    for minus_sim, arxiv_id in sorted(members[number]):
        expected.append([arxiv_id, papers[arxiv_id]["title"], format(-minus_sim, ".4f")])

    result = home.tool("topic_export", str(number))
    assert "\r" not in result.stdout and result.stdout.endswith("\n")
    assert list(csv.reader(io.StringIO(result.stdout))) == expected
    assert result.lines[0] == "arxiv_id,title,similarity"

    unknown = home.tool("topic_export", str(len(topics) + 5), check=False)
    assert (unknown.status, unknown.stdout) == (1, "")
    assert unknown.stderr.strip() == f"error: no such topic: {len(topics) + 5}"


def test_missing_refs_lists_ids_the_library_has_not_analysed(small, tmp_path):
    folder = small.build["END"]
    home = small.home_at["END"].copy(tmp_path / "h")
    external = rows(folder / "citations" / "external.jsonl")
    counts = {r["arxiv_id"]: r["cited_by"] for r in external}
    for node in rows(folder / "citations" / "nodes.jsonl"):
        if node["cited_by"] >= 1:
            counts[node["arxiv_id"]] = node["cited_by"]
    library = {}
    for arxiv_id, status, attempts, error, withdrawn_at, analysis in home.sql(
            "SELECT p.arxiv_id, p.status, p.attempts, p.error_message, p.withdrawn_at, a.id FROM papers p "
            "LEFT JOIN analyses a ON a.paper_id = p.id AND a.paper_version = p.version"):
        if withdrawn_at is not None:
            library[arxiv_id] = "withdrawn"
        elif analysis is not None:
            library[arxiv_id] = None
        elif str(status).lower() == "failed":
            library[arxiv_id] = f"failed after {attempts} tries: {' '.join((error or '').split())[:80]}".rstrip()
        else:
            library[arxiv_id] = "waiting"
    analysed = {a for a, said in library.items() if said is None}

    def expected(least: int) -> list[str]:
        kept = []
        for arxiv_id, count in counts.items():
            said = library.get(arxiv_id, "not tracked")
            if count >= least and said is not None:
                kept.append((-count, arxiv_id, said))
        kept.sort()
        untracked = len([k for k in kept if k[2] == "not tracked"])
        return [f"{-c}\t{a}\t{s}" for c, a, s in kept] + [
            f"{len(kept)} ids cited at least {least} times are not analysed: "
            f"{untracked} not tracked, {len(kept) - untracked} stored"]

    result = home.tool("missing_refs")
    assert result.lines == expected(2)
    top = min(external, key=lambda r: (-r["cited_by"], r["arxiv_id"]))
    stored_top = max([-1] + [counts[a] for a in counts if a in library and library[a] is not None])
    if top["cited_by"] >= 2 and top["cited_by"] > stored_top:
        assert result.lines[0] == f"{top['cited_by']}\t{top['arxiv_id']}\tnot tracked"
    assert not [line for line in result.lines[:-1] if line.split("\t")[1] in analysed]
    closing = re.match(r"^(\d+) ids cited at least 2 times are not analysed: (\d+) not tracked, (\d+) stored$",
                       result.lines[-1])
    assert closing, result.lines[-1]
    k, t, s = (int(g) for g in closing.groups())
    assert k == t + s == len(result.lines) - 1

    every = home.tool("missing_refs", "--min", "1")
    assert every.lines == expected(1)
    assert len(every.lines) >= len(result.lines)
    assert home.tool("missing_refs", "--min", "0", check=False).status == 2


def test_build_times_lists_the_runs(small, tmp_path):
    home = small.home_at["END"]
    runs = rows(small.build["END"] / "runs.jsonl")
    assert runs
    assert set(runs[-1]["seconds"]) == set(STAGES) | {"total"}

    result = home.tool("build_times")
    lines = result.lines
    assert lines[0] == "run\tstarted\tkind\tas_of\ttotal\t" + "\t".join(STAGES)
    table = lines[1:1 + len(runs)]
    for number, (run, line) in enumerate(zip(runs, table), start=1):
        cells = line.split("\t")
        assert cells[:4] == [str(number), run["started"], "build", run["as_of"]], line
        assert cells[4:] == [format(run["seconds"][k], ".3f") for k in ["total"] + STAGES], line
    assert lines[1 + len(runs)] == ""
    work = runs[-1]["work"]
    assert lines[-1] == f"run {len(runs)}: " + " ".join(f"{k}={work[k]}" for k in sorted(work))
    assert len(lines) == 2 + 2 * len(runs)

    last = home.tool("build_times", "--last", "1")
    assert last.lines[1].split("\t")[0] == str(len(runs))
    assert last.lines[-1] == lines[-1] and len(last.lines) == 4

    empty = new_home(tmp_path / "empty")
    none = empty.tool("build_times", check=False)
    assert (none.status, none.stdout) == (1, "")
    assert none.stderr.strip() == f"error: no run recorded in {os.path.abspath(empty.path / 'insights')}"
