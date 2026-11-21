"""The corpus generator and the two bundled corpora: determinism, layout of the texts, planted cases."""
import datetime as dt
import gzip
import importlib.util
import json
import os
import re
import subprocess
import sys
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "dev" / "make_corpus.py"
BUNDLED = [ROOT / "dev" / "corpus", ROOT / "dev" / "corpus_small"]

PRINTABLE = re.compile(r"^[\x20-\x7e]*$")
NEW_ID = re.compile(r"(?<![0-9.])([0-9]{2})([0-9]{2})\.([0-9]{4,5})(v[0-9]{1,2})?(?![0-9])")
OLD_ID = re.compile(r"(?<![A-Za-z0-9.-])((?:cs|math|physics)/[0-9]{2}([0-9]{2})[0-9]{3})(v[0-9]{1,2})?(?![0-9])")
HEADINGS = [{"1 Introduction"}, {"2 Method", "2 Approach", "2 Model", "2 Background"},
            {"3 Experiments", "3 Evaluation", "3 Results"},
            {"4 Conclusion", "4 Discussion", "4 Conclusion and Future Work"}]
ALL_HEADINGS = set().union(*HEADINGS)
REF_HEADINGS = {"A": "References", "B": "REFERENCES", "C": "Bibliography"}
WITHDRAWAL = "This paper has been withdrawn by the authors due to an error in the main result."
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def day(text: str) -> dt.date:
    return dt.date.fromisoformat(text)


def run_generator(out: Path, args: list, hash_seed: str = "0") -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONHASHSEED=hash_seed)
    return subprocess.run([sys.executable, str(SCRIPT), *args, "--out", str(out)], capture_output=True,
                          text=True, env=env, cwd=str(out.parent))


def arguments(manifest: dict) -> list:
    return ["--seed", str(manifest["seed"]), "--weeks", str(manifest["weeks"]), "--start", manifest["start"],
            "--feeds", ",".join(manifest["feeds"]), "--daily", str(manifest["daily"]),
            "--topics", str(manifest["topics"])]


def contents(folder: Path) -> dict:
    out = {}
    for path in sorted(folder.rglob("*")):
        if path.is_file():
            data = path.read_bytes()
            out[path.relative_to(folder).as_posix()] = gzip.decompress(data) if path.suffix == ".gz" else data
    return out


def find_ids(text: str) -> list:
    """(start, id without version) of every arXiv id with a valid month, left to right."""
    found = [(m.start(), m.group(1) + m.group(2) + "." + m.group(3)) for m in NEW_ID.finditer(text)
             if 1 <= int(m.group(2)) <= 12]
    found += [(m.start(), m.group(1)) for m in OLD_ID.finditer(text) if 1 <= int(m.group(2)) <= 12]
    return sorted(found)


class Loaded:
    def __init__(self, folder: Path):
        self.folder = folder
        self.files = contents(folder)
        self.manifest = json.loads(self.files["corpus.json"])
        self.papers = {}
        for line in self.files["papers.jsonl.gz"].decode("utf-8").splitlines():
            record = json.loads(line)
            self.papers[record["id"]] = record
        self.days = {}
        for name, data in self.files.items():
            if name.startswith("days/"):
                record = json.loads(data)
                self.days[record["day"]] = record
        self.planted = self.manifest["planted"]
        start, end = day(self.manifest["start"]), day(self.manifest["end"])
        calendar = [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]
        self.announce = [d for d in calendar if d.weekday() < 5]
        turn = day(self.manifest["month_turns"][0])
        self.A = [d.isoformat() for d in self.announce if d >= turn]
        self.B = [None] + [d.isoformat() for d in reversed(self.announce) if d < turn]

    def listings(self):
        for name, record in sorted(self.days.items()):
            for feed, items in record["feeds"].items():
                for ident, v, kind in items:
                    yield name, feed, ident, v, kind

    def versions(self):
        for ident, paper in sorted(self.papers.items()):
            for version in paper["versions"]:
                yield paper, version


