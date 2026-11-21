"""Stage 2: the terms of every paper and the arXiv ids its reference list names.

Terms are words and pairs of neighbouring words, folded to plain lower-case ASCII and stemmed, taken
from the current title, abstract and keywords and from the first stored title and abstract; they are
in text/tokens.jsonl. For a paper in state analysed the stored text is read and the list under its
last References or Bibliography heading is cut into entries; text/refs.jsonl holds the ids found in
them, the number of entries, the number of entries without an id and the hash of the text read.
"""
import functools
import hashlib
import re
import unicodedata
from pathlib import Path

from app.insights import work

STAGE_VERSION = 1

SOFT = re.compile(r"(?<=[a-z][a-z])-\n(?=[a-z])")
TEX = re.compile(r"\$[^$]*\$")
MARKS = re.compile("[\u0300-\u036f]")
SENTENCE_END = re.compile(r"[.!?;:](?:[ \n]+|$)")
TOKEN = re.compile(r"(?<![a-z0-9])[a-z][a-z0-9]*(?:-[a-z0-9]+)*")

NEW_ID = re.compile(r"(?<![0-9.])([0-9]{2})([0-9]{2})\.([0-9]{4,5})(?:v[0-9]{1,2})?(?![0-9])")
OLD_ID = re.compile(r"(?<![A-Za-z0-9.-])([a-z]+(?:-[a-z]+)*)/([0-9]{2})([0-9]{2})([0-9]{3})(?:v[0-9]{1,2})?(?![0-9])")

HEADINGS = ("references", "bibliography")

# Words the stemmer leaves alone: their last letters look like a plural ending but are not one.
_UNSTEMMED = ("series", "species")
_UNDOUBLE = "bdgmnprt"
_MONTHS = frozenset("%02d" % month for month in range(1, 13))


@functools.lru_cache(maxsize=None)
def _bundled_stopwords() -> frozenset[str]:
    data = Path(__file__).with_name("stopwords.txt").read_bytes().decode("ascii")
    return frozenset(line for line in data.split("\n") if line)


def load_stopwords() -> frozenset[str]:
    """The stop words that ship with the project (stopwords.txt), read once per process."""
    return _bundled_stopwords()


def normalise(text: str) -> str:
    """One block of text as plain lower-case characters, ready to be cut into sentences and tokens."""
    # Soft hyphens first, while the line ends that mark them are still where the text had them.
    text = SOFT.sub("", text)
    text = TEX.sub(" ", text)
    text = MARKS.sub("", unicodedata.normalize("NFKD", text))
    return text.lower()


def _undouble(rest: str) -> str:
    if len(rest) >= 2 and rest[-1] == rest[-2] and rest[-1] in _UNDOUBLE:
        return rest[:-1]
    return rest


def _stem_part(part: str) -> str:
    if part in _UNSTEMMED:
        return part
    if part.endswith("sses") and len(part) - 4 >= 2:
        return part[:-4] + "ss"
    if part.endswith("ies") and len(part) - 3 >= 3:
        return part[:-3] + "y"
    if part.endswith("ings") and len(part) - 4 >= 4:
        return _undouble(part[:-4])
    if part.endswith("ing") and len(part) - 3 >= 4:
        return _undouble(part[:-3])
    if part.endswith("ed") and len(part) - 2 >= 4:
        return _undouble(part[:-2])
    if part.endswith("es"):
        rest = part[:-2]
        if len(rest) >= 3 and rest.endswith(("s", "x", "z", "ch", "sh")):
            return rest
    if part.endswith("s") and len(part) - 1 >= 3 and not part.endswith(("ss", "us", "is", "as")):
        return part[:-1]
    return part


def stem(token: str) -> str:
    """The token with the part after its last hyphen stemmed by the first rule that applies."""
    head, hyphen, part = token.rpartition("-")
    return head + hyphen + _stem_part(part)


def sentences(text: str, stopwords: frozenset[str], min_len: int) -> list[list[str]]:
    """The kept, stemmed tokens of each sentence of one block; sentences that keep none are left out."""
    result = []
    for piece in SENTENCE_END.split(normalise(text)):
        kept = [stem(token) for token in TOKEN.findall(piece) if len(token) >= min_len and token not in stopwords]
        if kept:
            result.append(kept)
    return result


def count_terms(blocks: list[str], stopwords: frozenset[str], min_len: int) -> dict[str, int]:
    """How often each term occurs in the blocks: single tokens, and neighbours within a sentence."""
    counts: dict[str, int] = {}
    for block in blocks:
        for kept in sentences(block, stopwords, min_len):
            previous = None
            for token in kept:
                counts[token] = counts.get(token, 0) + 1
                if previous is not None:
                    pair = previous + " " + token
                    counts[pair] = counts.get(pair, 0) + 1
                previous = token
    return {term: counts[term] for term in sorted(counts)}


