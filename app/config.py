"""Settings of the application, read once from the environment and an optional .env file.

Left without DATABASE_URL, the application keeps its data in the SQLite file of the home
folder, so a local run needs no configuration at all.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict

from app import home


class Settings(BaseSettings):
    # Azure OpenAI (AI Foundry)
    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    azure_openai_deployment: str = "gpt-4o"
    azure_openai_api_version: str = "2024-12-01-preview"

    # Database: postgresql+asyncpg://... in production; empty means the SQLite file in the home folder
    database_url: str = ""

    # Azure Blob Storage (PDF staging is skipped when neither value is set)
    azure_blob_connection_string: str = ""
    azure_storage_account_name: str = ""
    azure_blob_container_name: str = "paper-pdfs"

    # arXiv endpoints (override them to run against dev/standin.py)
    arxiv_rss_base: str = "https://rss.arxiv.org/rss"
    arxiv_api_url: str = "https://export.arxiv.org/api/query"
    arxiv_api_delay_seconds: float = 3.0
    # When set, a PDF is fetched from <base>/<arxiv_id>v<version> instead of the stored link
    arxiv_pdf_base: str = ""

    # Scheduler
    scheduler_enabled: bool = True
    poll_interval_hours: int = 6

    # Processing: a paper whose lease was not renewed for this long may be taken over
    processing_lease_seconds: float = 600

    # Categories seed file
    categories_file: str = "categories.yaml"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def blob_storage_configured(self) -> bool:
        return bool(self.azure_blob_connection_string or self.azure_storage_account_name)

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


settings = Settings()
if not settings.database_url:
    # The home is looked up once, here: a process works on one database from start to end, and
    # everything that imports the settings finds a complete URL.
    settings.database_url = home.sqlite_url()