@pytest.fixture(scope="module")
def corpora():
    return [Loaded(folder) for folder in BUNDLED]


def text_lines(version: dict) -> list:
    return [line for page in version["pages"] for line in page]


def split_entries(lines: list, style: str) -> list:
    entries = []
    for line in lines:
        marker = f"{len(entries) + 1}. " if style == "B" else f"[{len(entries) + 1}] "
        if line.startswith(marker):
            entries.append(line[len(marker):])
        else:
            entries[-1] += " " + line
    return entries


def test_random_numbers_match_their_vectors():
    spec = importlib.util.spec_from_file_location("make_corpus", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rng = module.Rng(0, "any")
    rng.state = 0
    assert [format(rng.next(), "016x") for _ in range(3)] == ["e220a8397b1dcdaf", "6e789e6aa1b965f4",
                                                             "06c45d188009454f"]
    rng = module.Rng(7, "topics")
    assert format(rng.state, "016x") == "f4bd5ca6c19cfcd0"
    assert [format(rng.next(), "016x") for _ in range(3)] == ["5c41995096e9c4b8", "56bdea86bcfec1e4",
                                                             "48c1439ea74627db"]
    rng = module.Rng(7, "topics")
    assert [rng.below(10) for _ in range(8)] == [3, 3, 2, 6, 1, 1, 9, 8]


def test_bundled_corpora_are_what_the_generator_writes(tmp_path, corpora):
    jobs = [(tmp_path / corpus.folder.name, arguments(corpus.manifest)) for corpus in corpora]
    with ThreadPoolExecutor(len(jobs)) as pool:
        results = list(pool.map(lambda job: run_generator(*job), jobs))
    for (out, args), result, corpus in zip(jobs, results, corpora):
        assert result.returncode == 0, result.stderr
        assert result.stdout == (f"wrote {out}: {corpus.manifest['papers']} papers, "
                                 f"{corpus.manifest['versions']} versions, {len(corpus.days)} days\n")
        again = contents(out)
        assert sorted(again) == sorted(corpus.files), corpus.folder.name
        for name in again:
            assert again[name] == corpus.files[name], f"{corpus.folder.name}/{name}"


def test_a_corpus_does_not_depend_on_the_hash_seed(tmp_path):
    args = ["--seed", "11", "--weeks", "6"]
    jobs = [(tmp_path / "zero", args, "0"), (tmp_path / "other", args, "4242")]
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda job: run_generator(*job), jobs))
    for result in results:
        assert result.returncode == 0, result.stderr
    first, second = contents(tmp_path / "zero"), contents(tmp_path / "other")
    assert len(first) == 2 + 1 + 42
    assert first == second


def check_title(paper: dict, version: dict, unicode_ids: set) -> list:
    title, problems = version["title"], []
    if paper["id"] not in unicode_ids and not PRINTABLE.match(title):
        problems.append("title characters")
    if title != title.strip() or "  " in title or "\n" in title:
        problems.append("title white space")
    bare = re.sub(r"\$[^$]*\$-?", " ", title)
    if re.search(r"[0-9]", bare) or bare.count(":") > 1 or ("?" in bare and not title.endswith("?")):
        problems.append("title punctuation or digits")
    if paper["id"] not in unicode_ids:
        for word in bare.split():
            if not re.fullmatch(r"[A-Za-z]+(-[A-Za-z]+)*[:?]?", word):
                problems.append(f"title word {word!r}")
    return problems


def check_abstract(paper: dict, version: dict, unicode_ids: set) -> list:
    text, problems = version["abstract"], []
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if not line or len(line) > 72 or line != line.strip() or "  " in line:
            problems.append(f"abstract line {i}")
        if line.endswith("-") and not (re.search(r"[a-z]{2}-$", line) and i + 1 < len(lines)
                                       and re.match(r"[a-z]", lines[i + 1])):
            problems.append(f"abstract soft hyphen {i}")
    folded = text.replace("\ufb01", "fi").replace("\ufb02", "fl") if paper["id"] in unicode_ids else text
    if not PRINTABLE.match(folded.replace("\n", " ")):
        problems.append("abstract characters")
    rest = re.sub(r"[0-9]\.[0-9]", "", re.sub(r"e\.g\.|i\.e\.|et al\.|w\.r\.t\.", "", text))
    if re.search(r"\.(?![ \n]|$)", rest) or not text.endswith("."):
        problems.append("abstract periods")
    if "http" in text or "$" in text or re.search(r"\[[0-9]", text) or find_ids(text):
        problems.append("abstract holds a link, TeX, a citation or an id")
    return problems


