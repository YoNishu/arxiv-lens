# ArxivLens

ArxivLens tracks the arXiv categories you subscribe to, stores every paper with its versions, and has
a model analyse the full text of each one. From the library it derives similar papers, a citation
graph, named topics, trends and weekly digests.

## What it does

- Polls the feeds of the subscribed categories and stores each paper once, linked to every feed.
- Has Azure OpenAI (`gpt-4o`) analyse the full text of each PDF: summary, contributions, method,
  limitations, keywords and a novelty score from 1 to 10.
- Keeps every version it has seen and a timeline per paper; a replaced paper is analysed again.
- Builds insights into one folder: similar papers, citations, topics named by the model, trends,
  weekly digests.
- Shows it on web pages and over REST, and answers through a chat with tools and an MCP server.

## Try it offline

Nothing here needs arXiv, Azure or PostgreSQL. The database is a SQLite file in a home folder, and a
corpus stands in for several weeks of arXiv. The two bundled corpora are synthetic, papers, authors
and ids alike: `dev/corpus_small` (231 papers, three feeds, five weeks from 2026-08-10) and
`dev/corpus` (1,458 papers, five feeds, twelve weeks from 2026-08-03).
`python dev/make_corpus.py --seed N --out DIR` makes more of them.

Install into a virtual environment:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt -c constraints.txt
```

Start the stand-in. It plays arXiv and the model endpoint; here it only has to name topics, and it
counts the requests it gets:

```bash
python dev/standin.py --control /tmp/ctl &
```

Choose a home folder and point the model settings at the stand-in:

```bash
export ARXIVLENS_HOME=/tmp/lens AZURE_OPENAI_ENDPOINT=http://127.0.0.1:9100 AZURE_OPENAI_API_KEY=x
```

Play the small corpus up to Friday 2026-09-04. Each day sets the home's clock to 08:00 of that day
and runs one cycle, which prints one line, from `2026-08-10 run 1: new=6 updated=0 skipped=0
processed=6 failed=0` to the line of run 26:

```bash
python -m app play --corpus dev/corpus_small --fast --until 2026-09-04
```

Build the insights. It prints `built 30 files: 184 papers, 9 topics, as of 2026-09-04`:

```bash
python -m app insights build
```

List the topics, one line each: number, size, papers of the last four weeks, who named it
(`model`), name:

```bash
python -m app insights show topics
```

Write the library report into `$ARXIVLENS_HOME/report`. It prints `audit: 53 recounts, 0 differ`:
every number of the report that can be counted a second way was, and none differs:

```bash
python -m app insights report
```

Show what is related to one paper: its neighbours, the papers it cites, the papers that cite it,
ending with `cited by 2`:

```bash
python tools/related.py 2608.90033
```

Start the web application and open http://127.0.0.1:8000/ui/topics:

```bash
uvicorn app.main:app
```

## The home folder

Everything a running ArxivLens keeps lives in one folder, named by `ARXIVLENS_HOME` (default: `data`
under the working directory). It is created on demand.

```text
lens.db          the SQLite database
clock            the simulated time, one instant (optional)
corpus           the corpus that play used last, so later cycles use it too (optional)
texts/           the text of each paper version, one gzip file each
insights/        the result folder of insights build
insights.yaml    settings that override those of the project (optional)
report/          report.md and audit.txt of insights report
```

A home can be copied or moved as a whole (`cp -a`): no file in it names its own path, and the copy
goes on where the original stopped. The clock file wins over `ARXIVLENS_CLOCK`, which wins over the
system time; without either, time is real. With `DATABASE_URL` set (PostgreSQL in production) there
is no `lens.db`, and the rest of the home stays as it is.

## Commands

`python -m app [-v] <command>`, on the home of `ARXIVLENS_HOME`. Results go to stdout, one plain
line each; logs go to stderr (`-v` for more of them).

| Command | What it does |
|---|---|
| `db upgrade` | applies the schema steps the database is missing; prints `step <n> <name>` per step, then `schema <n>` |
| `db info` | `schema=<n>`, then `<table>=<rows>` for every table |
| `clock show` | the instant the home runs on and its source: `file`, `env` or `system` |
| `clock set INSTANT` | sets the home's clock (`2026-09-10`, `2026-09-10T08:00Z`, `2026-09-10T08:00:00Z`) |
| `clock advance DURATION` | moves the clock on (`90s`, `15m`, `2h`, `3d`); needs a clock file |
| `cycle` | polls every feed, then processes; prints `run <id>: new=<n> updated=<n> skipped=<n> processed=<n> failed=<n>` |
| `play --corpus DIR (--until DAY [--from DAY] \| --day DAY) [--fast] [--then "ARGS"]` | for each day: clock to 08:00, one cycle, then the command of `--then`; `--fast` answers from the corpus in-process, without it the cycle talks HTTP to a stand-in started with `--corpus DIR --home <home>` |
| `category add CODE [--name NAME]` | subscribes to a category (`cs.AI`, `stat.ML`, `hep-th`, `astro-ph.CO`) |
| `category remove CODE` | unsubscribes; papers no other feed lists are deleted with their texts |
| `category list` | `<id>`, `<code>`, linked papers, `<name>`, tab-separated |
| `paper show ARXIV_ID` | the stored fields of a paper, then its timeline |
| `paper retry ARXIV_ID` | makes a failed paper pending again with no tries counted |
| `tool [--mcp] [--url URL] NAME [JSON]` | calls a chat tool, or with `--mcp` an MCP tool (with `--url` on a running server), and prints its text |
| `insights build [--out OUT]` | builds the result folder (the home's `insights/`, or `OUT`) |
| `insights show work [--out OUT]` | the work counters of the last build, `name=value` |
| `insights show topics [--out OUT]` | `<topic>`, `<size>`, `<recent>`, `<source>`, `<name>` per topic |
| `insights show trends FEED [--out OUT]` | the rising terms and the bursts of a feed |
| `insights show paper ARXIV_ID [--out OUT]` | a paper's state, feeds, topic, citation counts and neighbours |
| `insights config` | the settings in force, as YAML |
| `insights check [--out OUT]` | compares the files of a result folder with the sizes and hashes in its `stats.json` |
| `insights diff A B` | which files and how many units per stage differ between two result folders |
| `insights report [--out OUT] [--to DIR]` | writes `report.md` and `audit.txt` (default: the home's `report/`) |

Exit statuses: 0 done; 1 the answer is no (no such paper, not subscribed, the cycle recorded an
error, a result file is changed or missing, the audit found a difference); 2 a usage or environment
error (bad arguments, a malformed day or instant, no clock file to advance, a corpus that cannot be
read, a database newer than the code). A failure prints one `error: <text>` line on stderr.

## Insights

`python -m app insights build` reads the database, the clock and the settings, and writes one folder.
Two builds of the same state write the same bytes in every file but `runs.jsonl`. `docs/insights.md`
says what every value in it is.

| File | Holds |
|---|---|
| `papers.jsonl` | one row per stored paper: metadata, feeds, state, keywords, first stored title and abstract |
| `text/tokens.jsonl` | the terms of each paper, now and as first stored |
| `text/refs.jsonl` | the arXiv ids each analysed paper's reference list names |
| `vocab.json` | the terms that count, with document frequency and idf |
| `vectors.jsonl` | one TF-IDF vector per paper |
| `similar.jsonl` | the most similar papers of each paper that is not withdrawn |
| `citations/edges.jsonl` | who cites whom inside the library |
| `citations/external.jsonl` | cited arXiv ids the library does not hold |
| `citations/nodes.jsonl` | per paper: citation counts, papers cited together with it, papers sharing its references |
| `citations/pagerank.jsonl` | per paper: its connected part and its PageRank |
| `centroids.json` | the fitted topics |
| `assignments.jsonl` | the topic of each paper that is not withdrawn |
| `topics.json` | per topic: name, blurb, terms, typical papers, size |
| `trends/<feed>.json` | weekly counts, rising keywords, bursts and topic momentum of a feed |
| `authors.json` | papers with a first-time author per week, the most active authors |
| `ranking.jsonl` | each feed's papers of each announced week, ranked |
| `digests/<feed>/<week>.md` | the weekly digest page of a feed |
| `stats.json` | the day, the settings hash, input counts, unit counts per stage, size and hash of every file |
| `runs.jsonl` | one line per finished build: seconds per stage and work counters |

The vocabulary and the topics are fitted once a month, on the papers the library held when the month began and on the titles and abstracts those papers had when they were first stored. So scores, topics and topic names hold still during a month, whatever is announced or replaced. A week's digest is ranked with the citations made by the end of that week, so the digest of a past week is not rewritten when later papers cite its members.

Settings live in `insights.yaml` in the project folder; a file of the same name in the home overrides
single values. `python -m app insights config` prints the values in force in the same form, so
`python -m app insights config > $ARXIVLENS_HOME/insights.yaml` changes nothing and a line of it can
then be edited.

`python -m app insights check` hashes every file `stats.json` names and lists files that are
`changed`, `missing` or `unknown`; it ends with 1 when a named file is changed or missing.
`insights diff` takes two result folders (say, one built with `--out` under other settings) and
says which files differ and, per stage, how many of its rows, topics or files.

`python -m app insights report` writes `report.md`, twelve tables about the library, and `audit.txt`,
which recounts the numbers of the tables from the database and from other files of the folder.
Each recount is one line, `ok` or `differ`; the command ends with 1 when any differs.

## Tools

Scripts that run from any folder with `ARXIVLENS_HOME` set as above:

| Script | What it prints |
|---|---|
| `python tools/weekly_mail.py cs.LG` | the text of the weekly mail of a feed, by default for its last closed week; it needs only a copy of the result folder (`--folder DIR`) |
| `python tools/related.py 2608.90033` | a paper's neighbours, the papers it cites and the papers that cite it |
| `python tools/topic_export.py 0` | the papers of a topic as CSV |
| `python tools/missing_refs.py --min 2` | the cited arXiv ids that are not analysed: not tracked, waiting, failed or withdrawn |
| `python tools/build_times.py --last 5` | seconds per stage and work counters of the last builds |

## Pages, API, chat and MCP

`uvicorn app.main:app` serves the pages under `/ui`, the REST routes, and the API docs at `/docs`.

| Page | Shows |
|---|---|
| `/ui` | the digest: analyses of the last 24 hours by novelty |
| `/ui/papers` | the library: search, category and score filters, sorting |
| `/ui/categories` | subscriptions: add and remove categories |
| `/ui/paper/{id}` | one paper: analysis, version, feeds, timeline, a Retry button when it failed, related papers |
| `/ui/chat` | the chat |
| `/ui/topics`, `/ui/topics/{topic}` | the topics with their terms; one topic with its typical and closest papers |
| `/ui/trends`, `/ui/trends/{feed}` | weekly counts, rising terms, bursts and topic momentum of a feed |
| `/ui/weekly`, `/ui/weekly/{feed}`, `/ui/weekly/{feed}/{week}` | the ranked papers of a feed's week |
| `/ui/arxiv/{arxiv_id}` | redirects to the page of that paper |

The REST routes: `GET /categories`, `POST /categories`, `DELETE /categories/{id}`, `GET /papers`,
`GET /papers/{id}/analysis`, `POST /papers/{id}/retry`, `GET /daily-digest`, `POST /process`,
`GET /status`, `GET /last-updated`, the conversation routes and `POST /chat` (server-sent events).
Beside them, read from the result folder, answering 404 until a build has run:
`GET /insights/stats`, `GET /insights/topics`, `GET /insights/topics/{topic}`,
`GET /insights/trends/{feed}`, `GET /insights/papers/{arxiv_id}/related`,
`GET /insights/digests/{feed}/{week}`.

The chat model can call these tools before it answers:

| Chat tool | Does |
|---|---|
| `get_daily_digest` | recent papers by novelty |
| `search_papers` | search titles, abstracts, summaries and keywords |
| `get_paper_detail` | the whole analysis of a paper |
| `compare_papers` | several papers side by side |
| `list_categories`, `add_category`, `remove_category` | the subscriptions |
| `get_related_papers` | similar, cited and citing papers of a paper |
| `get_topic` | the topics, or one topic by number |
| `get_trends` | rising terms, bursts and topic momentum of a feed |
| `get_weekly_digest` | the ranked papers of a feed's week |

MCP is served at `/mcp/mcp` for MCP clients:

| MCP tool | Does |
|---|---|
| `get_daily_digest`, `search_papers`, `get_paper_analysis` | papers and their analyses |
| `add_category`, `list_categories` | the subscriptions |
| `related_papers` | similar, cited and citing papers of an arXiv id |
| `topics` | the topics of the library |
| `trends` | the rising terms of a feed |

Both kinds of tools can be called from a shell, on the home of the offline session:

```bash
python -m app tool get_topic '{"topic": 0}'
python -m app tool --mcp topics
```

## Against the real services

Settings come from the environment or from `.env` in the working directory.

| Variable | Default | |
|---|---|---|
| `DATABASE_URL` | empty | empty means `lens.db` in the home; PostgreSQL in production (`postgresql+asyncpg://...`) |
| `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY` | | the model for analyses, chat and topic names |
| `AZURE_OPENAI_DEPLOYMENT` | `gpt-4o` | |
| `AZURE_OPENAI_API_VERSION` | `2024-12-01-preview` | |
| `AZURE_BLOB_CONNECTION_STRING`, `AZURE_STORAGE_ACCOUNT_NAME` | not set | PDFs are staged in blob storage only when one of the two is set |
| `AZURE_BLOB_CONTAINER_NAME` | `paper-pdfs` | |
| `ARXIV_RSS_BASE` | `https://rss.arxiv.org/rss` | the feeds, `<base>/<category>` |
| `ARXIV_API_URL` | `https://export.arxiv.org/api/query` | the arXiv API |
| `ARXIV_PDF_BASE` | empty | when set, PDFs come from `<base>/<arxiv_id>v<version>` instead of the link the API gave |
| `ARXIV_API_DELAY_SECONDS` | `3.0` | pause between two API requests |
| `SCHEDULER_ENABLED` | `true` | `false` starts the server without scheduled cycles |
| `POLL_INTERVAL_HOURS` | `6` | time between scheduled cycles |
| `PROCESSING_LEASE_SECONDS` | `600` | how long a claim on a paper holds without renewal, by the clock |
| `CATEGORIES_FILE` | `categories.yaml` | categories to subscribe to when first seen |

