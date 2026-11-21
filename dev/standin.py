"""Stand-in for arXiv's feeds, arXiv's API, the PDF host and the model endpoint, for running offline.

    python dev/standin.py [--port N] [--corpus DIR | --scenario FILE] [--home DIR] [--control FILE]
    python dev/standin.py count PREFIX [--port N]

It answers from a corpus folder for the day of the home's clock, or from a scenario file read at
every request; a control file adds faults. World holds the answering logic, also used in-process.
"""
import argparse
import gzip
import hashlib
import json
import os
import re
import sys
import threading
import time
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import urlopen
from xml.sax.saxutils import escape

DEFAULT_PORT = 9100
FAULT_KEYS = ("gone", "nopdf", "bad", "junk")

_CLOCK_LINE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_VERSION_SUFFIX = re.compile(r"v[0-9]+$")
_PDF_NAME = re.compile(r"(.+)v([0-9]+)")
_PAPER_LINE = re.compile(r"^arXiv: (\S+?)v(\d+)$", re.MULTILINE)
_SERVER_ERROR = {"error": {"message": "stand-in failure", "type": "server_error"}}


class Unavailable(Exception):
    """The service answers with an error status instead of a result."""

    def __init__(self, status: int):
        super().__init__(f"answered with status {status}")
        self.status = status


def render_pdf(pages: list[list[str]]) -> bytes:
    """A PDF with one page per list of lines, drawn in one call per page."""
    import pymupdf

    doc = pymupdf.open()
    for lines in pages:
        page = doc.new_page(width=612, height=792)
        if lines:
            # One call per page: inserting line by line is about fifteen times slower.
            page.insert_text((54, 60), "\n".join(lines), fontname="cour", fontsize=10, lineheight=1.2)
    data = doc.tobytes()
    doc.close()
    return data


def expected_text(pages: list[list[str]]) -> str:
    """The text the application stores for a PDF made by render_pdf from pages that hold text."""
    return "\n\n".join("\n".join(lines) + "\n" for lines in pages)


def clock_day(home) -> date | None:
    """The day of the instant in <home>/clock, or None when there is no readable clock file."""
    try:
        text = (Path(home) / "clock").read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError):
        return None
    match = _CLOCK_LINE.fullmatch(text.strip())
    if match is None:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _system_day() -> date:
    return datetime.now(timezone.utc).date()


def strip_version(arxiv_id: str) -> str:
    return _VERSION_SUFFIX.sub("", arxiv_id)


def prompt_text(messages) -> str:
    """The content strings of all messages, joined by newlines."""
    parts = []
    for message in messages if isinstance(messages, list) else []:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            parts.append(content)
    return "\n".join(parts)


def prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]


def paper_of(messages) -> tuple[str, int] | None:
    """The paper an analysis request is about: its 'arXiv: <id>v<n>' line."""
    for message in messages if isinstance(messages, list) else []:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            match = _PAPER_LINE.search(content)
            if match:
                return match.group(1), int(match.group(2))
    return None


def topic_name(prompt: str) -> dict:
    """A name and a blurb that depend on the prompt alone."""
    terms: list[str] = []
    for line in prompt.split("\n"):
        if line.startswith("Terms:"):
            terms = [term.strip() for term in line[len("Terms:"):].split(",")]
            terms = [term for term in terms if term]
            break
    if not terms:
        return {"name": "Topic " + prompt_hash(prompt)[:6], "blurb": "No terms given."}
    name = " and ".join(term[:1].upper() + term[1:] for term in terms[:2])
    shown = terms[:3]
    listed = shown[0] if len(shown) == 1 else ", ".join(shown[:-1]) + " and " + shown[-1]
    return {"name": name, "blurb": f"Papers about {listed}."}