def check_pages(paper: dict, version: dict, planted: dict) -> list:
    problems, style, own = [], paper["style"], paper["id"]
    pages = version["pages"]
    if version["withdrawn"] or pages is None:
        return [] if version["withdrawn"] and pages is None else ["pages of a withdrawal"]
    if pages == [[]]:
        return [] if own == planted["no_text"]["id"] else ["empty PDF"]
    for page in pages:
        if not 1 <= len(page) <= 56:
            problems.append("page length")
        if page and (page[-1] in ALL_HEADINGS or page[-1] in REF_HEADINGS.values()):
            problems.append("a heading ends a page")
        for line in page:
            if not 1 <= len(line) <= 80 or not PRINTABLE.match(line) or line != line.strip() or "  " in line:
                problems.append(f"line {line!r}")
    lines = text_lines(version)
    stamp = dt.datetime.strptime(version["updated"], "%Y-%m-%dT%H:%M:%SZ")
    if lines[0] != f"arXiv:{own}v{version['v']} [{paper['primary']}] {stamp.day} {MONTHS[stamp.month - 1]} {stamp.year}":
        problems.append("stamp")
    if "Abstract" not in lines:
        problems.append("no Abstract line")
    heads = [i for i, line in enumerate(lines) if line in ALL_HEADINGS]
    decoy = own == planted["contents_decoy"]["id"]
    if len(heads) != (8 if decoy else 4) or [lines[i] in HEADINGS[n] for n, i in enumerate(heads[-4:])] != [True] * 4:
        problems.append("section headings")
    refheads = [i for i, line in enumerate(lines) if line in REF_HEADINGS.values()]
    if [lines[i] for i in refheads] != (["References"] if decoy else []) + [REF_HEADINGS[style]]:
        problems.append("reference heading")
    if decoy and not (lines[heads[0] - 1] == "Contents" and refheads[0] == heads[3] + 1 < heads[4]):
        problems.append("contents block")
    head = refheads[-1]
    body = lines[head + 1:]
    marker = (lambda n: f"{n}. ") if style == "B" else (lambda n: f"[{n}] ")
    count = 0
    for line in body:
        if line.startswith(marker(count + 1)):
            count += 1
        elif count == 0 or line[0] in "[0123456789":
            problems.append(f"reference line {line!r}")
    if count != len(version["refs"]):
        problems.append("number of entries")
    seen = 0
    for page in pages:
        if seen > head and page[0][0] not in "[0123456789":
            problems.append("an entry is split across pages")
        seen += len(page)
    for entry in split_entries(body, style):
        if len(find_ids(entry)) > 1:
            problems.append("two ids in one entry")
    for line in lines:
        for m in list(NEW_ID.finditer(line)) + list(OLD_ID.finditer(line)):
            before, after = line[:m.start()], line[m.end():]
            if not before.endswith(("arXiv:", "abs/", "pdf/", " ")) or after[:1] not in ("", ".", ",", ")", " "):
                problems.append(f"punctuation around {m.group(0)}")
    early = [ident for _, ident in find_ids("\n".join(lines[:head]))]
    if own == planted["zoo"]["id"]:
        listed = {ref["id"] for ref in version["refs"]}
        if len(early) != 2 or early[0] != own or early[1] in listed:
            problems.append("ids before the reference list")
    elif early != [own]:
        problems.append("ids before the reference list")
    text = "\n".join(lines)
    stripped = OLD_ID.sub(" ", NEW_ID.sub(" ", text))
    if re.search(r"[0-9]{4,}\.[0-9]{4,}", stripped) or re.search(r"[a-z]/[0-9]{7}", stripped):
        problems.append("id-like text")
    return problems


