"""Local stand-in for the outside services ArxivLens talks to, for running things without the internet:

  GET  /rss/<category>                    the arXiv RSS feed of one category
  GET  /api/query?id_list=..&start=..     the arXiv API (Atom), the way the arxiv package asks for it
  GET  /pdf/<id>v<n>                      a one-page PDF
  POST .../chat/completions               an Azure OpenAI style chat completion holding a paper analysis
  GET  /_n                                how often each of the above was asked, as JSON

Usage:  python dev/standin.py SCENARIO.json [PORT]        (port 9100 unless given)

The scenario file is read again on every request, so rewriting it changes what the services answer:

  {"feeds": {"cs.AI": [["2401.00001", 1, "new"], ["2401.00002", 3, "replace"]]},
   "gone":  ["2401.00003"],
   "nopdf": ["2401.00004"],
   "bad":   ["2401.00005"],
   "junk":  ["2401.00006"],
   "slow":  2.5}

  feeds   per category, the announcements in its feed: paper id, its latest version, and the announce type
          arXiv uses (new, cross, replace, replace-cross). Feed links carry no version, as on arXiv;
          the API answers with the latest version found in any feed.
  gone    ids the API has no entry for
  nopdf   ids whose PDF answers 404
  bad     ids whose analysis request answers HTTP 500
  junk    ids whose analysis comes back with keywords as a string instead of a list
  slow    seconds every analysis request takes

Counters in /_n are named rss:<category>, api, pdf:<id> and llm:<id>.
"""
import json
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pymupdf

SCENARIO = sys.argv[1]
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 9100
BASE = f"http://127.0.0.1:{PORT}"
COUNTS: dict[str, int] = {}
LOCK = threading.Lock()


def scenario() -> dict:
    with open(SCENARIO, encoding="utf-8") as f:
        return json.load(f)


def count(key: str) -> None:
    with LOCK:
        COUNTS[key] = COUNTS.get(key, 0) + 1