`categories.yaml` lists the categories a new library starts with (`cs.AI`, `cs.LG`, `cs.CL`, `cs.CV`,
`stat.ML`). Each is subscribed the first time it is seen; one that was removed stays removed. Without
the file the library starts with no category.

A cycle polls every feed and then processes the papers that wait, one at a time. It starts on the
scheduler (at server start and every `POLL_INTERVAL_HOURS`), with `POST /process` or the Refresh
button, or with `python -m app cycle`. Each cycle is a row of `runs` with one row per feed in
`run_feeds`, shown by `GET /status`.

- A worker claims a paper and renews its lease while it works; a lease not renewed for `PROCESSING_LEASE_SECONDS` is taken over by the next cycle, so no paper is analysed twice.
- Every try counts; a failed paper is tried again by later cycles until it has three tries, then it waits for a retry by hand (`POST /papers/{id}/retry`, the Retry button, `python -m app paper retry ID`).
- A replaced paper starts again with no tries and its new version is analysed; a withdrawn paper is not processed.
- A run left open by a process that died is closed by the next cycle once its lease has passed.

## Tests

```bash
python -m pytest
```

The suite runs offline, on SQLite, from the project root, on Python 3.10 to 3.14. It needs Linux or
another POSIX system: tests stop a process at a chosen point with `ARXIVLENS_KILL_AT` (`fs:N` ends it
with status 137 before its N-th file-system change, `commit:N` before its N-th database commit) and
slow its analyses with `ARXIVLENS_SLOW` (seconds), and both need POSIX processes. To run the
ingestion tests on PostgreSQL, set `ARXIVLENS_TEST_DATABASE_URL` to an empty database whose tables
they may drop.