def test_texts_keep_the_layout_rules(corpora):
    for corpus in corpora:
        unicode_ids = {corpus.planted["unicode_title"]["id"]}
        problems = []
        for paper, version in corpus.versions():
            found = check_title(paper, version, unicode_ids) + check_abstract(paper, version, unicode_ids)
            found += check_pages(paper, version, corpus.planted)
            for name in paper["authors"]:
                if not re.fullmatch("[A-Z][a-z\u00e9\u00f6\u00fc\u0301]+ [A-Z][a-z\u00e9\u00f6\u00fc\u0301]+", name):
                    found.append(f"author {name!r}")
            comment = version["comment"]
            if version["withdrawn"] != (comment == WITHDRAWAL) or (
                    comment not in (None, WITHDRAWAL) and not re.fullmatch(r"[0-9]+ pages, [0-9]+ figures", comment)):
                found.append("comment")
            analysis = version["analysis"]
            if (analysis is None) != version["withdrawn"]:
                found.append("analysis of a withdrawal")
            if analysis:
                words = analysis["keywords"]
                lower = [w.lower() for w in words]
                if not 4 <= len(words) <= 7 or len(set(lower)) != len(lower) or not all(
                        re.fullmatch(r"[A-Za-z][a-z0-9]*(-[a-z0-9]+)*( [a-z0-9]+(-[a-z0-9]+)*){0,2}", w) for w in words):
                    found.append("keywords")
                texts = [analysis["summary"], analysis["methodology"], analysis["limitations"] or ""]
                if not all(PRINTABLE.match(t) for t in texts + analysis["key_contributions"]):
                    found.append("analysis characters")
            problems += [f"{corpus.folder.name} {paper['id']}v{version['v']}: {p}" for p in found]
        assert not problems, problems[:10]


def test_ids_are_unique_and_synthetic_and_days_name_known_versions(corpora):
    for corpus in corpora:
        lines = corpus.files["papers.jsonl.gz"].decode("utf-8").splitlines()
        assert len(lines) == len(corpus.papers) == corpus.manifest["papers"]
        assert [json.loads(line)["id"] for line in lines] == sorted(corpus.papers)
        by_month = {}
        for ident, paper in sorted(corpus.papers.items(), key=lambda item: item[1]["published"]):
            m = re.fullmatch(r"([0-9]{2})([0-9]{2})\.(9[0-9]{4})", ident)
            assert m and 1 <= int(m.group(2)) <= 12, ident
            assert paper["published"][2:4] + paper["published"][5:7] == ident[:4], ident
            by_month.setdefault(ident[:4], []).append(int(m.group(3)))
        for month, numbers in by_month.items():
            assert numbers == list(range(90001, 90001 + len(numbers))), month
        for name, record in corpus.days.items():
            assert sorted(record["feeds"]) == sorted(corpus.manifest["feeds"])
            if day(name).weekday() >= 5:
                assert not any(record["feeds"].values()), name
            for kind in ("gone", "nopdf", "bad", "junk"):
                assert record[kind] == sorted(record[kind]) and all(i in corpus.papers for i in record[kind])
            rank = {"new": 0, "cross": 1, "replace": 2, "replace-cross": 3}
            for items in record["feeds"].values():
                assert items == sorted(items, key=lambda item: (rank[item[2]], item[0])), name
            listed = {ident for items in record["feeds"].values() for ident, _, _ in items}
            late = {i for i in listed if corpus.papers[i]["versions"][0]["day"] > name}
            assert sorted(late) == record["gone"], name
        for name, feed, ident, v, kind in corpus.listings():
            versions = corpus.papers[ident]["versions"]
            assert 1 <= v <= len(versions), (name, ident, v)
            assert versions[v - 1]["day"] <= name or ident in corpus.days[name]["gone"], (name, ident, v)


