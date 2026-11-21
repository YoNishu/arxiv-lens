"""The database engine and session factory, and the way in for code without an event loop.

The engine is made at import from the settings: PostgreSQL in production, the SQLite file of
the home folder otherwise. Commands, tools and other synchronous callers hand each piece of
work to run() as one coroutine that starts with open_database().
"""
import asyncio
import logging
import sqlite3
import ssl

import certifi
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import home
from app.config import settings

logger = logging.getLogger(__name__)

IS_SQLITE = settings.is_sqlite

if IS_SQLITE and sqlite3.sqlite_version_info < (3, 35):
    # The schema steps drop columns with ALTER TABLE ... DROP COLUMN.
    raise RuntimeError("SQLite 3.35 or newer is needed")

connect_args: dict = {}
if "azure.com" in settings.database_url:
    ctx = ssl.create_default_context(cafile=certifi.where())
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    connect_args["ssl"] = ctx
    logger.info("Database: SSL enabled for Azure PostgreSQL")
if IS_SQLITE:
    # Wait for a writer in another connection or process instead of failing at once.
    connect_args["timeout"] = 30

logger.info("Database engine created — host=%s", settings.database_url.split("@")[-1].split("/")[0] if "@" in settings.database_url else "localhost")
engine = create_async_engine(settings.database_url, connect_args=connect_args)

if IS_SQLITE:

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_on_connect(dbapi_connection, connection_record):
        # Make SQLite behave like PostgreSQL where the app relies on it: enforced foreign keys
        # (ON DELETE CASCADE), and readers that do not block the writer (WAL), so several
        # connections and several app processes can share one database file.
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
        except Exception:  # e.g. an in-memory database
            logger.debug("Could not switch SQLite to WAL", exc_info=True)
        cursor.close()


async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def open_database() -> list[tuple[int, str]]:
    """Make the database ready: the home folder when the database lives there, then the schema.

    Returns the schema steps this call applied.
    """
    # Imported here so that making the engine does not need the models and the schema steps.
    from app.db import migrate

    if settings.database_url == home.sqlite_url():
        home.ensure()
    async with engine.connect() as conn:
        return await conn.run_sync(migrate.upgrade)


def run(coro):
    """Run one piece of database work from synchronous code and return its result.

    Pooled connections belong to the event loop that opened them, so they are given back before
    that loop ends. On SQLite this also closes the file, which folds the write-ahead log into it.
    """

    async def work():
        try:
            return await coro
        finally:
            await engine.dispose()

    return asyncio.run(work())
