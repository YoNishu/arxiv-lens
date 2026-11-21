# ArxivLens

An AI-powered research assistant that automatically tracks arXiv papers, analyzes them with GPT-4o, and lets you explore your paper library through a conversational chat interface.

## What It Does

- **Auto-fetches papers** from arXiv RSS feeds every 6 hours across your subscribed categories (cs.AI, cs.LG, cs.CL, cs.CV, stat.ML, etc.)
- **Downloads & analyzes full PDFs** — extracts text via PyMuPDF, then GPT-4o generates a structured analysis: summary, key contributions, methodology, limitations, keywords, and a novelty score (1–10)
- **Daily digest** — shows the latest papers ranked by novelty so you see what matters first
- **Browse & search** — full archive with keyword search, category filters, score filters, and sorting
- **Chat with your papers** — ask questions in natural language; the AI calls tools to search, retrieve, and compare papers from your database before answering
- **MCP server** — exposes paper tools to external AI agents (Claude, Copilot, etc.) via Model Context Protocol

## Tech Stack

| | |
|---|---|
| **Backend** | Python 3.11, FastAPI, SQLAlchemy 2.0 (async), asyncpg |
| **AI** | Azure OpenAI GPT-4o — JSON mode for analysis, function calling for chat |
| **Database** | PostgreSQL 16 (Azure Flexible Server); SQLite (aiosqlite) for local runs and tests |
| **PDF** | PyMuPDF for text extraction, Azure Blob Storage for staging |
| **Frontend** | Jinja2 templates, vanilla JS, SSE for real-time chat streaming |
| **Infra** | Docker, Azure Container Apps, ACR, managed identity |

## Architecture

```
arXiv RSS ──→ Poller ──→ Download PDF ──→ Extract Text ──→ GPT-4o ──→ PostgreSQL
                              │                              │
                         Blob Storage                  JSON analysis
                         (temp staging)              (summary, score,
                                                     contributions...)
                                                          │
                                           ┌──────────────┼──────────────┐
                                           │              │              │
                                        Web UI        REST API      MCP Server
                                     (Jinja2+SSE)    (FastAPI)     (FastMCP)
```

## Pages

| Page | What you see |
|---|---|
| **Digest** `/ui` | Today's papers ranked by novelty score |
| **Browse** `/ui/papers` | Full archive — search, filter by category/score, paginate |
| **Categories** `/ui/categories` | Add/remove arXiv category subscriptions |
| **Paper Detail** `/ui/paper/{id}` | Full AI analysis, abstract, metadata |
| **Chat** `/ui/chat` | Conversational AI — searches papers, compares them, answers questions |
| **API Docs** `/docs` | Auto-generated Swagger UI |

## Chat Tools

The chat assistant has 7 tools it can call before answering:

| Tool | What it does |
|---|---|
| `get_daily_digest` | Fetch recent papers ranked by score |
| `search_papers` | Full-text search across titles, abstracts, summaries, keywords |
| `get_paper_detail` | Get complete analysis for a specific paper |
| `compare_papers` | Side-by-side comparison of multiple papers |
| `list_categories` | Show subscribed arXiv categories |
| `add_category` | Subscribe to a new category |
| `remove_category` | Unsubscribe from a category |

## Quick Start

### Local, against the real services

```bash
pip install -r requirements.txt
# Set env vars: AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY, DATABASE_URL, AZURE_BLOB_CONNECTION_STRING
uvicorn app.main:app --port 8000
```

### Local, with no PostgreSQL, no Azure and no arXiv

Everything on one machine: a SQLite file is the database, and `dev/standin.py` stands in for the
arXiv feeds, the arXiv API, the PDF host and the model endpoint. PDFs are staged in blob storage
only when it is configured, so nothing here needs Azure.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt        # requirements.txt + pytest

# 1. What the outside world answers. The file is re-read on every request, so it can be
#    edited while things run (see the top of dev/standin.py for all keys).
cat > /tmp/scenario.json <<'EOF'
{"feeds": {"cs.AI": [["2401.00001", 1, "new"], ["2401.00002", 1, "new"]],
           "cs.LG": [["2401.00001", 1, "cross"]]}}
EOF
python dev/standin.py /tmp/scenario.json 9100 &

# 2. The app
export DATABASE_URL=sqlite+aiosqlite:////tmp/arxivlens.db   # four slashes = absolute path
export ARXIV_RSS_BASE=http://127.0.0.1:9100/rss
export ARXIV_API_URL=http://127.0.0.1:9100/api/query
export ARXIV_API_DELAY_SECONDS=0                            # no politeness delay for the stand-in
export AZURE_OPENAI_ENDPOINT=http://127.0.0.1:9100 AZURE_OPENAI_API_KEY=local
export SCHEDULER_ENABLED=false                              # no automatic cycles
uvicorn app.main:app --port 8000

