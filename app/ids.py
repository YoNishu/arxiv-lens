"""Small helpers for arXiv identifiers and category codes.

They live in a module of their own because the database layer, the services and the command
line all need them, and none of those should import another for it.
"""
import re

# Digits spelled out: \d would also take the digits of other scripts, which no arXiv id holds.
_VERSIONED_ID = re.compile(r"^(?P<base>.+?)v(?P<version>[0-9]+)$")
_CATEGORY_CODE = re.compile(r"[a-z][a-z-]*(\.[A-Za-z-]+)?")


def split(raw: str) -> tuple[str, int | None]:
    """'2401.90001v3' -> ('2401.90001', 3); '2401.90001' -> ('2401.90001', None)."""
    raw = (raw or "").strip()
    m = _VERSIONED_ID.match(raw)
    if m:
        return m.group("base"), int(m.group("version"))
    return raw, None


def file_stem(arxiv_id: str) -> str:
    """The id as part of a file name: old-style ids hold a slash ('hep-th/9901901')."""
    return arxiv_id.replace("/", "_")


def is_category_code(code: str) -> bool:
    """True for codes such as cs.AI, stat.ML, hep-th and astro-ph.CO."""
    return _CATEGORY_CODE.fullmatch(code.strip()) is not None
