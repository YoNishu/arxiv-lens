import json
import logging

from openai import AsyncAzureOpenAI

from app.config import settings

logger = logging.getLogger(__name__)

MAX_TEXT_CHARS = 120_000

SYSTEM_PROMPT = """You are an expert academic paper analyst. Given the full text of a research paper, produce a structured JSON analysis.

Scoring rubric for novelty_score (1-10):
- 1-3: Incremental. Minor extensions of known methods. Limited new insight.
- 4-5: Solid. Useful engineering contribution or thorough evaluation. Standard methodology.
- 6-7: Significant. Novel approach, strong experimental results, or meaningful theoretical contribution.
- 8-9: Highly novel. Potential to shift research direction. New paradigm, surprising results, or major capability leap.
- 10: Landmark. Fundamentally redefines the field. Rare — reserve for truly exceptional work.

You MUST output valid JSON with exactly these keys:
{
  "summary": "3-5 sentence plain-English summary of what the paper does, its core idea, and main result",
  "key_contributions": ["contribution 1", "contribution 2"],
  "methodology": "Description of the approach, architecture, training procedure, or proof technique",
  "limitations": "Key limitations, assumptions, or scope constraints acknowledged or apparent",
  "keywords": ["keyword1", "keyword2"],
  "novelty_score": 7
}
"""


class InvalidAnalysis(ValueError):
    """The model answered, but not with an analysis of the agreed shape."""


def _get_client() -> AsyncAzureOpenAI:
    # The SDK itself repeats a request that was rate-limited or hit a server error (twice, with
    # backoff). Anything beyond that fails this try of the paper, and a later cycle tries the
    # paper again (see app/services/processor.py) — there is no second retry loop in here.
    return AsyncAzureOpenAI(
        azure_endpoint=settings.azure_openai_endpoint,
        api_key=settings.azure_openai_api_key,
        api_version=settings.azure_openai_api_version,
    )


def _is_str_list(value) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def validate_analysis(data) -> None:
    """Raise InvalidAnalysis unless every field is there and has the type the prompt asks for."""
    if not isinstance(data, dict):
        raise InvalidAnalysis(f"Analysis is not a JSON object but {type(data).__name__}")

    required = {"summary", "key_contributions", "methodology", "keywords", "novelty_score"}
    if not required.issubset(data.keys()):
        raise InvalidAnalysis(f"Missing keys: {sorted(required - data.keys())}")

    problems: list[str] = []
    for key in ("summary", "methodology"):
        if not isinstance(data[key], str):
            problems.append(f"{key} must be a string, got {type(data[key]).__name__}")
    for key in ("key_contributions", "keywords"):
        if not _is_str_list(data[key]):
            problems.append(f"{key} must be a list of strings, got {type(data[key]).__name__}")
    limitations = data.get("limitations")
    if limitations is not None and not isinstance(limitations, str):
        problems.append(f"limitations must be a string or null, got {type(limitations).__name__}")
    score = data["novelty_score"]
    is_whole_number = (isinstance(score, int) and not isinstance(score, bool)) or (
        isinstance(score, float) and score.is_integer()
    )
    if not is_whole_number:
        problems.append(f"novelty_score must be an integer, got {type(score).__name__}")
    if problems:
        raise InvalidAnalysis("Analysis has fields of the wrong type: " + "; ".join(problems))


async def analyze_paper(full_text: str, title: str = "", abstract: str = "") -> dict:
    truncated = full_text[:MAX_TEXT_CHARS]
    if len(full_text) > MAX_TEXT_CHARS:
        logger.warning("Truncated text from %d to %d chars", len(full_text), MAX_TEXT_CHARS)

    user_content = f"Paper title: {title}\n\nAbstract: {abstract}\n\nFull text:\n{truncated}"

    client = _get_client()
    try:
        resp = await client.chat.completions.create(
            model=settings.azure_openai_deployment,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            response_format={"type": "json_object"},
            temperature=0.3,
        )
    finally:
        await client.close()
    raw = resp.choices[0].message.content
    data = json.loads(raw)
    validate_analysis(data)

    data["novelty_score"] = max(1, min(10, int(data["novelty_score"])))
    data.setdefault("limitations", None)
    logger.info("Analysis complete for '%s' — score %d", title[:60], data["novelty_score"])
    return data