# 3. Drive it (in another shell)
curl -X POST localhost:8000/process      # 202 {"run_id": 1, "started": true, ...}
curl localhost:8000/status               # running, papers per status, the latest cycle
curl localhost:8000/papers
curl localhost:9100/_n                   # how often each stand-in service was asked
```

The chat page needs a real model (the stand-in only answers analysis requests); everything else,
including the chat's tools and the MCP tools, works locally.

### Tests

```bash
pip install -r requirements-dev.txt
python -m pytest                          # from the project root; about 3 minutes
```

The tests are offline. Each one starts `dev/standin.py` and the real app (uvicorn, as a
subprocess) on a SQLite file in pytest's temporary directory, and drives it over HTTP: restarts,
killed processes and two app processes on one database are the real thing. To run the same tests
on PostgreSQL, point them at an empty database whose `public` schema they may drop:

```bash
ARXIVLENS_TEST_DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/arxivlens_test python -m pytest
```

### Settings

All settings are environment variables (or lines of `.env`).

| Variable | Default | |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://localhost:5432/paper_digest` | PostgreSQL in production; `sqlite+aiosqlite:///<file>` for local runs |
| `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_DEPLOYMENT`, `AZURE_OPENAI_API_VERSION` | | The model that writes the analyses and answers the chat |
| `AZURE_BLOB_CONNECTION_STRING` or `AZURE_STORAGE_ACCOUNT_NAME`, `AZURE_BLOB_CONTAINER_NAME` | not set | PDF staging in blob storage; skipped when neither of the first two is set |
| `ARXIV_RSS_BASE` | `https://rss.arxiv.org/rss` | Where the category feeds are read (`<base>/<category>`) |
| `ARXIV_API_URL` | `https://export.arxiv.org/api/query` | The arXiv API |
| `ARXIV_API_DELAY_SECONDS` | `3` | Pause between two arXiv API requests |
| `SCHEDULER_ENABLED` | `true` | `false` starts the app without its scheduler |
| `POLL_INTERVAL_HOURS` | `6` | Time between scheduled cycles |
| `PROCESSING_LEASE_SECONDS` | `600` | A paper in work whose lease was not renewed for this long is taken over |
| `CATEGORIES_FILE` | `categories.yaml` | Categories to subscribe to on first sight |
| `DATA_DIR` | `data` | Scratch directory for downloaded PDFs |

## Ingestion

- **One row per paper.** `arxiv_id` is the id without a version (`2401.00001`); `version` holds the
  version. Feed announcements of type `new` or `cross` store a paper that is not stored yet;
  `replace` and `replace-cross` never create one, they move a stored paper to the newer version
  (version, title, abstract, PDF link) and keep its analysis.
- **Categories.** A paper belongs to every subscribed category whose feed has listed it
  (`category_ids` in its JSON, `category_id` is the first). Removing a category removes only the
  papers no other category lists. A category from `categories.yaml` is created the first time it
  is seen; once removed it stays removed.
- **Whole feeds.** All new papers of a feed are fetched from the arXiv API in batches and stored in
  the same cycle. Ids the API does not return are skipped and counted.
- **One worker per paper.** A worker claims a paper in the database before it touches it and
  renews a lease while it works, so overlapping cycles and several app processes on one database
  never analyse a paper twice. A paper whose worker died is taken over by the next cycle once its
  lease has not been renewed for `PROCESSING_LEASE_SECONDS` (measured on the machine's monotonic
  clock between workers on one machine, on UTC time between machines, so keep clocks in sync when
  several machines share a database).
- **Attempts.** Every try is counted. A failed paper is tried again by later cycles until it has
  3 attempts; after that it stays failed until `POST /papers/{id}/retry`.
- **Cycles.** A cycle (poll all feeds, then process) runs in the background, one at a time per
  process, and is recorded in the `runs` table. `POST /process`, the Refresh button and adding a
  category start one and answer at once; `GET /status` shows what is going on.
- **Upgrade.** A database written by an earlier version is upgraded in place at startup
  (`app/db/migrate.py`): version suffixes move out of `arxiv_id`, duplicate rows of one paper are
  merged, and papers left failed or processing are queued again.

| Method | Path | |
|---|---|---|
| POST | `/process` | Start a cycle: `202 {"run_id", "started"}`; `started` is false while one is running |
| GET | `/status` | `running`, `papers` (count per status), `run` (the latest cycle) |
| POST | `/papers/{id}/retry` | Make a failed paper pending with 0 attempts: `202`; `404` unknown id; `409` unless failed |

### Azure (one-click)

```powershell
.\deploy.ps1
```

Creates all resources (PostgreSQL, Storage, OpenAI, Container Registry, Container App), builds the Docker image, and deploys.

### Tear down

```bash
az group delete --name rg-paper-digest --yes --no-wait
```

## Project Structure

```
app/
├── main.py              # FastAPI app, lifespan, router mounts
├── config.py            # Pydantic settings from env vars
├── api/
│   ├── routes.py        # REST endpoints (papers, categories, chat SSE)
│   └── schemas.py       # Pydantic request/response models
├── db/
│   ├── models.py        # SQLAlchemy models (Paper, Analysis, Run, Conversation, Message, ...)
│   ├── database.py      # Async engine + session factory (PostgreSQL or SQLite)
│   ├── migrate.py       # Schema creation + in-place upgrade of older databases
│   └── crud.py          # All database operations
├── services/
│   ├── feed_poller.py    # RSS polling + arXiv API metadata enrichment
│   ├── processor.py      # Claim → download → extract → analyze → save, with lease + attempts
│   ├── cycle.py          # The background poll+process cycle, one at a time, recorded in runs
│   ├── paper_analyzer.py # GPT-4o structured analysis (JSON mode)
│   ├── pdf_extractor.py  # PDF download, text extraction, blob storage
│   └── chat_agent.py     # Chat engine with tool-calling loop + SSE streaming
├── scheduler/
│   └── tasks.py          # APScheduler: cycle every 6h + startup catch-up
├── mcp/
│   └── server.py        # MCP tools for external AI agents
└── ui/
    ├── views.py         # Jinja2 page routes
    ├── templates/       # HTML templates (base, digest, browse, chat, etc.)
    └── static/          # CSS + chat.js
dev/
└── standin.py           # Local stand-in for arXiv, the PDF host and the model endpoint
tests/                   # Offline tests: python -m pytest
```

## License

MIT
