"""The stand-in: corpus days, faults, analyses, topic names, scenario mode and its count command."""
import gzip
import hashlib
import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STANDIN = ROOT / "dev" / "standin.py"
SMALL = ROOT / "dev" / "corpus_small"
BIG = ROOT / "dev" / "corpus"
CHAT = "/openai/deployments/gpt-4o/chat/completions?api-version=2024-12-01-preview"
ATOM = "{http://www.w3.org/2005/Atom}"
OPENSEARCH = "{http://a9.com/-/spec/opensearch/1.1/}"
ARXIV = "{http://arxiv.org/schemas/atom}"
SERVER_ERROR = {"error": {"message": "stand-in failure", "type": "server_error"}}


def load_standin(name: str = "standin"):
    spec = importlib.util.spec_from_file_location(name, STANDIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


standin = load_standin()


class Corpus:
    """A bundled corpus read the plain way, as the truth the answers are checked against."""

    def __init__(self, path: Path):
        self.path = path
        self.manifest = json.loads((path / "corpus.json").read_bytes())
        with gzip.open(path / "papers.jsonl.gz", "rb") as f:
            self.papers = {paper["id"]: paper for paper in (json.loads(line) for line in f if line.strip())}
        self.days = {
            file.stem: json.loads(file.read_bytes()) for file in sorted((path / "days").glob("*.json"))
        }

    def known(self, day: str, ids: list[str], gone=()) -> list[tuple[str, int]]:
        """What the API should list on a day: the latest version so far, in request order."""
        listed_gone = set(self.days.get(day, {}).get("gone", [])) | set(gone)
        found = []
        for raw in ids:
            arxiv_id = re.sub(r"v[0-9]+$", "", raw)
            paper = self.papers.get(arxiv_id)
            if paper is None or arxiv_id in listed_gone:
                continue
            versions = [v for v in paper["versions"] if v["day"] <= day]
            if versions:
                found.append((arxiv_id, versions[-1]["v"]))
        return found

    def first_day_with(self, key: str) -> tuple[str, str]:
        for day, data in self.days.items():
            if data[key]:
                return day, data[key][0]
        raise AssertionError(f"no day of {self.path.name} has {key}")

    def ordinary(self, day: str, skip=()) -> str:
        """A paper with text and an analysis in every version and no fault on the day."""
        faults = set()
        for key in ("gone", "nopdf", "bad", "junk"):
            faults.update(self.days[day][key])
        for arxiv_id in sorted(self.papers):
            versions = self.papers[arxiv_id]["versions"]
            if arxiv_id in faults or arxiv_id in skip or versions[0]["day"] > day:
                continue
            if all(v["pages"] and v["pages"] != [[]] and v["analysis"] for v in versions):
                return arxiv_id
        raise AssertionError("no ordinary paper")


@pytest.fixture(scope="module")
def small():
    return Corpus(SMALL)


@pytest.fixture(scope="module")
def big():
    return Corpus(BIG)


class Served:
    """dev/standin.py served from a thread of the test, with its own home and control file."""

    def __init__(self, folder: Path, corpus=None, scenario=None):
        self.home = folder / "home"
        self.home.mkdir(parents=True)
        self.control_file = folder / "control.json"
        self.server = standin.make_server(0, corpus=corpus, scenario=scenario, home=self.home,
                                          control=self.control_file)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05},
                                       daemon=True)
        self.thread.start()

    def set_day(self, day: str) -> None:
        (self.home / "clock").write_text(f"{day}T08:00:00Z\n", encoding="ascii")

    def control(self, **keys) -> None:
        # A new file each time, as an editor that saves by replacing would leave it.
        tmp = self.control_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(keys), encoding="utf-8")
        os.replace(tmp, self.control_file)

    def get(self, path: str) -> tuple[int, bytes]:
        try:
            with urllib.request.urlopen(self.url + path, timeout=60) as answer:
                return answer.status, answer.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def post(self, path: str, body: dict) -> tuple[int, dict]:
        request = urllib.request.Request(self.url + path, data=json.dumps(body).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=60) as answer:
                return answer.status, json.loads(answer.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def api(self, ids: list[str], start: int = 0, size: int = 100) -> tuple[int, bytes]:
        query = urllib.parse.urlencode({"id_list": ",".join(ids), "start": start, "max_results": size})
        return self.get("/api/query?" + query)

    def counts(self) -> dict[str, int]:
        return json.loads(self.get("/_n")[1])

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(10)


@pytest.fixture
def serve(tmp_path):
    made: list[Served] = []

    def make(corpus=None, scenario=None) -> Served:
        served = Served(tmp_path / f"standin{len(made)}", corpus=corpus, scenario=scenario)
        made.append(served)
        return served

    yield make
    for served in made:
        served.close()


def rss_items(data: bytes) -> list[dict]:
    items = []
    for item in ET.fromstring(data).iter("item"):
        name = item.find("guid").text[len("oai:arXiv.org:"):]
        arxiv_id, _, version = name.rpartition("v")
        items.append({
            "id": arxiv_id, "v": int(version), "kind": item.find(ARXIV + "announce_type").text,
            "title": item.find("title").text, "link": item.find("link").text,
            "description": item.find("description").text, "creator": item.find(
                "{http://purl.org/dc/elements/1.1/}creator").text,
            "category": item.find("category").text,
        })
    return items


def atom_entries(data: bytes) -> tuple[int, list[dict]]:
    root = ET.fromstring(data)
    entries = []
    for entry in root.findall(ATOM + "entry"):
        name = entry.find(ATOM + "id").text.rsplit("/abs/", 1)[1]
        arxiv_id, _, version = name.rpartition("v")
        comment = entry.find(ARXIV + "comment")
        entries.append({
            "id": arxiv_id, "v": int(version),
            "title": entry.find(ATOM + "title").text, "abstract": entry.find(ATOM + "summary").text,
            "published": entry.find(ATOM + "published").text, "updated": entry.find(ATOM + "updated").text,
            "authors": [author.find(ATOM + "name").text for author in entry.findall(ATOM + "author")],
            "comment": comment.text if comment is not None else None,
            "primary": entry.find(ARXIV + "primary_category").get("term"),
            "categories": [c.get("term") for c in entry.findall(ATOM + "category")],
            "pdf": [link.get("href") for link in entry.findall(ATOM + "link") if link.get("title") == "pdf"],
        })
    return int(root.find(OPENSEARCH + "totalResults").text), entries


def analysis_request(arxiv_id: str, version: int, title: str = "Anything at all") -> dict:
    return {
        "model": "gpt-4o", "temperature": 0.3, "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": "You analyse research papers."},
            {"role": "user", "content": f"arXiv: {arxiv_id}v{version}\nPaper title: {title}\n\n"
                                        f"Abstract: Some words.\n\nFull text:\nMore words."},
        ],
    }


