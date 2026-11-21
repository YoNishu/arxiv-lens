"""The model's analysis of one paper: the prompt, the request and the check of the answer.

Whatever answers (the model endpoint over HTTP, or a corpus in-process), the analysis goes
through checked() before it is stored, so both give the same values and fail with the same
words.
"""
import json
import logging

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


class ModelError(RuntimeError):
    """The model endpoint gave no answer, or an error status instead of one."""


def _get_client():
    # The SDK itself repeats a request that was rate-limited or hit a server error (twice, with
    # backoff). Anything beyond that fails this try of the paper, and a later cycle tries the
    # paper again (see app/services/processor.py) — there is no second retry loop in here.
    from openai import AsyncAzureOpenAI

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


def checked(data) -> dict:
    """The analysis as it is stored: validated, the score a whole number from 1 to 10."""
    validate_analysis(data)
    # A copy: an in-process source hands over its own object, which must stay as it is.
    data = dict(data)
    data["novelty_score"] = max(1, min(10, int(data["novelty_score"])))
    data.setdefault("limitations", None)
    return data


async def analyze_paper(arxiv_id: str, version: int, title: str, abstract: str, full_text: str) -> dict:
    import openai

    truncated = full_text[:MAX_TEXT_CHARS]
    if len(full_text) > MAX_TEXT_CHARS:
        logger.warning("Truncated text from %d to %d chars", len(full_text), MAX_TEXT_CHARS)

    # The first line names the paper and its version, so an answering service never has to
    # recognise a paper by its title.
    user_content = (
        f"arXiv: {arxiv_id}v{version}\nPaper title: {title}\n\nAbstract: {abstract}\n\n"
        f"Full text:\n{truncated}"
    )

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
    except openai.APIStatusError as e:
        raise ModelError(f"Model request failed with status {e.status_code}") from e
    except openai.APIConnectionError as e:
        raise ModelError(f"Model request failed: {type(e).__name__}") from e
    finally:
        await client.close()

    raw = resp.choices[0].message.content
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        raise InvalidAnalysis("Analysis is not valid JSON") from None
    data = checked(data)
    logger.info("Analysis complete for %sv%d, score %d", arxiv_id, version, data["novelty_score"])
    return data