def test_versions_never_go_backwards(corpora):
    for corpus in corpora:
        listed = set((ident, v, name) for name, _, ident, v, _ in corpus.listings())
        for ident, paper in corpus.papers.items():
            versions = paper["versions"]
            assert [v["v"] for v in versions] == list(range(1, len(versions) + 1)), ident
            assert versions[0]["updated"] == paper["published"], ident
            for before, after in zip(versions, versions[1:]):
                assert before["day"] < after["day"] and before["updated"] < after["updated"], ident
                assert not before["withdrawn"], ident
            for version in versions:
                assert day(version["day"]).weekday() < 5, ident
                assert (ident, version["v"], version["day"]) in listed, (ident, version["v"])


def test_references_point_to_earlier_papers(corpora):
    for corpus in corpora:
        late = corpus.planted["late_cited"]
        for paper, version in corpus.versions():
            seen = []
            for n, ref in enumerate(version["refs"], 1):
                where = (paper["id"], version["v"], n)
                assert ref["n"] == n, where
                kind, ident = ref["kind"], ref["id"]
                if kind == "internal":
                    first = corpus.papers[ident]["versions"][0]["day"]
                    exempt = paper["id"] in late["citers"] and ident == late["id"]
                    assert exempt or first < version["day"], where
                    assert ident != paper["id"], where
                elif kind == "external":
                    assert ident not in corpus.papers and ident, where
                elif kind == "self":
                    assert ident == paper["id"], where
                elif kind == "duplicate":
                    assert ident in seen, where
                else:
                    assert kind in ("other", "broken") and ident is None, where
                if ident:
                    assert not re.search(r"v[0-9]+$", ident), where
                    seen.append(ident)


def test_cites_follow_from_the_reference_lists(corpora):
    for corpus in corpora:
        for paper, version in corpus.versions():
            cites, other = [], 0
            for ref in version["refs"]:
                if ref["id"] is None:
                    other += 1
                elif ref["id"] != paper["id"] and ref["id"] not in cites:
                    cites.append(ref["id"])
            where = (paper["id"], version["v"])
            assert version["cites"] == cites and version["other_refs"] == other, where
            if not version["pages"] or version["pages"] == [[]]:
                assert version["refs"] == [], where
                continue
            lines = text_lines(version)
            head = max(i for i, line in enumerate(lines) if line == REF_HEADINGS[paper["style"]])
            entries = split_entries(lines[head + 1:], paper["style"])
            assert len(entries) == len(version["refs"]), where
            for entry, ref in zip(entries, version["refs"]):
                assert [ident for _, ident in find_ids(entry)] == ([ref["id"]] if ref["id"] else []), (where, entry)


def citing(corpus: Loaded, target: str) -> list:
    return sorted({p["id"] for p, v in corpus.versions() if any(r["id"] == target for r in v["refs"])})


def listed_on(corpus: Loaded, ident: str) -> list:
    return [(name, feed, v, kind) for name, feed, i, v, kind in corpus.listings() if i == ident]