def label_request(*contents: str) -> dict:
    return {"model": "gpt-4o", "messages": [{"role": "user", "content": text} for text in contents]}


def content_of(body: dict):
    return json.loads(body["choices"][0]["message"]["content"])


def label_key(*contents: str) -> str:
    return "label:" + hashlib.sha256("\n".join(contents).encode("utf-8")).hexdigest()[:12]


def answer_while_counted(served: Served, key: str, ask) -> tuple[int, dict]:
    """Start a slow request; see its counter rise while the answer is still out; then take it."""
    before = served.counts().get(key, 0)
    result = {}
    worker = threading.Thread(target=lambda: result.setdefault("answer", ask()))
    worker.start()
    for _ in range(3000):
        if served.counts().get(key, 0) > before:
            break
        time.sleep(0.01)
    assert served.counts().get(key, 0) == before + 1
    assert worker.is_alive(), "the answer came before the wait was over"
    worker.join(60)
    return result["answer"]


def next_day(day: str) -> str:
    return (date.fromisoformat(day) + timedelta(days=1)).isoformat()


def test_importing_the_file_starts_nothing_and_an_empty_world_still_names_topics(serve):
    threads = threading.active_count()
    saved = sys.argv
    sys.argv = ["standin.py", "--port", "not-a-port", "--corpus", "/nowhere"]
    try:
        module = load_standin("standin_imported_again")
    finally:
        sys.argv = saved
    assert threading.active_count() == threads
    assert callable(module.make_server) and callable(module.World)
    probe = (
        "import importlib.util, json, sys, threading\n"
        "spec = importlib.util.spec_from_file_location('s', sys.argv[1])\n"
        "spec.loader.exec_module(importlib.util.module_from_spec(spec))\n"
        "heavy = [n for n in ('pymupdf', 'fitz', 'app', 'sqlalchemy', 'httpx') if n in sys.modules]\n"
        "print(json.dumps([heavy, threading.active_count()]))\n"
    )
    done = subprocess.run([sys.executable, "-c", probe, str(STANDIN)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == [[], 1]

    served = serve()
    status, data = served.get("/rss/cs.AI")
    assert status == 200 and rss_items(data) == []
    status, data = served.api(["2401.90001"])
    assert status == 200 and atom_entries(data) == (0, [])
    assert served.get("/pdf/2401.90001v1")[0] == 404
    assert served.post(CHAT, analysis_request("2401.90001", 1)) == (404, SERVER_ERROR)
    status, body = served.post(CHAT, label_request("Name this group of papers.", "Terms: graph, node, edge"))
    assert status == 200
    assert content_of(body) == {"name": "Graph and Node", "blurb": "Papers about graph, node and edge."}
    assert served.counts() == {
        "rss:cs.AI": 1, "api": 1, "pdf:2401.90001": 1, "llm:2401.90001": 1,
        label_key("Name this group of papers.", "Terms: graph, node, edge"): 1,
    }
    assert served.get("/nothing/here") == (404, b"not found")


def test_feeds_follow_the_day_of_the_clock(serve, small):
    served = serve(corpus=SMALL)
    feeds = small.manifest["feeds"]
    start = small.manifest["start"]
    second = next_day(start)
    saturday = (date.fromisoformat(start) + timedelta(days=5)).isoformat()
    before = (date.fromisoformat(start) - timedelta(days=1)).isoformat()
    assert small.days[start]["feeds"] != small.days[second]["feeds"]
    for day in (start, second, saturday, before):
        served.set_day(day)
        for feed in feeds:
            status, data = served.get(f"/rss/{feed}")
            assert status == 200, (day, feed)
            expected = small.days[day]["feeds"][feed] if day in small.days else []
            assert [[i["id"], i["v"], i["kind"]] for i in rss_items(data)] == expected, (day, feed)
    assert all(not small.days[saturday]["feeds"][feed] for feed in feeds)

    served.set_day(start)
    item = rss_items(served.get(f"/rss/{feeds[0]}")[1])[0]
    paper = small.papers[item["id"]]
    version = paper["versions"][item["v"] - 1]
    assert item["title"] == version["title"]
    assert item["link"] == f"https://arxiv.org/abs/{item['id']}"
    assert item["description"] == f"arXiv:{item['id']}v{item['v']} Announce Type: {item['kind']}\n" \
                                  f"Abstract: {version['abstract']}"
    assert item["creator"] == ", ".join(paper["authors"])
    assert item["category"] == feeds[0]
    assert served.counts()[f"rss:{feeds[0]}"] == 5


def test_api_gives_the_latest_version_as_of_the_day(serve, small, big):
    served = serve(corpus=SMALL)
    replaced = next(i for i in sorted(small.papers) if len(small.papers[i]["versions"]) > 1
                    and not small.papers[i]["versions"][1]["withdrawn"])
    first, second = small.papers[replaced]["versions"][:2]
    gone_day, gone_id = small.first_day_with("gone")
    later = sorted(small.papers, key=lambda i: small.papers[i]["versions"][0]["day"])[-1]
    others = sorted(small.papers)[:5]
    asked = [later, f"{replaced}v1", gone_id, "0101.99999"] + others[::-1]
    for day in (first["day"], (date.fromisoformat(second["day"]) - timedelta(days=1)).isoformat(),
                second["day"], gone_day, small.manifest["end"]):
        served.set_day(day)
        status, data = served.api(asked)
        assert status == 200, day
        total, entries = atom_entries(data)
        expected = small.known(day, asked)
        assert [(e["id"], e["v"]) for e in entries] == expected, day
        assert total == len(expected), day
    served.set_day(gone_day)
    assert atom_entries(served.api([gone_id])[1]) == (0, [])
    served.set_day(first["day"])
    assert [(e["id"], e["v"]) for e in atom_entries(served.api([replaced])[1])[1]] == [(replaced, first["v"])]
    served.set_day(second["day"])
    entry = atom_entries(served.api([replaced])[1])[1][0]
    paper = small.papers[replaced]
    assert entry == {
        "id": replaced, "v": second["v"], "title": second["title"], "abstract": second["abstract"],
        "published": paper["published"], "updated": second["updated"], "authors": paper["authors"],
        "comment": second["comment"], "primary": paper["primary"],
        "categories": second["categories"] + [c["feed"] for c in paper["cross_later"]
                                              if c["day"] <= second["day"] and c["feed"] not in second["categories"]],
        "pdf": [f"https://arxiv.org/pdf/{replaced}v{second['v']}"],
    }
    linked = [i for i in sorted(small.papers) if small.papers[i]["cross_later"]]
    for arxiv_id in linked:
        later_link = small.papers[arxiv_id]["cross_later"][0]
        for day, present in ((later_link["day"], True),
                             ((date.fromisoformat(later_link["day"]) - timedelta(days=1)).isoformat(), False)):
            served.set_day(day)
            entries = atom_entries(served.api([arxiv_id])[1])[1]
            if entries:
                assert (later_link["feed"] in entries[0]["categories"]) == present, (arxiv_id, day)

    burst = big.manifest["planted"]["burst"]
    many = serve(corpus=BIG)
    many.set_day(burst["days"][0])
    ids = [i for i, _v, kind in big.days[burst["days"][0]]["feeds"][burst["feed"]] if kind == "new"]
    assert len(ids) >= 131
    pages = []
    for start in range(0, len(ids), 100):
        status, data = many.api(ids, start=start, size=100)
        assert status == 200
        total, entries = atom_entries(data)
        assert total == len(ids)
        pages.append([e["id"] for e in entries])
    assert [len(page) for page in pages] == [min(100, len(ids) - start) for start in range(0, len(ids), 100)]
    assert [arxiv_id for page in pages for arxiv_id in page] == ids


def test_pdf_text_survives_render_and_extraction(small, big):
    from app.services.pdf_extractor import extract_text

    checked = 0
    for corpus, ids in ((small, sorted(small.papers)), (big, sorted(big.papers)[::10])):
        for arxiv_id in ids:
            for version in corpus.papers[arxiv_id]["versions"]:
                pages = version["pages"]
                if pages is None:
                    continue
                text = extract_text(standin.render_pdf(pages))
                if all(not lines for lines in pages):
                    assert text == "", (arxiv_id, version["v"])
                else:
                    assert text == standin.expected_text(pages), (arxiv_id, version["v"])
                checked += 1
    assert checked > len(small.papers)


def test_faults_come_from_day_files_and_from_the_control_file(serve, small):
    served = serve(corpus=SMALL)
    feeds = small.manifest["feeds"]

    day, arxiv_id = small.first_day_with("nopdf")
    served.set_day(day)
    assert served.get(f"/pdf/{arxiv_id}v1")[0] == 404
    healed = next(d for d in sorted(small.days) if d > day and arxiv_id not in small.days[d]["nopdf"])
    served.set_day(healed)
    status, data = served.get(f"/pdf/{arxiv_id}v1")
    assert status == 200 and data.startswith(b"%PDF")
    assert served.counts()[f"pdf:{arxiv_id}"] == 2

    day, arxiv_id = small.first_day_with("bad")
    served.set_day(day)
    assert served.post(CHAT, analysis_request(arxiv_id, 1)) == (500, SERVER_ERROR)
    fine = next(d for d in sorted(small.days) if d > day and arxiv_id not in small.days[d]["bad"])
    served.set_day(fine)
    status, body = served.post(CHAT, analysis_request(arxiv_id, 1))
    assert status == 200 and content_of(body) == small.papers[arxiv_id]["versions"][0]["analysis"]

    day, arxiv_id = small.first_day_with("junk")
    served.set_day(day)
    status, body = served.post(CHAT, analysis_request(arxiv_id, 1))
    expected = dict(small.papers[arxiv_id]["versions"][0]["analysis"])
    expected["keywords"] = ", ".join(expected["keywords"])
    assert status == 200 and content_of(body) == expected

    quiet = small.manifest["end"]
    served.set_day(quiet)
    plain = small.ordinary(quiet)
    other = small.ordinary(quiet, skip={plain})
    assert served.get(f"/pdf/{plain}v1")[0] == 200
    served.control(nopdf=[plain], bad=[other])
    assert served.get(f"/pdf/{plain}v1")[0] == 404
    assert served.post(CHAT, analysis_request(other, 1)) == (500, SERVER_ERROR)
    served.control(junk=[plain], gone=[other])
    keywords = small.papers[plain]["versions"][0]["analysis"]["keywords"]
    assert content_of(served.post(CHAT, analysis_request(plain, 1))[1])["keywords"] == ", ".join(keywords)
    assert [e["id"] for e in atom_entries(served.api([other, plain])[1])[1]] == [plain]

    served.control(slow=1.0)
    status, body = answer_while_counted(served, f"llm:{plain}", lambda: served.post(CHAT, analysis_request(plain, 1)))
    assert status == 200 and content_of(body) == small.papers[plain]["versions"][0]["analysis"]

    served.control(feed_fail=[feeds[0]])
    assert served.get(f"/rss/{feeds[0]}")[0] == 500
    assert served.get(f"/rss/{feeds[1]}")[0] == 200
    assert served.counts()[f"rss:{feeds[0]}"] == 1

    for _round in range(2):
        served.control(api_fail_from=3)
        assert [served.api([plain])[0] for _ in range(4)] == [200, 200, 500, 500]


def test_an_analysis_is_found_by_the_arxiv_line_whatever_the_title(serve, small):
    served = serve(corpus=SMALL)
    quiet = small.manifest["end"]
    served.set_day(quiet)
    plain = small.ordinary(quiet)
    expected = small.papers[plain]["versions"][0]["analysis"]
    for title in ("A title nobody wrote", small.papers[plain]["versions"][0]["title"], ""):
        status, body = served.post(CHAT, analysis_request(plain, 1, title))
        assert status == 200 and content_of(body) == expected, title
        assert body["model"] == "gpt-4o" and body["choices"][0]["message"]["role"] == "assistant"
    request = analysis_request(plain, 1)
    request["messages"] = [{"role": "system", "content": "You analyse research papers."},
                           {"role": "user", "content": "Read this paper.\narXiv: %sv1\nMore text." % plain}]
    assert content_of(served.post(CHAT, request)[1]) == expected
    assert served.counts()[f"llm:{plain}"] == 4

    revised = next(i for i in sorted(small.papers) if len(small.papers[i]["versions"]) > 1
                   and small.papers[i]["versions"][1]["analysis"] and i not in small.days[quiet]["junk"]
                   and i not in small.days[quiet]["bad"])
    status, body = served.post(CHAT, analysis_request(revised, 2))
    assert status == 200 and content_of(body) == small.papers[revised]["versions"][1]["analysis"]

    withdrawn = next((i, v["v"]) for i in sorted(small.papers) for v in small.papers[i]["versions"] if v["withdrawn"])
    assert served.post(CHAT, analysis_request(*withdrawn)) == (404, SERVER_ERROR)
    assert served.post(CHAT, analysis_request("0101.99999", 1)) == (404, SERVER_ERROR)
    assert served.post(CHAT, analysis_request(plain, 99)) == (404, SERVER_ERROR)

    with_tools = label_request("Find papers about graphs.")
    with_tools["tools"] = [{"type": "function", "function": {"name": "search", "parameters": {}}}]
    assert served.post(CHAT, with_tools)[0] == 400
    streamed = label_request("Find papers about graphs.")
    streamed["stream"] = True
    assert served.post(CHAT, streamed)[0] == 400
    assert not [key for key in served.counts() if key.startswith("label:")]


def test_topic_names_are_a_function_of_the_prompt_and_are_counted(serve):
    served = serve()
    asked = ("You name groups of papers.", "Terms: graph, node embedding, edge, walk\nPapers: 12")
    first = served.post(CHAT, label_request(*asked))
    assert first == served.post(CHAT, label_request(*asked))
    assert first[0] == 200
    raw = first[1]["choices"][0]["message"]["content"]
    assert raw == '{"blurb":"Papers about graph, node embedding and edge.","name":"Graph and Node embedding"}'
    digest = label_key(*asked)[len("label:"):]
    assert served.counts() == {"label:" + digest: 2}

    cases = [
        (("No list of terms here.",), None),
        (("Terms: graph",), {"name": "Graph", "blurb": "Papers about graph."}),
        (("Words first.\nTerms:  graph ,  walk \nThen more.",), {"name": "Graph and Walk",
                                                              "blurb": "Papers about graph and walk."}),
    ]
    for contents, expected in cases:
        status, body = served.post(CHAT, label_request(*contents))
        if expected is None:
            key = label_key(*contents)
            expected = {"name": "Topic " + key[len("label:"):][:6], "blurb": "No terms given."}
        assert (status, content_of(body)) == (200, expected), contents
        assert served.counts()[label_key(*contents)] == 1, contents

    for _round in range(2):
        served.control(label_fail=3)
        statuses = [served.post(CHAT, label_request(*asked))[0] for _ in range(5)]
        assert statuses == [500, 500, 500, 200, 200]
    assert served.counts()["label:" + digest] == 12

    served.control(label_slow=1.0)
    status, body = answer_while_counted(served, "label:" + digest, lambda: served.post(CHAT, label_request(*asked)))
    assert (status, body) == first


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_scenario_mode_and_the_count_command(tmp_path):
    import pymupdf

    scenario = tmp_path / "scenario.json"

    def write_scenario(**keys) -> None:
        keys["feeds"] = {
            "cs.AI": [["2401.90001", 1, "new"], ["2401.90002", 2, "replace"]],
            "cs.LG": [["2401.90003", 1, "new"], ["2401.90001", 1, "cross"]],
        }
        scenario.write_text(json.dumps(keys), encoding="utf-8")

    write_scenario(withdrawn=["2401.90003"], nopdf=["2401.90002"])
    process = subprocess.Popen([sys.executable, str(STANDIN), "--scenario", str(scenario), "--port", "0"],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=tmp_path)
    try:
        line = process.stdout.readline()
        assert line.startswith("listening on http://127.0.0.1:"), process.stderr.read()
        url = line.split()[-1]
        port = url.rsplit(":", 1)[1]

        def get(path):
            try:
                with urllib.request.urlopen(url + path, timeout=60) as answer:
                    return answer.status, answer.read()
            except urllib.error.HTTPError as e:
                return e.code, e.read()

        def post(body):
            request = urllib.request.Request(url + CHAT, data=json.dumps(body).encode("utf-8"))
            try:
                with urllib.request.urlopen(request, timeout=60) as answer:
                    return answer.status, json.loads(answer.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())

        items = rss_items(get("/rss/cs.AI")[1])
        assert [(i["id"], i["v"], i["kind"]) for i in items] == [("2401.90001", 1, "new"), ("2401.90002", 2, "replace")]
        assert items[1]["title"] == "Paper 2401.90002 (v2)"
        assert items[1]["description"] == "arXiv:2401.90002v2 Announce Type: replace\n" \
                                          "Abstract: Abstract of 2401.90002, version 2."
        assert items[1]["creator"] == "A. Author, B. Author"

        query = urllib.parse.urlencode({"id_list": "2401.90003,2401.90001v1,2401.90009,2401.90002",
                                        "start": 0, "max_results": 100})
        total, entries = atom_entries(get("/api/query?" + query)[1])
        assert total == 3
        common = {"published": "2024-01-01T10:00:00Z", "authors": ["A. Author", "B. Author"]}
        assert entries == [
            dict(common, id="2401.90003", v=1, title="Paper 2401.90003 (v1)",
                 abstract="Abstract of 2401.90003, version 1.", updated="2024-01-01T10:00:00Z",
                 comment="This paper has been withdrawn.", primary="cs.LG", categories=["cs.LG"],
                 pdf=["https://arxiv.org/pdf/2401.90003v1"]),
            dict(common, id="2401.90001", v=1, title="Paper 2401.90001 (v1)",
                 abstract="Abstract of 2401.90001, version 1.", updated="2024-01-01T10:00:00Z",
                 comment=None, primary="cs.AI", categories=["cs.AI", "cs.LG"],
                 pdf=["https://arxiv.org/pdf/2401.90001v1"]),
            dict(common, id="2401.90002", v=2, title="Paper 2401.90002 (v2)",
                 abstract="Abstract of 2401.90002, version 2.", updated="2024-01-02T10:00:00Z",
                 comment=None, primary="cs.AI", categories=["cs.AI"],
                 pdf=["https://arxiv.org/pdf/2401.90002v2"]),
        ]

        status, data = get("/pdf/2401.90001v1")
        assert status == 200
        doc = pymupdf.open(stream=data, filetype="pdf")
        assert [page.get_text() for page in doc] == [
            "Full text of paper 2401.90001v1. It proposes a method and reports results.\n"]
        doc.close()
        assert get("/pdf/2401.90002v2")[0] == 404
        assert get("/pdf/2401.90009v1")[0] == 404

        status, body = post(analysis_request("2401.90001", 1, "Whatever the title says"))
        assert status == 200 and content_of(body) == {
            "summary": "Summary of 2401.90001.", "key_contributions": ["Contribution of 2401.90001"],
            "methodology": "Method.", "limitations": "Limits.", "keywords": ["kw", "2401.90001"],
            "novelty_score": 2,
        }
        assert post(analysis_request("2401.90009", 1)) == (404, SERVER_ERROR)
        write_scenario(bad=["2401.90001"], junk=["2401.90003"])
        assert post(analysis_request("2401.90001", 1)) == (500, SERVER_ERROR)
        assert content_of(post(analysis_request("2401.90003", 1))[1])["keywords"] == "kw, 2401.90003"

        def count(prefix, at):
            return subprocess.run([sys.executable, str(STANDIN), "count", prefix, "--port", str(at)],
                                  capture_output=True, text=True, timeout=60)

        for prefix, expected in (("pdf", "3\n"), ("llm:2401.90001", "2\n")):
            done = count(prefix, port)
            assert (done.returncode, done.stdout, done.stderr) == (0, expected, ""), prefix
        nobody = free_port()
        done = count("api", nobody)
        assert (done.returncode, done.stdout, done.stderr) == (1, "", f"error: no stand-in on port {nobody}\n")
    finally:
        process.terminate()
        process.wait(10)
        process.stdout.close()
        process.stderr.close()
