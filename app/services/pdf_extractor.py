"""PDFs: download, text extraction, and the optional staging copy in blob storage.

A PDF is only ever held in memory. The text taken from it goes to the text store
(app/services/textstore.py); nothing of the PDF itself is written to disk.
"""
import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class PdfUnavailable(RuntimeError):
    """The PDF host did not hand out the PDF."""


def blob_storage_configured() -> bool:
    """PDFs are staged in blob storage only when a connection string or an account is set."""
    return settings.blob_storage_configured


async def download_pdf(url: str) -> bytes:
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=300) as client:
            resp = await client.get(url)
    except Exception as e:
        raise PdfUnavailable(f"PDF request failed: {type(e).__name__}") from e
    if resp.status_code != 200:
        raise PdfUnavailable(f"PDF request failed with status {resp.status_code}")
    logger.info("Downloaded %s (%d bytes)", url, len(resp.content))
    return resp.content


def _blob_service():
    from azure.storage.blob.aio import BlobServiceClient

    if settings.azure_blob_connection_string:
        logger.debug("Using connection string for blob storage")
        return BlobServiceClient.from_connection_string(settings.azure_blob_connection_string)
    # Use managed identity (DefaultAzureCredential)
    from azure.identity.aio import DefaultAzureCredential

    logger.debug("Using DefaultAzureCredential for blob storage account=%s", settings.azure_storage_account_name)
    account_url = f"https://{settings.azure_storage_account_name}.blob.core.windows.net"
    return BlobServiceClient(account_url, credential=DefaultAzureCredential())


async def upload_to_blob(data: bytes, blob_name: str) -> str:
    async with _blob_service() as bsc:
        container = bsc.get_container_client(settings.azure_blob_container_name)
        blob = container.get_blob_client(blob_name)
        await blob.upload_blob(data, overwrite=True)
        logger.info("Uploaded blob %s", blob_name)
        return blob.url


async def delete_blob(blob_name: str) -> None:
    try:
        async with _blob_service() as bsc:
            container = bsc.get_container_client(settings.azure_blob_container_name)
            await container.delete_blob(blob_name)
            logger.info("Deleted blob %s", blob_name)
    except Exception:
        logger.warning("Failed to delete blob %s", blob_name, exc_info=True)


def extract_text(data: bytes) -> str:
    """The text of the PDF's pages that hold any, joined by two line breaks."""
    import pymupdf

    doc = pymupdf.open(stream=data, filetype="pdf")
    pages: list[str] = []
    try:
        for page in doc:
            text = page.get_text()
            if text.strip():
                pages.append(text)
    finally:
        doc.close()
    full_text = "\n\n".join(pages)
    logger.info("Extracted %d chars from a PDF of %d bytes", len(full_text), len(data))
    return full_text
