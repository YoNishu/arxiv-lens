"""Runs several command lines in one Python process and prints what each one did, as JSON."""
import contextlib
import io
import json
import sys
import traceback

from app import cli


def _one(argv: list[str]) -> dict:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            status = cli.main(argv)
        except SystemExit as e:
            status = e.code if isinstance(e.code, int) else (0 if e.code is None else 2)
        except Exception:
            # What the command would have done as a process of its own: a traceback and status 1.
            traceback.print_exc()
            status = 1
    return {"status": status, "stdout": out.getvalue(), "stderr": err.getvalue()}


def main() -> int:
    commands = json.loads(sys.argv[1])
    results = [_one(list(argv)) for argv in commands]
    sys.stdout.write(json.dumps(results) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