def latest_versions(sc: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    for items in sc.get("feeds", {}).values():
        for paper_id, version, _kind in items:
            out[paper_id] = max(out.get(paper_id, 0), version)
    return out


def rss(sc: dict, code: str) -> str:
    items = "".join(
        f"<item><title>Paper {pid}</title><link>https://arxiv.org/abs/{pid}</link>"
        f"<description>arXiv:{pid}v{ver} Announce Type: {kind}\nAbstract: Abstract of {pid}.</description>"
        f'<guid isPermaLink="false">oai:arXiv.org:{pid}v{ver}</guid><category>{code}</category>'
        f"<arxiv:announce_type>{kind}</arxiv:announce_type><dc:creator>A. Author, B. Author</dc:creator></item>"
        for pid, ver, kind in sc.get("feeds", {}).get(code, [])
    )
    return (
        "<?xml version='1.0' encoding='UTF-8'?>"
        '<rss xmlns:arxiv="http://arxiv.org/schemas/atom" xmlns:dc="http://purl.org/dc/elements/1.1/" version="2.0">'
        f"<channel><title>{code} updates on arXiv.org</title><link>http://rss.arxiv.org/rss/{code}</link>"
        f"<description>{code} updates on the arXiv.org e-print archive.</description>{items}</channel></rss>"
    )


def atom(sc: dict, id_list: list[str], start: int, size: int) -> str:
    known, gone = latest_versions(sc), set(sc.get("gone", []))
    found = [pid for pid in (re.sub(r"v\d+$", "", raw) for raw in id_list) if pid in known and pid not in gone]
    entries = []
    for pid in found[start:start + size]:
        ver = known[pid]
        cats = [code for code, items in sc.get("feeds", {}).items() if any(i[0] == pid for i in items)]
        entries.append(
            f"<entry><id>http://arxiv.org/abs/{pid}v{ver}</id><title>Paper {pid} (v{ver})</title>"
            f"<updated>2024-01-{min(ver, 28):02d}T10:00:00Z</updated>"
            f'<link href="https://arxiv.org/abs/{pid}v{ver}" rel="alternate" type="text/html"/>'
            f'<link href="{BASE}/pdf/{pid}v{ver}" rel="related" type="application/pdf" title="pdf"/>'
            f"<summary>Abstract of {pid}, version {ver}.</summary>"
            + "".join(f'<category term="{c}" scheme="http://arxiv.org/schemas/atom"/>' for c in cats)
            + f'<published>2024-01-01T10:00:00Z</published><arxiv:primary_category term="{cats[0]}"/>'
            "<author><name>A. Author</name></author><author><name>B. Author</name></author></entry>"
        )
    return (
        "<?xml version='1.0' encoding='UTF-8'?>"
        '<feed xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/" xmlns:arxiv="http://arxiv.org/schemas/atom" '
        'xmlns="http://www.w3.org/2005/Atom"><title>arXiv Query</title><updated>2024-01-01T00:00:00Z</updated>'
        f"<opensearch:itemsPerPage>{size}</opensearch:itemsPerPage>"
        f"<opensearch:totalResults>{len(found)}</opensearch:totalResults>"
        f"<opensearch:startIndex>{start}</opensearch:startIndex>{''.join(entries)}</feed>"
    )


def pdf(name: str) -> bytes:
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), f"Full text of paper {name}. It proposes a method and reports results.")
    data = doc.tobytes()
    doc.close()
    return data


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status: int, body, content_type: str = "application/xml") -> None:
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        path, query, sc = url.path, parse_qs(url.query), scenario()
        if path == "/_n":
            with LOCK:
                return self.reply(200, json.dumps(COUNTS, sort_keys=True), "application/json")
        if path.startswith("/rss/"):
            code = path[len("/rss/"):]
            count(f"rss:{code}")
            return self.reply(200, rss(sc, code), "application/rss+xml")
        if path == "/api/query":
            count("api")
            ids = [i for i in query.get("id_list", [""])[0].split(",") if i]
            start = int(query.get("start", ["0"])[0])
            size = int(query.get("max_results", ["100"])[0])
            return self.reply(200, atom(sc, ids, start, size), "application/atom+xml")
        if path.startswith("/pdf/"):
            name = path[len("/pdf/"):]
            pid = re.sub(r"v\d+$", "", name)
            count(f"pdf:{pid}")
            if pid in sc.get("nopdf", []):
                return self.reply(404, "not found", "text/plain")
            return self.reply(200, pdf(name), "application/pdf")
        return self.reply(404, "not found", "text/plain")

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if not self.path.split("?")[0].endswith("/chat/completions"):
            return self.reply(404, "not found", "text/plain")
        body = json.loads(raw or b"{}")
        text = " ".join(str(m.get("content", "")) for m in body.get("messages", []))
        found = re.search(r"Paper title: Paper (\d{4}\.\d{4,5})", text)
        pid = found.group(1) if found else "unknown"
        count(f"llm:{pid}")
        sc = scenario()
        time.sleep(float(sc.get("slow", 0)))
        if pid in sc.get("bad", []):
            error = {"error": {"message": "stand-in failure", "type": "server_error"}}
            return self.reply(500, json.dumps(error), "application/json")
        analysis = {
            "summary": f"Summary of {pid}.",
            "key_contributions": [f"Contribution of {pid}"],
            "methodology": "Method.",
            "limitations": "Limits.",
            "keywords": "kw" if pid in sc.get("junk", []) else ["kw", pid],
            "novelty_score": 1 + int(pid[-1]) % 10,
        }
        completion = {
            "id": "chatcmpl-standin",
            "object": "chat.completion",
            "created": 0,
            "model": body.get("model", "standin"),
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": json.dumps(analysis)}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        return self.reply(200, json.dumps(completion), "application/json")


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
