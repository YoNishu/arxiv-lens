"""Canonical bytes of everything an insights build writes.

One JSON form (compact, keys sorted, raw UTF-8), three writers that build a whole file in memory
and hand it to home.write_file in one piece, SHA-256 helpers, and ln(), the natural logarithm
of a whole number computed with decimal arithmetic so that it is the same double on every
machine and every Python.
"""
import hashlib
import json
from decimal import ROUND_HALF_EVEN, Context, Decimal

from app import home

_LN_CONTEXT = Context(prec=40, rounding=ROUND_HALF_EVEN)
_ln_cache: dict[int, float] = {}


def dumps(value) -> bytes:
    """The canonical JSON of a value, without a line feed. NaN and infinities raise."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def write_json(path, value) -> tuple[str, int]:
    """A .json file: one canonical line. Returns the SHA-256 and the size of the bytes written."""
    return _write(path, dumps(value) + b"\n")


def write_jsonl(path, rows: list[dict]) -> tuple[str, int]:
    """A .jsonl file: one canonical line per row, in the order given; no rows give an empty file."""
    return _write(path, b"".join(dumps(row) + b"\n" for row in rows))


def write_text(path, text: str) -> tuple[str, int]:
    """A UTF-8 text file. The text must end with exactly one line feed."""
    if not text.endswith("\n") or text.endswith("\n\n"):
        raise ValueError("a text file must end with exactly one line feed")
    return _write(path, text.encode("utf-8"))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(value) -> str:
    return sha256_bytes(dumps(value))


def ln(k: int) -> float:
    """Natural logarithm of a whole number k >= 1, correctly rounded and kept for the process."""
    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise ValueError("ln is taken of whole numbers of 1 or more, not %r" % (k,))
    value = _ln_cache.get(k)
    if value is None:
        value = float(_LN_CONTEXT.ln(Decimal(k)))
        _ln_cache[k] = value
    return value


def _write(path, data: bytes) -> tuple[str, int]:
    home.write_file(path, data)
    return sha256_bytes(data), len(data)
