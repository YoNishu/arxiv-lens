"""Commits a new paper while the insights snapshot is reading, and prints what the read saw."""

import json
import sqlite3

from sqlalchemy import event

from app import home
from app.db import database
from app.insights import snapshot

NEW_ID = "2608.99901"
state = {"armed": False, "began": False, "done": False}


def _copy(db, table: str, values: dict) -> None:
    """Insert a copy of the oldest row of `table`, with `values` in place of the columns they name."""
    columns = [row[1] for row in db.execute(f'PRAGMA table_info("{table}")') if not (row[1] == "id" and row[5])]
    picked = ", ".join("?" if name in values else f'"{name}"' for name in columns)
    params = [values[name] for name in columns if name in values]
    db.execute(f'INSERT INTO "{table}" ({", ".join(columns)}) SELECT {picked} FROM "{table}" ORDER BY rowid LIMIT 1',
               params)


def _commit_new_paper() -> None:
    db = sqlite3.connect(str(home.db_file()), timeout=30)
    try:
        _copy(db, "papers", {"arxiv_id": NEW_ID, "version": 1, "withdrawn_at": None})
        paper_id, category_id = db.execute("SELECT id, category_id FROM papers WHERE arxiv_id = ?", (NEW_ID,)).fetchone()
        db.execute("INSERT INTO paper_categories (paper_id, category_id) VALUES (?, ?)", (paper_id, category_id))
        _copy(db, "analyses", {"paper_id": paper_id, "paper_version": 1})
        _copy(db, "paper_versions", {"paper_id": paper_id, "version": 1})
        db.commit()
    finally:
        db.close()


def _after_execute(conn, cursor, statement, parameters, context, executemany) -> None:
    # The new paper is committed right after the first query of the read transaction has run.
    text = statement.strip().upper()
    if not state["armed"] or state["done"]:
        return
    if text == "BEGIN":
        state["began"] = True
    elif state["began"] and text.startswith("SELECT") and "FROM PAPERS" in text:
        state["done"] = True
        _commit_new_paper()


def main() -> None:
    before = database.run(snapshot.read())
    event.listen(database.engine.sync_engine, "after_cursor_execute", _after_execute)
    state["armed"] = True
    during = database.run(snapshot.read())
    state["armed"] = False
    after = database.run(snapshot.read())
    new = [facts for facts in after if facts["arxiv_id"] == NEW_ID]
    print(json.dumps({
        "inserted": state["done"],
        "same_as_before": during == before,
        "seen_during": any(facts["arxiv_id"] == NEW_ID for facts in during),
        "papers_before": len(before),
        "papers_after": len(after),
        "new_feeds": new[0]["feeds"] if new else [],
        "new_analysis": bool(new and new[0]["analysis"]),
        "new_first": bool(new and new[0]["first"]),
    }, sort_keys=True))


if __name__ == "__main__":
    main()
