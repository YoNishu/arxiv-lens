"""ArxivLens: polls arXiv feeds, analyses each paper with a model and shows what it found.

The failure switches of app.faults are armed here, before anything else of the application is
imported, so that they see every file-system change and commit of the process.
"""
import os

if os.environ.get("ARXIVLENS_KILL_AT"):
    from app import faults

    faults.install()