def tokens_row(paper: dict, stopwords: frozenset[str], min_len: int) -> dict:
    """The row of text/tokens.jsonl for one paper."""
    work.count("text.tokenized")
    blocks = [paper["title"], paper["abstract"]]
    if paper["keywords"] is not None:
        # Each keyword stands alone, so no pair joins two keywords or the abstract to a keyword.
        blocks.extend(paper["keywords"])
    return {
        "arxiv_id": paper["arxiv_id"],
        "terms": count_terms(blocks, stopwords, min_len),
        "first_terms": count_terms([paper["first_title"], paper["first_abstract"]], stopwords, min_len),
    }


def read_text(arxiv_id: str, version: int) -> str | None:
    """The stored text of one version of a paper, or None when there is no readable file."""
    from app.services import textstore

    work.count("text.read")
    return textstore.read(arxiv_id, version)


def find_id(entry: str) -> str | None:
    """The arXiv id, without version, that one reference entry names; None when it names none."""
    found = None
    for match in NEW_ID.finditer(entry):
        if match.group(2) in _MONTHS:
            found = (match.start(), match.group(1) + match.group(2) + "." + match.group(3))
            break
    for match in OLD_ID.finditer(entry):
        if match.group(3) in _MONTHS:
            if found is None or match.start() < found[0]:
                found = (match.start(), match.group(1) + "/" + match.group(2) + match.group(3) + match.group(4))
            break
    return None if found is None else found[1]


def _marker(style: str, number: int) -> str:
    return ("[%d] " if style == "bracket" else "%d. ") % number


def split_entries(text: str) -> list[str]:
    """The entries of the reference list of a text, each as one line of text without its marker."""
    lines = text.split("\n")
    heading = None
    for index, line in enumerate(lines):
        if line.strip(" ").lower() in HEADINGS:
            heading = index
    if heading is None:
        return []
    body = [line.strip(" ") for line in lines[heading + 1:]]
    body = [line for line in body if line]
    if not body:
        return []
    if body[0].startswith(_marker("bracket", 1)):
        style = "bracket"
    elif body[0].startswith(_marker("dot", 1)):
        style = "dot"
    else:
        return []
    entries: list[str] = []
    for line in body:
        marker = _marker(style, len(entries) + 1)
        if line.startswith(marker):
            entries.append(line[len(marker):])
        else:
            # A number out of sequence is not a marker: such a line continues the entry before it.
            entries[-1] = entries[-1] + " " + line
    return entries


def references(text: str, own_id: str) -> dict:
    """What the reference list of a text names: the number of entries, the ids in order, the rest."""
    work.count("text.parsed")
    entries = split_entries(text)
    cites: list[str] = []
    seen: set[str] = set()
    other = 0
    for entry in entries:
        found = find_id(entry)
        if found is None:
            other += 1
        elif found != own_id and found not in seen:
            seen.add(found)
            cites.append(found)
    return {"entries": len(entries), "cites": cites, "other": other}


def refs_row(paper: dict, text: str | None) -> dict:
    """The row of text/refs.jsonl for one analysed paper, from the text that was read for it."""
    if text is None:
        return {"arxiv_id": paper["arxiv_id"], "text_sha256": None, "entries": 0, "cites": [], "other": 0}
    found = references(text, paper["arxiv_id"])
    return {
        "arxiv_id": paper["arxiv_id"],
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "entries": found["entries"],
        "cites": found["cites"],
        "other": found["other"],
    }


def run(ctx) -> dict[str, int]:
    settings = ctx.settings.text
    stopwords = load_stopwords() | frozenset(settings.extra_stopwords)
    min_len = settings.min_token_len
    token_rows = []
    refs_rows = []
    entries = arxiv_refs = other_refs = 0
    for paper in ctx.papers:
        row = tokens_row(paper, stopwords, min_len)
        token_rows.append(row)
        ctx.tokens[paper["arxiv_id"]] = {"terms": row["terms"], "first_terms": row["first_terms"]}
        if paper["state"] == "analysed":
            refs = refs_row(paper, read_text(paper["arxiv_id"], paper["version"]))
            refs_rows.append(refs)
            ctx.refs[paper["arxiv_id"]] = refs
            entries += refs["entries"]
            arxiv_refs += len(refs["cites"])
            other_refs += refs["other"]
    ctx.write_jsonl("text/tokens.jsonl", token_rows)
    ctx.write_jsonl("text/refs.jsonl", refs_rows)
    return {
        "papers": len(token_rows),
        "texts": len(refs_rows),
        "entries": entries,
        "arxiv_refs": arxiv_refs,
        "other_refs": other_refs,
    }
