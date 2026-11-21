"""Writes a small database in an earlier schema of ArxivLens, for trying the upgrade on existing data.

    python dev/sample_db.py TARGET [--schema original|reworked]

TARGET is a file path (written as SQLite) or a full SQLAlchemy URL. "original" is the schema before
the ingestion rework, with one row per stored version; "reworked" is the schema of the rework, before
schema steps were recorded. The table definitions are frozen copies; nothing comes from the app.
"""
import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    inspect,
)
from sqlalchemy.engine import make_url

STARTED = datetime(2024, 6, 2, 12, 0, tzinfo=timezone.utc)
PUBLISHED = datetime(2024, 6, 1, tzinfo=timezone.utc)
CATEGORIES = [(1, "AI", "cs.AI"), (2, "LG", "cs.LG")]

SUMMARY_90005 = (
    "The paper makes the router of a sparse expert layer stable by asking two routers to agree. "
    "Tokens on which they disagree go to a shared expert, so no balancing loss is needed. "
    "Training needs fewer restarts and expert load is more even, at the same final quality."
)
SUMMARY_90006 = (
    "The paper sends each input of a mixture model to its two best components, weighed by score. "
    "This gives the gate a signal about close choices at little extra cost. "
    "Training varies less than with hard routing and reaches the same quality."
)

TEXT_90005 = """arXiv:2406.90005v1 [cs.AI] 1 Jun 2024
Routing Sparse Experts by Agreement for Stable Training
A. Author
Abstract
Sparse mixtures of experts send each token to a few of many sub-networks, but
the router that makes this choice is often unstable early in training. We
propose agreement routing, which asks two cheap routers to agree before a token
is sent and sends it to a shared expert when they do not. The method adds no
auxiliary loss and keeps the load of every expert within a fixed band. On three
language tasks it reaches the quality of a tuned baseline with 0.6 times the
restarts and a 12% smaller spread of expert load.
1 Introduction
Sparse layers let a model grow without a matching growth in compute per token.
Each token visits a small number of experts chosen by a learned router [1]. The
router is trained together with the experts, so its early choices are made on
features that are still changing. Small changes in those features then move
whole groups of tokens from one expert to another [2].
Earlier work adds a balancing term to the loss to keep experts busy [3]. Such
terms help on average but interact with the main objective in ways that are hard
to tune. This paper asks a simpler question. Can a router be made stable by
asking it to be sure before it acts, rather than by pushing it towards balance?
2 Method
Agreement routing keeps two routers with separate weights and the same input. A
token is sent to an expert only when both routers rank that expert first.
Otherwise the token goes to a shared expert that every token may use. The shared
expert is an ordinary feed-forward block and is trained on the tokens it
receives.
Because disagreement sends tokens to the shared expert, a router that changes
its mind costs little. The two routers are initialised differently and drift
apart only when their inputs justify it. We cap the share of tokens any expert
may take at twice the mean, as in [4]. No other change to the layer is needed.
Training uses the usual cross-entropy objective and nothing else. The routers
receive gradients through the gate values of the tokens they route. A token sent
to the shared expert gives no gradient to either router. This keeps the update
of the routers small while they disagree.
3 Experiments
We train models with 16 and 64 experts on three language tasks of growing size.
Each setting is run with five seeds, and a run that diverges is restarted from
its last checkpoint. We count restarts, measure the spread of expert load, and
report the final validation loss. The baseline uses a tuned balancing term with
the weight of [1].
Agreement routing needs 0.6 times the restarts of the baseline over all
settings. The spread of expert load falls by 12% on average and by 19% with 64
experts. Final loss is within 0.02 of the baseline in every setting. Ablations
show that the shared expert matters most: without it, disagreement drops tokens
and quality falls [5, 6].
4 Conclusion
Asking two routers to agree is a cheap way to make sparse layers stable. It
needs no balancing term and leaves the objective unchanged. The shared expert
absorbs the tokens on which the routers are unsure.
The approach is simple to add to existing layers. Its cost is one extra router
per layer, which is small next to the experts. We see it as a step towards
sparse models that train as reliably as dense ones.
References
[1] K. Maren and T. Soval. Graded routing for mixture models.
arXiv:2406.90006, 2024.


[2] R. Ilden, K. Maren, and D. Volen. Agreement losses in sparse networks. arXiv
preprint arXiv:2311.90412v2, 2023.
[3] T. Soval. Load balancing without auxiliary terms. CoRR,
abs/2208.91377, 2022.
[4] D. Volen et al. Capacity limits for wide expert layers.
arXiv:2103.90554, 2021.
[5] K. Maren. On the capacity of gated layers. In Proceedings of the Northern
Workshop on Learning Systems, pages 112-130, 2021.
[6] T. Soval and R. Ilden. Routing as assignment. Journal of Applied
Inference, 14(2):301-322, 2020. doi:10.9412/routing.214
"""

