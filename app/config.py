from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Azure OpenAI (AI Foundry)
    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    azure_openai_deployment: str = "gpt-4o"
    azure_openai_api_version: str = "2024-12-01-preview"

    # Database: postgresql+asyncpg://... in production, sqlite+aiosqlite:///<file> for local runs
    database_url: str = "postgresql+asyncpg://localhost:5432/paper_digest"

    # Azure Blob Storage (PDF staging is skipped when neither value is set)
    azure_blob_connection_string: str = ""
    azure_storage_account_name: str = ""
    azure_blob_container_name: str = "paper-pdfs"

    # arXiv endpoints (override both to run against dev/standin.py)
    arxiv_rss_base: str = "https://rss.arxiv.org/rss"
    arxiv_api_url: str = "https://export.arxiv.org/api/query"
    arxiv_api_delay_seconds: float = 3.0

    # Scheduler
    scheduler_enabled: bool = True
    poll_interval_hours: int = 6

    # Processing: a paper whose lease was not renewed for this long may be taken over
    processing_lease_seconds: float = 600

    # Scratch directory for downloaded PDFs
    data_dir: str = "data"

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
