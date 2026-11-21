# ArxivLens: design

ArxivLens is one Python process (FastAPI with an in-process scheduler) and a command line over the
same code. A cycle reads the arXiv feeds, the arXiv API, the PDFs and a model endpoint and stores
papers, versions, texts and analyses. An insights build turns the stored library into one folder of
files, and the readers (report, tools, REST, pages, chat, MCP) work from that folder. Everything a
running instance keeps lives in a home folder, and every piece of time comes from one clock.

## Parts

```text
 arXiv feeds    arXiv API    PDF host    model endpoint
      \             |            |             /
       +------------+-----+------+------------+
                          |  sources: HTTP, or a corpus in-process
                          v
                  cycle: poll, then process
                          |
             +------------+------------+
             v                         v
     database (lens.db or      text store (texts/, one
     PostgreSQL)               gzip file per version)
             |                         |
             +------------+------------+
                          v
          insights build: ten stages, one folder (insights/)
                          |
   +--------+--------+----+----+--------+--------+
   v        v        v         v        v        v
 report   tools    REST      pages    chat      MCP
```

## A cycle

`run_cycle()` is the only code that runs a cycle; the scheduler, `POST /process` and
`python -m app cycle` all reach it. It closes runs a dead process left open, records a run, seeds
the categories it has not seen, polls every feed in order of category id, then processes the papers
that wait, one at a time.

Polling links every stored paper a feed lists to that feed and stores new papers in batches of 100
API ids. A `replace` announcement with a higher version moves the paper to it in one guarded update:
the paper is pending again with no tries counted, and its old analysis and text stay until the new
version has been analysed. Each feed's counts go into its own `run_feeds` row; a feed that fails
does not stop the others.

Processing claims a paper with a token and a lease stamped by the clock. The worker renews the
lease while it works; a lease not renewed for `PROCESSING_LEASE_SECONDS` can be taken over. The text
is stored before the model is asked, so a try that fails at the model does not fetch the PDF again.
An analysis is stored only for the version the worker claimed. Every try counts; after three failed
tries a paper waits for a retry by hand. A withdrawn paper is never claimed.

The timeline records six kinds of entries: `announced`, `replaced`, `withdrawn`, `analysed`,
`failed`, `retried`. Each is written in the transaction of the change it describes.

## Data

| Table | One row per |
|---|---|
| `categories` | subscribed feed (arXiv code, name) |
| `papers` | paper: current version, metadata, processing state, tries, lease, withdrawal |
| `paper_categories` | link between a paper and a feed that listed it |
| `paper_versions` | version the tracker stored: title, abstract, authors, categories, when seen |
| `paper_events` | timeline entry: kind, version, feed, time |
| `paper_texts` | paper: version, hash, length and source of its stored text |
| `analyses` | paper: the analysis and the version it describes |
| `runs` | cycle: start, last sign of life, end, totals, error |
| `run_feeds` | feed of a cycle: listed, new, updated, skipped, error |
| `seeded_categories` | category code subscribed once from the seed list |
| `conversations`, `messages` | chat history |
| `schema_version` | schema step applied |

A database of any earlier generation is upgraded in place by recorded steps (`app/db/migrate.py`).
Each step runs in one transaction and is written against what it finds, so a step that is
interrupted leaves the database as it was and the next start applies it again.

The text store is a flat folder of gzip files, `texts/<id>v<version>.txt.gz`. A text is written
first, then its row, then the file of the version it replaces is removed; a process that dies in
between leaves at worst a file without a row.

## Time and place

The home (`ARXIVLENS_HOME`) holds the database, the clock file, the corpus marker, the texts, the
result folder, the settings file and the report. Nothing in it names its own path, so a copy works
as the original.

`app/clock.py` is the only module that reads the time. It returns the instant in the home's clock
file, else in `ARXIVLENS_CLOCK`, else the system time. The clock file is read at every call, so
several processes and a copied home share one simulated time, and it does not tick. Leases, run
stamps, the 24-hour digest, version and timeline times, and the as-of day of a build all follow it.
A test never waits for time to pass: it sets or advances the clock. What stays on real time: the
scheduler, the pause between lease renewals, HTTP time-outs, the pause between API requests, the
slow switch and log stamps.

## Insights

A build runs ten stages in a fixed order. Stage 1 reads the database in one read transaction; after
that no stage opens it. Each file is written as soon as it is computed.

