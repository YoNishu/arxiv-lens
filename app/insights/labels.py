"""Topic names from the model endpoint, asked one topic at a time.

A topic's card (its heaviest terms and the papers nearest to it) becomes a short prompt, and the
answer gives a name and a one-sentence blurb. A topic the model could not name is named after its
first three terms; the `source` of each topic in topics.json says which of the two it got.
"""
import json
import logging

from app.insights import work

logger = logging.getLogger(__name__)

TIMEOUT = 60.0
NAME_MAX = 80
BLURB_MAX = 300

SYSTEM = ('You name research topics for a paper library. Reply with a JSON object with two keys: '
          '"name", a title of at most six words, and "blurb", one sentence that says what the papers are about.')


def _one_line(title: str) -> str:
    return title.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")


def prompt(card: dict) -> list[dict]:
    """The two messages of the request for one card; nothing but the card goes into them."""
    papers = "".join("- " + _one_line(exemplar["title"]) + "\n" for exemplar in card["exemplars"])
    user = "Terms: " + ", ".join(card["terms"]) + "\nPapers:\n" + papers
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def parse(content) -> dict | None:
    """The name and blurb of an answer, white space squeezed; None for anything unusable."""
    if not isinstance(content, str):
        return None
    try:
        value = json.loads(content)
    except ValueError:
        return None
    if not isinstance(value, dict):
        return None
    name, blurb = value.get("name"), value.get("blurb")
    if not isinstance(name, str) or not isinstance(blurb, str):
        return None
    name = " ".join(name.split())
    blurb = " ".join(blurb.split())
    if 1 <= len(name) <= NAME_MAX and len(blurb) <= BLURB_MAX:
        return {"name": name, "blurb": blurb}
    return None


def fallback(card: dict) -> dict:
    return {"name": " / ".join(card["terms"][:3]), "blurb": "", "source": "fallback"}


def connect():
    """A client for the endpoint of the application settings, or None when no request can be sent."""
    from app.config import settings

    if not settings.azure_openai_endpoint:
        return None
    import openai

    try:
        return openai.AzureOpenAI(
            azure_endpoint=settings.azure_openai_endpoint,
            api_key=settings.azure_openai_api_key,
            api_version=settings.azure_openai_api_version,
            max_retries=0,
            timeout=TIMEOUT,
        )
    except Exception as exc:
        logger.warning("topic names cannot be asked of %s: %s", settings.azure_openai_endpoint, exc)
        return None


def ask(client, card: dict) -> dict | None:
    """One request for one card. A failing endpoint gives None; the build goes on without that name."""
    from app.config import settings

    work.count("labels.asked")
    try:
        response = client.chat.completions.create(
            model=settings.azure_openai_deployment,
            messages=prompt(card),
            response_format={"type": "json_object"},
            temperature=0,
        )
        content = response.choices[0].message.content
    except Exception as exc:
        logger.info("no topic name from the model endpoint: %s", exc)
        return None
    return parse(content)


def name_topics(cards: list[dict]) -> list[dict]:
    """A name, blurb and source for every card, in the order of the cards."""
    client = connect()
    names: list[dict] = []
    try:
        for card in cards:
            answer = ask(client, card) if client is not None else None
            if answer is None:
                names.append(fallback(card))
            else:
                names.append({"name": answer["name"], "blurb": answer["blurb"], "source": "model"})
    finally:
        if client is not None:
            client.close()
    unnamed = len([name for name in names if name["source"] != "model"])
    if unnamed:
        logger.warning("%s of %s topics are named after their terms", unnamed, len(names))
    return names