## Limits

- A build computes everything again and asks the model for every topic name.
- A build that is interrupted leaves a partly written folder; run it again.
- The report audits against the database as it is now; run it right after a build.

## Deployment

The `Dockerfile` builds a `python:3.12-slim` image with `app/`, `categories.yaml` and
`insights.yaml`, serving uvicorn on port 8000. `deploy.ps1` (PowerShell with the Azure CLI) creates
the resource group `rg-paper-digest` with a PostgreSQL Flexible Server, a storage account reached
through a managed identity, an Azure OpenAI resource with a `gpt-4o` deployment and a container
registry, builds the image in the registry, and runs it as a Container App with the settings above.
On a server the home (`ARXIVLENS_HOME`) still holds the paper texts, the result folder and the
report. Deleting the resource group removes everything again.

## Project structure

```text
app/
  main.py, config.py, cli.py     the server, the settings, the command line (python -m app)
  clock.py, home.py, ids.py      the one clock, the home folder, arXiv id helpers
  faults.py                      switches that end or slow a process, for tests
  api/                           REST routes, including /insights/*
  db/                            tables, schema steps, data operations, versions and timeline
  services/                      sources, poller, processor, analyser, PDF text, text store, cycle, chat
  insights/                      the ten stages of a build, settings, check, diff, report, the read side
  mcp/                           the MCP server
  scheduler/                     the scheduled cycles
  ui/                            pages, templates and static files
dev/                             corpus generator, the two corpora, the stand-in, an old-schema database
docs/                            insights.md: every file and value of a result folder
tools/                           five scripts over a result folder
tests/                           the test suite and the small programs it starts
```

## License

MIT
