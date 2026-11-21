"""Runs one command through app.cli.main and prints its status and the heavy packages it imported."""
import contextlib
import io
import json
import sys

HEAVY = ("pymupdf", "openai", "azure", "fastmcp", "arxiv", "feedparser", "fastapi")


def main(argv: list[str]) -> int:
    from app import cli

    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        try:
            status = cli.main(argv)
        except SystemExit as e:
            status = e.code if isinstance(e.code, int) else 2
    loaded = sorted({name.split(".")[0] for name in sys.modules if name.split(".")[0] in HEAVY})
    print(json.dumps({"status": status, "heavy": loaded, "stdout": captured.getvalue()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