def _seconds(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return max(float(value), 0.0)


class World:
    """What the four services answer today, from a corpus, a scenario file, or nothing."""

    def __init__(self, corpus=None, scenario=None, control=None, today=None):
        if corpus is not None and scenario is not None:
            raise ValueError("a corpus or a scenario, not both")
        self.feeds: list[str] = []
        self._corpus = Path(corpus) if corpus is not None else None
        self._scenario = Path(scenario) if scenario is not None else None
        self._control = Path(control) if control is not None else None
        self._today = today or _system_day
        self._lock = threading.Lock()
        self._papers: dict[str, dict] = {}
        self._days: dict[str, dict] = {}
        self._control_seen = None
        self._api_requests = 0
        self._label_budget = 0
        if self._corpus is not None:
            self._load_corpus()

    def announcements(self, feed) -> list[tuple[str, int, str]]:
        control = self._read_control()
        if feed in _listed(control, "feed_fail"):
            raise Unavailable(500)
        if self._corpus is not None:
            items = self._day_file()["feeds"].get(feed, [])
        elif self._scenario is not None:
            items = (self._read_scenario().get("feeds") or {}).get(feed, [])
        else:
            items = []
        return [(str(arxiv_id), int(version), str(kind)) for arxiv_id, version, kind in items]

    def entries(self, ids) -> list[dict]:
        """API entries for the ids known today, in request order."""
        control = self._read_control()
        with self._lock:
            self._api_requests += 1
            number = self._api_requests
        fail_from = control.get("api_fail_from")
        if isinstance(fail_from, int) and not isinstance(fail_from, bool) and number >= fail_from:
            raise Unavailable(500)
        wanted = [strip_version(raw) for raw in ids if raw]
        if self._corpus is not None:
            day = self._today().isoformat()
            gone = self._faults("gone", control)
            found = []
            for arxiv_id in wanted:
                paper = self._papers.get(arxiv_id)
                if paper is None or arxiv_id in gone:
                    continue
                current = None
                for version in paper["versions"]:
                    if version["day"] <= day:
                        current = version
                if current is not None:
                    found.append(self._corpus_entry(paper, current, day))
            return found
        if self._scenario is not None:
            data = self._read_scenario()
            latest, feeds = _scenario_index(data)
            gone = self._faults("gone", control, data)
            return [
                _scenario_entry(arxiv_id, latest[arxiv_id], feeds[arxiv_id], data)
                for arxiv_id in wanted
                if arxiv_id in latest and arxiv_id not in gone
            ]
        return []

    def version(self, arxiv_id, version) -> dict | None:
        """The entry of one given version, whatever the day; what a feed item shows of it."""
        if self._corpus is not None:
            paper = self._papers.get(arxiv_id)
            for item in paper["versions"] if paper else []:
                if item["v"] == version:
                    return self._corpus_entry(paper, item, self._today().isoformat())
            return None
        if self._scenario is not None:
            data = self._read_scenario()
            latest, feeds = _scenario_index(data)
            if arxiv_id not in latest:
                return None
            return _scenario_entry(arxiv_id, version, feeds[arxiv_id], data)
        return None

    def pdf(self, arxiv_id, version) -> bytes | None:
        """The PDF of a version; None means 404."""
        control = self._read_control()
        if self._corpus is not None:
            item = self._corpus_version(arxiv_id, version)
            if item is None or item["pages"] is None or arxiv_id in self._faults("nopdf", control):
                return None
            return render_pdf(item["pages"])
        if self._scenario is not None:
            data = self._read_scenario()
            latest, _feeds = _scenario_index(data)
            if arxiv_id not in latest or arxiv_id in self._faults("nopdf", control, data):
                return None
            return render_pdf([[f"Full text of paper {arxiv_id}v{version}. It proposes a method and reports results."]])
        return None

    def analysis(self, arxiv_id, version) -> dict:
        """The model's answer for a version; raises Unavailable(status) when it fails."""
        control = self._read_control()
        data = self._read_scenario() if self._scenario is not None else {}
        wait = _seconds(control["slow"]) if "slow" in control else _seconds(data.get("slow"))
        if wait:
            time.sleep(wait)
        if self._corpus is not None:
            item = self._corpus_version(arxiv_id, version)
            if item is None or item.get("analysis") is None:
                raise Unavailable(404)
            # A copy: whoever receives the answer may change it, the corpus stays as loaded.
            answer = json.loads(json.dumps(item["analysis"]))
        elif self._scenario is not None:
            latest, _feeds = _scenario_index(data)
            if arxiv_id not in latest:
                raise Unavailable(404)
            answer = {
                "summary": f"Summary of {arxiv_id}.",
                "key_contributions": [f"Contribution of {arxiv_id}"],
                "methodology": "Method.",
                "limitations": "Limits.",
                "keywords": ["kw", arxiv_id],
                "novelty_score": 1 + (int(arxiv_id[-1]) if arxiv_id[-1:].isdigit() else 0) % 10,
            }
        else:
            raise Unavailable(404)
        if arxiv_id in self._faults("bad", control, data):
            raise Unavailable(500)
        if arxiv_id in self._faults("junk", control, data):
            answer["keywords"] = ", ".join(answer["keywords"])
        return answer

    def label(self, messages) -> dict:
        """A topic name for a prompt; raises Unavailable(500) while the failure budget lasts."""
        control = self._read_control()
        wait = _seconds(control.get("label_slow"))
        if wait:
            time.sleep(wait)
        with self._lock:
            if self._label_budget > 0:
                self._label_budget -= 1
                raise Unavailable(500)
        return topic_name(prompt_text(messages))

    def _load_corpus(self) -> None:
        manifest_file = self._corpus / "corpus.json"
        if not manifest_file.is_file():
            raise ValueError(f"corpus not found: {self._corpus}")
        manifest = json.loads(manifest_file.read_bytes())
        self.feeds = list(manifest["feeds"])
        with gzip.open(self._corpus / "papers.jsonl.gz", "rb") as f:
            for line in f:
                if line.strip():
                    paper = json.loads(line)
                    self._papers[paper["id"]] = paper

    def _day_file(self) -> dict:
        day = self._today().isoformat()
        with self._lock:
            cached = self._days.get(day)
        if cached is not None:
            return cached
        path = self._corpus / "days" / f"{day}.json"
        # Outside the corpus range a day is one without announcements and without faults.
        loaded = json.loads(path.read_bytes()) if path.is_file() else {}
        loaded.setdefault("feeds", {})
        for key in FAULT_KEYS:
            loaded.setdefault(key, [])
        with self._lock:
            self._days[day] = loaded
        return loaded

    def _corpus_version(self, arxiv_id, version) -> dict | None:
        paper = self._papers.get(arxiv_id)
        for item in paper["versions"] if paper else []:
            if item["v"] == version:
                return item
        return None

    def _corpus_entry(self, paper: dict, item: dict, day: str) -> dict:
        categories = list(item["categories"])
        for later in paper.get("cross_later") or []:
            if later["day"] <= day and later["feed"] not in categories:
                categories.append(later["feed"])
        return {
            "id": paper["id"],
            "v": item["v"],
            "published": paper["published"],
            "updated": item["updated"],
            "title": item["title"],
            "abstract": item["abstract"],
            "authors": list(paper["authors"]),
            "comment": item.get("comment"),
            "journal_ref": item.get("journal_ref"),
            "doi": item.get("doi"),
            "primary": paper["primary"],
            "categories": categories,
        }

    def _faults(self, key: str, control: dict, scenario: dict | None = None) -> set[str]:
        ids = set(_listed(control, key))
        if self._corpus is not None:
            ids.update(self._day_file()[key])
        elif scenario:
            ids.update(_listed(scenario, key))
        return ids

    def _read_scenario(self) -> dict:
        data = json.loads(self._scenario.read_bytes())
        return data if isinstance(data, dict) else {}

    def _read_control(self) -> dict:
        if self._control is None:
            return {}
        try:
            info = os.stat(self._control)
            with open(self._control, "rb") as f:
                raw = f.read()
            seen = (info.st_ino, info.st_mtime_ns, raw)
        except OSError:
            raw, seen = b"", None
        try:
            data = json.loads(raw) if raw.strip() else {}
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        with self._lock:
            if seen != self._control_seen:
                # A changed file starts the API count again and gives the topic names a new budget.
                self._control_seen = seen
                self._api_requests = 0
                budget = data.get("label_fail")
                ok = isinstance(budget, int) and not isinstance(budget, bool) and budget > 0
                self._label_budget = budget if ok else 0
        return data


def _listed(data: dict, key: str) -> list:
    value = data.get(key)
    return value if isinstance(value, list) else []


def _scenario_index(data: dict) -> tuple[dict[str, int], dict[str, list[str]]]:
    """Highest listed version of every id, and the feeds that list it, in file order."""
    latest: dict[str, int] = {}
    feeds: dict[str, list[str]] = {}
    for feed, items in (data.get("feeds") or {}).items():
        for arxiv_id, version, _kind in items:
            latest[arxiv_id] = max(latest.get(arxiv_id, 0), int(version))
            listed = feeds.setdefault(arxiv_id, [])
            if feed not in listed:
                listed.append(feed)
    return latest, feeds


def _scenario_entry(arxiv_id: str, version: int, feeds: list[str], data: dict) -> dict:
    withdrawn = arxiv_id in _listed(data, "withdrawn")
    return {
        "id": arxiv_id,
        "v": version,
        "published": "2024-01-01T10:00:00Z",
        "updated": "2024-01-%02dT10:00:00Z" % min(version, 28),
        "title": f"Paper {arxiv_id} (v{version})",
        "abstract": f"Abstract of {arxiv_id}, version {version}.",
        "authors": ["A. Author", "B. Author"],
        "comment": "This paper has been withdrawn." if withdrawn else None,
        "journal_ref": None,
        "doi": None,
        "primary": feeds[0],
        "categories": list(feeds),
    }


def _attr(text) -> str:
    return escape(str(text), {'"': "&quot;"})


def rss(world: World, feed: str, items: list[tuple[str, int, str]]) -> str:
    parts = []
    for arxiv_id, version, kind in items:
        entry = world.version(arxiv_id, version) or {"title": "", "abstract": "", "authors": []}
        parts.append(
            f"<item><title>{escape(entry['title'])}</title>"
            f"<link>https://arxiv.org/abs/{escape(arxiv_id)}</link>"
            f"<description>arXiv:{escape(arxiv_id)}v{version} Announce Type: {escape(kind)}\n"
            f"Abstract: {escape(entry['abstract'])}</description>"
            f'<guid isPermaLink="false">oai:arXiv.org:{escape(arxiv_id)}v{version}</guid>'
            f"<category>{escape(feed)}</category>"
            f"<arxiv:announce_type>{escape(kind)}</arxiv:announce_type>"
            f"<dc:creator>{escape(', '.join(entry['authors']))}</dc:creator></item>"
        )
    code = escape(feed)
    return (
        "<?xml version='1.0' encoding='UTF-8'?>"
        '<rss xmlns:arxiv="http://arxiv.org/schemas/atom" xmlns:dc="http://purl.org/dc/elements/1.1/" version="2.0">'
        f"<channel><title>{code} updates on arXiv.org</title><link>http://rss.arxiv.org/rss/{code}</link>"
        f"<description>{code} updates on the arXiv.org e-print archive.</description>{''.join(parts)}</channel></rss>"
    )


def atom(known: list[dict], start: int, size: int) -> str:
    parts = []
    for entry in known[start:start + size]:
        name = f"{entry['id']}v{entry['v']}"
        text = [
            f"<entry><id>http://arxiv.org/abs/{escape(name)}</id>",
            f"<updated>{escape(entry['updated'])}</updated><published>{escape(entry['published'])}</published>",
            f"<title>{escape(entry['title'])}</title><summary>{escape(entry['abstract'])}</summary>",
        ]
        text.extend(f"<author><name>{escape(author)}</name></author>" for author in entry["authors"])
        for key in ("comment", "journal_ref", "doi"):
            if entry[key] is not None:
                text.append(f"<arxiv:{key}>{escape(entry[key])}</arxiv:{key}>")
        text.append(f'<link href="https://arxiv.org/abs/{_attr(name)}" rel="alternate" type="text/html"/>')
        text.append(f'<link href="https://arxiv.org/pdf/{_attr(name)}" rel="related" type="application/pdf" title="pdf"/>')
        text.append(f'<arxiv:primary_category term="{_attr(entry["primary"])}"/>')
        text.extend(
            f'<category term="{_attr(code)}" scheme="http://arxiv.org/schemas/atom"/>' for code in entry["categories"]
        )
        text.append("</entry>")
        parts.append("".join(text))
    return (
        "<?xml version='1.0' encoding='UTF-8'?>"
        '<feed xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/" xmlns:arxiv="http://arxiv.org/schemas/atom" '
        'xmlns="http://www.w3.org/2005/Atom"><title>arXiv Query</title><updated>2024-01-01T00:00:00Z</updated>'
        f"<opensearch:itemsPerPage>{size}</opensearch:itemsPerPage>"
        f"<opensearch:totalResults>{len(known)}</opensearch:totalResults>"
        f"<opensearch:startIndex>{start}</opensearch:startIndex>{''.join(parts)}</feed>"
    )


def completion(content: str, model: str) -> dict:
    return {
        "id": "chatcmpl-standin",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _reply(self, status: int, body, content_type: str = "text/plain; charset=utf-8") -> None:
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status: int, value) -> None:
        self._reply(status, json.dumps(value, sort_keys=True), "application/json")

    def do_GET(self):
        url = urlparse(self.path)
        path = unquote(url.path)
        server, world = self.server, self.server.world
        if path == "/_n":
            return self._json(200, server.counts())
        if path.startswith("/rss/"):
            feed = path[len("/rss/"):]
            server.count(f"rss:{feed}")
            try:
                items = world.announcements(feed)
            except Unavailable as e:
                return self._reply(e.status, "feed failure")
            return self._reply(200, rss(world, feed, items), "application/rss+xml; charset=utf-8")
        if path == "/api/query":
            server.count("api")
            query = parse_qs(url.query)
            ids = [part for part in query.get("id_list", [""])[0].split(",") if part]
            try:
                start = int(query.get("start", ["0"])[0])
                size = int(query.get("max_results", ["100"])[0])
            except ValueError:
                return self._reply(400, "bad paging")
            try:
                known = world.entries(ids)
            except Unavailable as e:
                return self._reply(e.status, "api failure")
            return self._reply(200, atom(known, start, size), "application/atom+xml; charset=utf-8")
        if path.startswith("/pdf/"):
            match = _PDF_NAME.fullmatch(path[len("/pdf/"):])
            if match:
                arxiv_id = match.group(1)
                server.count(f"pdf:{arxiv_id}")
                data = world.pdf(arxiv_id, int(match.group(2)))
                if data is None:
                    return self._reply(404, "not found")
                return self._reply(200, data, "application/pdf")
        return self._reply(404, "not found")

    def do_POST(self):
        url = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        if not url.path.endswith("/chat/completions"):
            return self._reply(404, "not found")
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return self._json(400, {"error": {"message": "the body is not a JSON object", "type": "invalid_request_error"}})
        server, world = self.server, self.server.world
        messages = body.get("messages") or []
        model = str(body.get("model") or "standin")
        paper = paper_of(messages)
        if paper is not None:
            arxiv_id, version = paper
            server.count(f"llm:{arxiv_id}")
            try:
                answer = world.analysis(arxiv_id, version)
            except Unavailable as e:
                return self._json(e.status, _SERVER_ERROR)
            return self._json(200, completion(json.dumps(answer), model))
        if "tools" in body or body.get("stream") is True:
            return self._json(400, {"error": {"message": "the stand-in answers analyses and topic names only",
                                              "type": "invalid_request_error"}})
        server.count(f"label:{prompt_hash(prompt_text(messages))}")
        try:
            answer = world.label(messages)
        except Unavailable as e:
            return self._json(e.status, _SERVER_ERROR)
        content = json.dumps(answer, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return self._json(200, completion(content, model))


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, world: World):
        super().__init__(address, _Handler)
        self.world = world
        self._counts: dict[str, int] = {}
        self._counts_lock = threading.Lock()

    def count(self, key: str) -> None:
        with self._counts_lock:
            self._counts[key] = self._counts.get(key, 0) + 1

    def counts(self) -> dict[str, int]:
        with self._counts_lock:
            return dict(self._counts)


def make_server(port, corpus=None, scenario=None, home=None, control=None) -> ThreadingHTTPServer:
    """The world and a server bound to 127.0.0.1:<port>, not serving yet (port 0: any free port)."""
    today = None
    if home is not None:
        def today():
            return clock_day(home) or _system_day()
    world = World(corpus=corpus, scenario=scenario, control=control, today=today)
    return _Server(("127.0.0.1", port), world)


def _count(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="standin.py count")
    parser.add_argument("prefix")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    try:
        with urlopen(f"http://127.0.0.1:{args.port}/_n", timeout=10) as answer:
            counts = json.loads(answer.read())
    except (OSError, ValueError):
        print(f"error: no stand-in on port {args.port}", file=sys.stderr)
        return 1
    print(sum(value for key, value in counts.items() if key.startswith(args.prefix)))  # ints
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["count"]:
        return _count(argv[1:])
    parser = argparse.ArgumentParser(prog="standin.py")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--corpus")
    source.add_argument("--scenario")
    parser.add_argument("--home", default=os.environ.get("ARXIVLENS_HOME") or None)
    parser.add_argument("--control")
    args = parser.parse_args(argv)
    try:
        server = make_server(args.port, corpus=args.corpus, scenario=args.scenario, home=args.home,
                             control=args.control)
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"listening on http://127.0.0.1:{server.server_address[1]}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