def test_planted_cases_are_present(corpora):
    keys = {"id", "ids", "days", "cluster", "citers", "feed", "feeds", "count", "topic"}
    for corpus in corpora:
        A, B, P, papers, feeds = corpus.A, corpus.B, corpus.planted, corpus.papers, corpus.manifest["feeds"]
        f0, f1, f2 = feeds[0], feeds[1], feeds[min(2, len(feeds) - 1)]
        names = {"api_late", "body_only", "clean_days", "cocite", "contents_decoy", "fading_topic", "junk",
                 "late_cited", "late_link", "late_topic", "nfd_author", "no_text", "pdf_heals", "prototype",
                 "sunday_monday", "tex_title", "twins", "unicode_title", "withdrawn_early", "withdrawn_hub", "zoo"}
        assert set(P) == names | ({"burst"} if corpus.manifest["daily"] >= 20 else set())
        for name, case in P.items():
            assert set(case) <= keys and case.get("days", []) == sorted(case.get("days", [])), name
        for name in A[1], A[7]:
            items = [item for items in corpus.days[name]["feeds"].values() for item in items]
            assert items and all(kind == "new" for _, _, kind in items), name
            assert len({i for i, _, _ in items}) == len(items), name
            weekday = day(name).weekday()
            for ident, _, _ in items:
                published = day(papers[ident]["published"][:10])
                assert weekday == 0 or (day(name) - published).days <= weekday, ident
        proto = papers[P["prototype"]["id"]]
        assert proto["topics"] == [0] and [v["day"] for v in proto["versions"]] == [B[10], A[2]]
        assert proto["versions"][1]["title"] != proto["versions"][0]["title"]
        assert proto["versions"][1]["abstract"] == proto["versions"][0]["abstract"]
        body = papers[P["body_only"]["id"]]["versions"]
        assert [v["day"] for v in body] == [B[7], A[2]] and body[1]["refs"] != body[0]["refs"]
        assert all(body[1][k] == body[0][k] for k in ("title", "abstract", "analysis"))
        assert body[1]["pages"][0][5:] != body[0]["pages"][0][5:]
        link = papers[P["late_link"]["id"]]
        assert link["cross_later"] == [{"day": A[2], "feed": f1}]
        assert listed_on(corpus, link["id"]) == [(B[4], f0, 1, "new"), (A[2], f1, 1, "cross")]
        x = papers[P["late_cited"]["id"]]
        turn = day(corpus.manifest["month_turns"][0])
        assert x["primary"] == "math.OC" and x["versions"][0]["day"] == A[3]
        assert day(x["published"][:10]) == turn - dt.timedelta(days=20)
        assert listed_on(corpus, x["id"]) == [(A[3], f1, 1, "cross")]
        citers = P["late_cited"]["citers"]
        assert citing(corpus, x["id"]) == sorted(citers)
        assert [papers[c]["versions"][0]["day"] for c in citers] == [B[4], B[3], B[2], B[1]]
        forms = [r"\. arXiv:ID, [0-9]{4}\.$", r"arXiv preprint arXiv:IDv[0-9]+, [0-9]{4}\.$",
                 r"CoRR, abs/ID, [0-9]{4}\.$", r"\. arXiv:ID\.$", r"https://arxiv\.org/abs/IDv[0-9]+$",
                 r"Preprint, arXiv:ID \[", r"arXiv e-prints, ID, [0-9]{4}\.$", r"arXiv:IDv[0-9]+ \([0-9]{4}\)\.$",
                 r"http://arxiv\.org/pdf/IDv[0-9]+$"]
        used = set()
        for c in citers:
            version = papers[c]["versions"][0]
            assert [r["id"] for r in version["refs"]].count(x["id"]) == 1
            lines = text_lines(version)
            entries = split_entries(lines[lines.index(REF_HEADINGS[papers[c]["style"]]) + 1:], papers[c]["style"])
            entry = entries[[r["id"] for r in version["refs"]].index(x["id"])].replace(x["id"], "ID")
            used.add(next(i for i, form in enumerate(forms) if re.search(form, entry)))
        assert len(used) == 4
        heals = P["pdf_heals"]["id"]
        assert [n for n, r in sorted(corpus.days.items()) if heals in r["nopdf"]] == [
            d for d in sorted(corpus.days) if B[3] <= d < A[3]]
        hub = P["withdrawn_hub"]
        cluster = [papers[i] for i in hub["cluster"]]
        assert hub["id"] == hub["cluster"][3] and [p["versions"][0]["day"] for p in cluster] == [B[i] for i in range(11, 4, -1)]
        assert [listed_on(corpus, p["id"])[0][1] for p in cluster] == [f0, f1] * 3 + [f0]
        for p in cluster:
            assert p["topics"] == [1] and p["versions"][0]["analysis"]["keywords"] == cluster[0]["versions"][0]["analysis"]["keywords"]
            assert p["versions"][0]["title"] == cluster[0]["versions"][0]["title"]
            a = cluster[0]["versions"][0]["abstract"].replace("-\n", "").split()
            b = p["versions"][0]["abstract"].replace("-\n", "").split()
            assert len(a) == len(b) and len([1 for u, w in zip(a, b) if u != w]) <= 2
        assert len({p["versions"][0]["abstract"] for p in cluster}) == 7
        assert citing(corpus, hub["id"]) == sorted(hub["citers"]) and len(hub["citers"]) == 3
        assert sorted(papers[c]["versions"][0]["day"] for c in hub["citers"]) == sorted([B[6], B[6], B[3]])
        assert papers[hub["id"]]["versions"][-1]["withdrawn"] and papers[hub["id"]]["versions"][-1]["day"] == A[5]
        q = papers[P["api_late"]["id"]]
        assert q["primary"] == f2 and q["versions"][0]["day"] == A[5] and corpus.days[A[0]]["gone"] == [q["id"]]
        assert listed_on(corpus, q["id"]) == [(A[0], f2, 1, "new"), (A[5], f1, 1, "cross")]
        assert citing(corpus, q["id"]) == [] and q["published"][:10] == (day(corpus.manifest["month_turns"][0]) - dt.timedelta(days=1)).isoformat()
        early = papers[P["withdrawn_early"]["id"]]
        assert [n for n, r in sorted(corpus.days.items()) if early["id"] in r["bad"]] == [B[6]]
        assert [v["day"] for v in early["versions"]] == [B[6], B[5]] and early["versions"][1]["withdrawn"]
        junk = P["junk"]["id"]
        assert [n for n, r in sorted(corpus.days.items()) if junk in r["junk"]] == [d for d in sorted(corpus.days) if d >= B[8]]
        assert papers[P["no_text"]["id"]]["versions"][0]["pages"] == [[]]
        zoo = papers[P["zoo"]["id"]]["versions"][0]
        assert papers[P["zoo"]["id"]]["style"] == "A" and [r["kind"] for r in zoo["refs"]] == [
            "internal", "duplicate", "internal", "external", "external", "external", "external", "self",
            "internal", "internal", "broken", "broken", "other", "other"]
        assert zoo["cites"][3:6] == ["cs/9812901", "math/0001901", "0911.9412"] and zoo["other_refs"] == 4
        assert len(zoo["cites"]) == 8
        t1, t2 = (papers[i] for i in P["twins"]["ids"])
        for k in ("title", "abstract"):
            assert t1["versions"][0][k] == t2["versions"][0][k]
        for k in ("keywords", "novelty_score"):
            assert t1["versions"][0]["analysis"][k] == t2["versions"][0]["analysis"][k]
        assert t1["authors"] != t2["authors"] and t1["versions"][0]["refs"] != t2["versions"][0]["refs"]
        cocite = P["cocite"]
        assert len(cocite["citers"]) == 2 and all(papers[c]["versions"][0]["day"] == B[7] for c in cocite["citers"])
        for p, v in corpus.versions():
            hits = len(set(cocite["ids"]) & set(v["cites"]))
            assert hits in (0, 1) or (hits == 3 and p["id"] in cocite["citers"]), p["id"]
        s1, s2 = (papers[i] for i in P["sunday_monday"]["ids"])
        assert s1["published"][11:15] == "23:5" and s2["published"][11:15] == "00:0"
        assert day(s1["published"][:10]).weekday() == 6 and day(s2["published"][:10]).weekday() == 0
        assert s1["versions"][0]["day"] == s2["versions"][0]["day"] == P["sunday_monday"]["days"][0]
        title = papers[P["unicode_title"]["id"]]["versions"][0]
        assert all(ch in title["title"] for ch in "\u00f6\ufb01\u201c\u201d\u2013\u03b2") and "i\u0308" in title["title"]
        assert title["abstract"].count("\ufb01") == 1 and title["abstract"].count("\ufb02") == 1
        assert papers[P["tex_title"]["id"]]["versions"][0]["title"].count("$") >= 2
        n1, n2 = (papers[i] for i in P["nfd_author"]["ids"])
        shared = [a for a in n1["authors"] if "\u00e9" in a and unicodedata.normalize("NFD", a) in n2["authors"]]
        assert len(shared) == 1
        decoy = text_lines(papers[P["contents_decoy"]["id"]]["versions"][0])
        assert decoy.index("Contents") < decoy.index("References") < decoy.index("1 Introduction", decoy.index("References"))
        late_topic, fading = P["late_topic"]["topic"], P["fading_topic"]["topic"]
        assert late_topic == corpus.manifest["topics"] - 1 and fading == late_topic - 1
        firsts = [(p["versions"][0]["day"], p["topics"]) for p in papers.values()]
        assert not any(late_topic in t for d, t in firsts if d <= corpus.manifest["month_turns"][0])
        assert any(late_topic in t for d, t in firsts if d > corpus.manifest["month_turns"][0])
        if "burst" in P:
            items = corpus.days[P["burst"]["days"][0]]["feeds"][P["burst"]["feed"]]
            assert P["burst"]["count"] == 131 and len([1 for _, _, k in items if k == "new"]) == 131