TEXT_90006 = """arXiv:2406.90006v1 [cs.LG] 1 Jun 2024
Graded Routing for Mixture Models
A. Author
Abstract
Mixture models assign each input to one of several components, and the
assignment is usually made by a hard choice. We study graded routing, in which
an input is shared between the two most likely components in proportion to their
scores. Graded routing keeps the cost of a hard choice close and gives smoother
gradients to the gate. In experiments on four tasks it lowers the variance of
training by 0.3 and matches hard routing in final quality.
1 Introduction
A mixture model is only as good as the gate that chooses its components. Hard
gates are cheap but tell the gate nothing about the choices it did not make [1].
Soft gates give that signal but visit every component for every input. Both are
common, and the trade-off between them is rarely measured.
We propose a middle way that visits two components and weighs them by score. The
second component is cheap to add and tells the gate how close its choice was. We
show that this is enough to make training steadier without changing the final
model much.
2 Approach
For each input the gate scores every component and keeps the two best. The input
is sent to both, and their outputs are mixed by the normalised scores. When the
best score is far ahead, the mixture is almost a hard choice. When two scores
are close, both components learn from the input [2].
The gate is trained by the main loss alone. We add no term that pushes the
scores apart or together. Components that are never among the two best receive
no inputs and are reset after a fixed number of steps.
3 Evaluation
We compare hard, soft and graded routing on four tasks with 8 and 32 components.
Each setting is run with seven seeds. We measure the variance of the training
loss over the last tenth of training and the final validation loss.
Graded routing lowers the variance by 0.3 relative to hard routing and costs 1.4
times its compute. Soft routing lowers the variance further but costs 6.5 times
as much with 32 components. Final quality of graded and hard routing differs by
less than 0.01 in every setting [3].
4 Discussion
Visiting a second component is a cheap way to give a gate the signal it lacks.
The method is easy to add and has no setting that needs tuning beyond the reset
interval.
Graded routing is not a replacement for careful design of the components. It
makes the gate steadier, which helps most when training is short or the data is
noisy. We expect the same idea to help other models that make discrete choices
during training.
REFERENCES
1. K. Maren (2019). Soft assignment in deep mixtures. arXiv:1907.90233.
2. T. Soval and R. Ilden (2022). Sparse gates revisited.
https://arxiv.org/abs/2201.90871v1
3. D. Volen (2018). Expert networks at scale. Proceedings of the Northern
Workshop on Learning Systems.
"""

TEXTS = {"2406.90005": TEXT_90005, "2406.90006": TEXT_90006}

# arXiv id as stored (with its version), category, status, analysis summary
ORIGINAL_ROWS = [
    ("2406.90001v1", 1, "done", "A1"),
    ("2406.90001v2", 2, "done", "A2"),
    ("2406.90002v1", 1, "done", "B1"),
    ("2406.90002v3", 2, "pending", None),
    ("2406.90003v1", 2, "failed", None),
    ("2406.90004v2", 2, "processing", None),
    ("2406.90005v1", 1, "done", SUMMARY_90005),
    ("2406.90006v1", 2, "done", SUMMARY_90006),
]

# arXiv id, version, categories that listed it, status, attempts, analysis summary
REWORKED_ROWS = [
    ("2406.90001", 2, (1, 2), "done", 1, "A2"),
    # replaced by version 3 and kept as done with the analysis of version 1
    ("2406.90002", 3, (1, 2), "done", 1, "B1"),
    ("2406.90003", 1, (2,), "failed", 3, None),
    ("2406.90004", 2, (2,), "processing", 1, None),
    ("2406.90005", 1, (1,), "done", 1, SUMMARY_90005),
    ("2406.90006", 1, (2,), "done", 1, SUMMARY_90006),
]
RUN_STARTED = STARTED + timedelta(minutes=30)


def _status():
    return Enum("pending", "processing", "done", "failed", name="paperstatus")


