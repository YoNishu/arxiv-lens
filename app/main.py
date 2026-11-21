import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.routes import router as api_router
from app.config import settings
from app.db import migrate
from app.db.database import engine
from app.mcp.server import mcp
from app.scheduler.tasks import start_scheduler, stop_scheduler
from app.services import cycle
from app.ui.views import ui_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting ArxivLens")
    logger.info("Azure OpenAI endpoint: %s", settings.azure_openai_endpoint or "NOT SET")
    logger.info("Azure OpenAI deployment: %s", settings.azure_openai_deployment)
    logger.info("Database: %s", settings.database_url.split("@")[-1] if "@" in settings.database_url else settings.database_url)
    logger.info(
        "Blob container: %s",
        settings.azure_blob_container_name if settings.blob_storage_configured else "not configured (PDFs are not staged)",
    )
    logger.info("arXiv feeds: %s — API: %s", settings.arxiv_rss_base, settings.arxiv_api_url)
    logger.info("Poll interval: %dh", settings.poll_interval_hours)

    # Creates the tables, or upgrades a database written by an older version in place.
    async with engine.begin() as conn:
        await conn.run_sync(migrate.upgrade)
    logger.info("Database tables created/verified")

    if settings.scheduler_enabled:
        start_scheduler()
    else:
        logger.info("Scheduler disabled (SCHEDULER_ENABLED=false)")
    logger.info("Application ready")
    # The mounted MCP app needs its own lifespan (its session manager) to answer requests.
    async with mcp_app.lifespan(mcp_app):
        yield
    if settings.scheduler_enabled:
        stop_scheduler()
    await cycle.shutdown()
    await engine.dispose()
    logger.info("Application shutdown complete")


app = FastAPI(title="ArxivLens", lifespan=lifespan)

app.include_router(api_router)
app.include_router(ui_router)
app.mount("/static", StaticFiles(directory="app/ui/static"), name="static")

# Mount MCP server
mcp_app = mcp.http_app(path="/mcp")
app.mount("/mcp", mcp_app)
