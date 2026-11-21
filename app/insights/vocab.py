"""Stage 3: which terms count, and how much.

The vocabulary is fitted on the reference papers: those announced before the first day of the
month the build runs in, or the whole library while that would be too few. Only the terms of a
paper's first stored title and abstract are read. A term is kept when it is neither too rare nor
too common; its idf weighs it in the vectors. vocab.json shows the reference and every kept term
with its document frequency and idf.
"""
import logging
from datetime import date

from app.insights import asof, canon, work

logger = logging.getLogger(__name__)

STAGE_VERSION = 1


def reference_ids(papers: list[dict], month_start: date, min_reference: int) -> tuple[list[str], bool]:
    """Ids of the reference papers, ascending, and whether they are the whole library."""
    before = sorted(paper["arxiv_id"] for paper in papers if asof.day_of(paper["announced"]) < month_start)
    if len(before) >= min_reference:
        return before, False
    return sorted(paper["arxiv_id"] for paper in papers), True


def fit(docs: list[dict[str, int]], min_df: int, max_df_ratio: float) -> dict[str, tuple[int, float]]:
    """Kept terms with their document frequency and idf, in ascending term order."""
    work.count("vocab.fits")
    n = len(docs)
    df: dict[str, int] = {}
    for doc in docs:
        for term in doc:
            df[term] = df.get(term, 0) + 1
    ceiling = max_df_ratio * n
    kept: dict[str, tuple[int, float]] = {}
    for term in sorted(df):
        k = df[term]
        if k >= min_df and k <= ceiling:
            kept[term] = (k, (canon.ln(n + 1) - canon.ln(k + 1)) + 1.0)
    return kept


def run(ctx) -> dict[str, int]:
    section = ctx.settings.vocab
    reference, whole_library = reference_ids(ctx.papers, ctx.asof.month_start, section.min_reference)
    terms = fit([ctx.tokens[i]["first_terms"] for i in reference], section.min_df, section.max_df_ratio)
    ctx.write_json("vocab.json", {
        "reference": {"month": ctx.asof.month, "papers": len(reference), "whole_library": whole_library},
        "terms": {term: {"df": df, "idf": idf} for term, (df, idf) in terms.items()},
    })
    ctx.reference = reference
    ctx.idf = {term: idf for term, (_, idf) in terms.items()}
    return {"papers": len(reference), "terms": len(terms)}