| # | Stage | Files | What it does |
|---|---|---|---|
| 1 | snapshot | `papers.jsonl` | one row per stored paper, with its state decided from stored facts |
| 2 | text | `text/tokens.jsonl`, `text/refs.jsonl` | stemmed terms and term pairs; the arXiv ids of each reference list |
| 3 | vocab | `vocab.json` | the terms that count and their idf, fitted on the reference papers |
| 4 | vectors | `vectors.jsonl` | a TF-IDF vector per paper |
| 5 | similar | `similar.jsonl` | neighbour lists, searched through each owner's heaviest terms |
| 6 | citations | `citations/*.jsonl` | edges, external ids, co-citation, coupling, connected parts, PageRank |
| 7 | topics | `centroids.json`, `assignments.jsonl`, `topics.json` | topics fitted on the reference papers, papers put with topics, names from the model |
| 8 | trends | `trends/<feed>.json`, `authors.json` | weekly counts over closed weeks, rising keywords, bursts, topic momentum, authors |
| 9 | ranking | `ranking.jsonl` | each feed's papers of an announced week, ranked |
| 10 | digests | `digests/<feed>/<week>.md` | one page per ranked week |

The reference papers are those first stored before the month began, read with the titles and
abstracts they had when first stored. A week is ranked with what was announced by its Sunday. Both
are chosen so that what a reader has seen holds still: scores, topics and topic names do not move
within a month, whatever is announced or replaced, and the digest of a past week is not rewritten
when later papers cite its members.

Two builds of the same state write the same bytes, on every Python from 3.10 to 3.14:

- floats are added in loops in a stated order, never with `sum()`;
- logarithms are taken of whole numbers only, through `decimal`, so no math library enters a file;
- every ordering ends in a unique key (an id, a term, a topic number);
- JSON is written in one canonical form, sorted keys, compact, UTF-8;
- no file but `runs.jsonl` holds a time, a path or a duration.

`stats.json` names every file the build wrote with its size and hash, and `insights check` compares
the folder with it.

## Reading the results

Each reader loads the files its own way, because each has its own reason to read them. The web
application (REST, pages, chat and MCP tools) goes through `app/insights/store.py`, which caches
each file by its modification time and size and reads large files through an index of byte offsets,
since it serves many small requests from a long-running process. The report parses every file
itself and recounts from the database what the tables say, so it must not share the code it is
auditing. `tools/weekly_mail.py` uses the standard library only, because it runs on a machine that
has a copy of the folder and not the application. `insights show`, `insights check` and
`insights diff` read files directly too.

## The offline world

`dev/make_corpus.py` writes a synthetic corpus from a seed: per day the announcements of each feed,
what the API answers, the text of each PDF, the analysis the model gives, and which requests fail.
The same arguments give the same content on every Python. `dev/corpus` and `dev/corpus_small` are
two such corpora kept in the repository; they hold planted cases (replacements, withdrawals, late
citations, twins, odd reference lists) that the tests look for by id.

`dev/standin.py` answers from a corpus over HTTP as arXiv's feeds, the API, the PDF host and the
model endpoint, for the day of the home's clock, and counts every request. Without a corpus it
still names topics. `python -m app play` feeds a corpus into a home day by day: it sets the clock to
08:00 of each day and runs one cycle, talking HTTP to a stand-in, or with `--fast` reading the corpus
in-process through the same answering code. Both give the same database.

The failure switches of `app/faults.py` are test tools. `ARXIVLENS_KILL_AT=fs:N` ends the process
before its N-th file-system change, `commit:N` before its N-th database commit, as `kill -9` would;
`ARXIVLENS_SLOW` makes in-process analyses wait. The tests use them to show that a cycle or an
upgrade stopped anywhere leaves a state the next run finishes from.

## Choices

- **Full text over abstracts.** An abstract is a few hundred words; the method, the results and
  the limitations are in the body. The cost is a PDF download and extraction per paper.
- **PyMuPDF.** Pure Python from pip, fast, good enough on arXiv PDFs; no extra service to run.
- **RSS plus the API.** One request per feed finds the day's announcements; the API gives complete
  metadata for up to 100 papers per request.
- **One process with APScheduler.** Cycles run in the web process; leases in the database keep
  several processes on one database from doing a paper twice.
- **SQLite in a home.** A local run needs no server, and a home can be copied whole; PostgreSQL
  stays for production through `DATABASE_URL`.
- **Pure Python for the pipeline.** No numeric library; dicts and lists are fast enough for a
  library of a few thousand papers and give the same bytes everywhere.
- **Azure OpenAI for every model call.** Analyses, chat and topic names use one endpoint and one
  deployment.
- **No login.** It is a tool for one person's reading.