def test_the_days_after_the_month_turn_hold_only_planted_events(corpora):
    for corpus in corpora:
        P, papers = corpus.planted, corpus.papers
        planted = {P[n]["id"] for n in ("prototype", "body_only", "late_link", "late_cited", "api_late",
                                        "withdrawn_hub", "pdf_heals", "junk")}
        firsts = {}
        for name, feed, ident, v, kind in corpus.listings():
            firsts.setdefault(ident, name)
        for name in corpus.A[:8]:
            record = corpus.days[name]
            for feed, items in record["feeds"].items():
                for ident, v, kind in items:
                    ordinary = kind in ("new", "cross") and v == 1 and firsts[ident] == name
                    assert ordinary or ident in planted, (name, feed, ident, v, kind)
            for kind in ("gone", "nopdf", "bad", "junk"):
                assert set(record[kind]) <= planted, (name, kind)
            for ident, paper in papers.items():
                if any(c["day"] == name for c in paper["cross_later"]):
                    assert ident == P["late_link"]["id"], (name, ident)


def test_corpus_files_hold_nothing_of_the_machine_that_made_them(corpora):
    for corpus in corpora:
        for name, data in corpus.files.items():
            text = data.decode("utf-8")
            assert "\t" not in text and "\r" not in text, name
            assert not re.search(r"/tmp/|/home/|/root/|/mnt/|/Users/|(?<![A-Za-z])[A-Z]:\\", text), name
        m = corpus.manifest
        expected = m["daily"] * 5 * m["weeks"] + (126 if "burst" in corpus.planted else 0)
        assert 0.85 * expected <= m["papers"] <= 1.15 * expected
        lines = [len(text_lines(v)) for _, v in corpus.versions() if v["pages"] and v["pages"] != [[]]]
        assert 90 <= sum(lines) / len(lines) <= 130
        assert len(corpus.files["papers.jsonl.gz"]) / m["papers"] <= 12000
        size = sum(f.stat().st_size for f in corpus.folder.rglob("*") if f.is_file())
        assert size <= (4 if m["weeks"] == 12 else 0.7) * 1024 * 1024
        readme = corpus.files["README.md"].decode("utf-8")
        assert f"python dev/make_corpus.py --seed {m['seed']} --out <DIR> " in readme
        assert f"From {corpus.B[1]} on, no paper is waiting for another try." in readme
        for name in corpus.planted:
            assert f"| {name} |" in readme, name
        for heading in REF_HEADINGS.values():
            assert f"`{heading}`" in readme
        assert re.search(r"\| [0-9.]+ \| junk \| " + corpus.B[8], readme)


def test_generator_refuses_a_start_that_is_not_a_monday_and_a_range_too_short(tmp_path):
    for args in (["--seed", "1", "--start", "2026-08-04"], ["--seed", "1", "--weeks", "3"],
                 ["--seed", "1", "--start", "2026-08-24", "--weeks", "4"]):
        result = run_generator(tmp_path / "out", args)
        assert result.returncode == 2, (args, result.stderr)
        assert result.stdout == "" and result.stderr.startswith("error: ") and result.stderr.count("\n") == 1, args
        assert not (tmp_path / "out").exists(), args