def _chat_tables(metadata: MetaData) -> None:
    Table(
        "conversations", metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("title", String, nullable=False),
        Column("research_interest", Text, nullable=True),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("updated_at", DateTime(timezone=True), nullable=False),
    )
    Table(
        "messages", metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("conversation_id", Integer, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        Column("role", Enum("user", "assistant", "tool", name="messagerole"), nullable=False),
        Column("content", Text, nullable=True),
        Column("tool_calls", JSON, nullable=True),
        Column("tool_call_id", String, nullable=True),
        Column("created_at", DateTime(timezone=True), nullable=False),
    )


def _categories(metadata: MetaData) -> None:
    Table(
        "categories", metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("name", String, nullable=False),
        Column("arxiv_code", String, unique=True, nullable=False),
        Column("description", String, nullable=True),
        Column("added_at", DateTime(timezone=True), nullable=False),
    )


def original_schema() -> MetaData:
    """The tables as the version before the ingestion rework created them."""
    metadata = MetaData()
    _categories(metadata)
    Table(
        "papers", metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("category_id", Integer, ForeignKey("categories.id", ondelete="CASCADE"), nullable=False),
        Column("arxiv_id", String, unique=True, nullable=False),
        Column("title", String, nullable=False),
        Column("authors", JSON, nullable=False),
        Column("abstract", Text, nullable=False),
        Column("pdf_url", String, nullable=False),
        Column("published_at", DateTime(timezone=True), nullable=False),
        Column("categories", JSON, nullable=False),
        Column("journal_ref", String, nullable=True),
        Column("doi", String, nullable=True),
        Column("status", _status(), nullable=False),
        Column("error_message", Text, nullable=True),
        Column("created_at", DateTime(timezone=True), nullable=False),
    )
    Table(
        "analyses", metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("paper_id", Integer, ForeignKey("papers.id", ondelete="CASCADE"), nullable=False),
        Column("full_text", Text, nullable=False),
        Column("summary", Text, nullable=False),
        Column("key_contributions", JSON, nullable=False),
        Column("methodology", Text, nullable=False),
        Column("limitations", Text, nullable=True),
        Column("keywords", JSON, nullable=False),
        Column("novelty_score", Integer, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        UniqueConstraint("paper_id"),
    )
    _chat_tables(metadata)
    return metadata


def reworked_schema() -> MetaData:
    """The tables as the ingestion rework created them: one row per paper, leases, runs."""
    metadata = MetaData()
    _categories(metadata)
    Table(
        "paper_categories", metadata,
        Column("paper_id", Integer, ForeignKey("papers.id", ondelete="CASCADE"), primary_key=True),
        Column("category_id", Integer, ForeignKey("categories.id", ondelete="CASCADE"), primary_key=True,
               index=True),
    )
    Table(
        "papers", metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("category_id", Integer, ForeignKey("categories.id", ondelete="CASCADE"), nullable=False),
        Column("arxiv_id", String, unique=True, nullable=False),
        Column("version", Integer, nullable=False, server_default="1"),
        Column("title", String, nullable=False),
        Column("authors", JSON, nullable=False),
        Column("abstract", Text, nullable=False),
        Column("pdf_url", String, nullable=False),
        Column("published_at", DateTime(timezone=True), nullable=False),
        Column("categories", JSON, nullable=False),
        Column("journal_ref", String, nullable=True),
        Column("doi", String, nullable=True),
        Column("status", _status(), nullable=False),
        Column("error_message", Text, nullable=True),
        Column("attempts", Integer, nullable=False, server_default="0"),
        Column("lease_token", String, nullable=True),
        Column("lease_at", DateTime(timezone=True), nullable=True),
        Column("lease_host", String, nullable=True),
        Column("lease_mono", Float, nullable=True),
        Column("created_at", DateTime(timezone=True), nullable=False),
    )
    Table(
        "analyses", metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("paper_id", Integer, ForeignKey("papers.id", ondelete="CASCADE"), nullable=False),
        Column("full_text", Text, nullable=False),
        Column("summary", Text, nullable=False),
        Column("key_contributions", JSON, nullable=False),
        Column("methodology", Text, nullable=False),
        Column("limitations", Text, nullable=True),
        Column("keywords", JSON, nullable=False),
        Column("novelty_score", Integer, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        UniqueConstraint("paper_id"),
    )
    Table(
        "runs", metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("started_at", DateTime(timezone=True), nullable=False),
        Column("finished_at", DateTime(timezone=True), nullable=True),
        Column("new_papers", Integer, nullable=False),
        Column("updated", Integer, nullable=False),
        Column("skipped", Integer, nullable=False),
        Column("processed", Integer, nullable=False),
        Column("failed", Integer, nullable=False),
        Column("error_message", Text, nullable=True),
    )
    Table(
        "seeded_categories", metadata,
        Column("arxiv_code", String, primary_key=True),
        Column("seeded_at", DateTime(timezone=True), nullable=False),
    )
    _chat_tables(metadata)
    return metadata


def _minute(n: int) -> datetime:
    return STARTED + timedelta(minutes=n)


def _paper(arxiv_id: str, shown_id: str, category_id: int, status: str, created: int) -> dict:
    return {
        "category_id": category_id,
        "arxiv_id": arxiv_id,
        "title": f"Paper {shown_id[:10]}",
        "authors": ["A. Author"],
        "abstract": f"Abstract of {shown_id}.",
        "pdf_url": f"https://arxiv.org/pdf/{shown_id}",
        "published_at": PUBLISHED,
        "categories": ["cs.AI", "cs.LG"],
        "journal_ref": None,
        "doi": None,
        "status": status,
        "error_message": "analysis failed" if status == "failed" else None,
        "created_at": _minute(created),
    }


def _analysis(paper_id: int, arxiv_id: str, summary: str, created: int) -> dict:
    return {
        "paper_id": paper_id,
        "full_text": TEXTS.get(arxiv_id, "Full text."),
        "summary": summary,
        "key_contributions": ["Contribution"],
        "methodology": "Method.",
        "limitations": None,
        "keywords": ["kw"],
        "novelty_score": 5,
        "created_at": _minute(created),
    }


def _insert(conn, table, row: dict) -> int:
    return conn.execute(table.insert().values(**row)).inserted_primary_key[0]


def _write_categories(conn, tables) -> None:
    for category_id, name, code in CATEGORIES:
        conn.execute(tables["categories"].insert().values(
            id=category_id, name=name, arxiv_code=code, description=None, added_at=STARTED,
        ))


def write_original(conn) -> int:
    tables = original_schema().tables
    _write_categories(conn, tables)
    analyses = 0
    for number, (arxiv_id, category_id, status, summary) in enumerate(ORIGINAL_ROWS):
        paper_id = _insert(conn, tables["papers"], _paper(arxiv_id, arxiv_id, category_id, status, number))
        if summary:
            _insert(conn, tables["analyses"], _analysis(paper_id, arxiv_id[:10], summary, analyses))
            analyses += 1
    return len(ORIGINAL_ROWS)


def write_reworked(conn) -> int:
    tables = reworked_schema().tables
    _write_categories(conn, tables)
    analyses = 0
    for number, (arxiv_id, version, links, status, attempts, summary) in enumerate(REWORKED_ROWS):
        row = _paper(arxiv_id, f"{arxiv_id}v{version}", links[0], status, number)
        row.update(version=version, attempts=attempts, lease_token=None, lease_at=None, lease_host=None,
                   lease_mono=None)
        if status == "processing":
            # Left behind by a worker on another machine that stopped during the run below.
            row.update(lease_token="5d0c3f9a8e2b4c61a7f0e9d8c6b5a413", lease_at=RUN_STARTED,
                       lease_host="oldhost/boot", lease_mono=12.5)
        paper_id = _insert(conn, tables["papers"], row)
        for category_id in links:
            conn.execute(tables["paper_categories"].insert().values(paper_id=paper_id, category_id=category_id))
        if summary:
            _insert(conn, tables["analyses"], _analysis(paper_id, arxiv_id, summary, analyses))
            analyses += 1
    conn.execute(tables["runs"].insert().values(
        started_at=RUN_STARTED, finished_at=None, new_papers=0, updated=0, skipped=0, processed=0, failed=0,
        error_message=None,
    ))
    for _category_id, _name, code in CATEGORIES:
        conn.execute(tables["seeded_categories"].insert().values(arxiv_code=code, seeded_at=STARTED))
    return len(REWORKED_ROWS)


class _Refused(Exception):
    pass


def _write(conn, schema: str) -> int:
    if inspect(conn).has_table("papers"):
        raise _Refused()
    metadata = original_schema() if schema == "original" else reworked_schema()
    metadata.create_all(conn)
    return write_original(conn) if schema == "original" else write_reworked(conn)


async def _write_async(url, schema: str) -> int:
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            return await conn.run_sync(_write, schema)
    finally:
        await engine.dispose()


def database_url(target: str):
    """A URL as given, or a SQLite file for a plain path."""
    if "://" in target:
        return make_url(target)
    return make_url("sqlite:///" + os.path.abspath(target).replace(os.sep, "/"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="sample_db.py", description="Write a small database in an earlier schema.")
    parser.add_argument("target", help="a file path (SQLite) or a SQLAlchemy URL")
    parser.add_argument("--schema", choices=["original", "reworked"], default="original")
    args = parser.parse_args(argv)
    url = database_url(args.target)
    try:
        if url.get_dialect().is_async:
            count = asyncio.run(_write_async(url, args.schema))
        else:
            engine = create_engine(url)
            try:
                with engine.begin() as conn:
                    count = _write(conn, args.schema)
            finally:
                engine.dispose()
    except _Refused:
        print(f"error: {args.target} already has a papers table", file=sys.stderr)
        return 2
    print(f"wrote {count} papers to {args.target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
