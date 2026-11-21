"""Settings of the insights build: what each stage can be tuned with, and the defaults.

Values come in three layers, later ones winning key by key: the defaults below, insights.yaml
of the project, and insights.yaml in the home folder. Each file is checked strictly, so a
mistyped key or value stops the command instead of being ignored. dump() writes the settings in
the same YAML form a file uses, and digest() names a set of settings with one hash.
"""
import math
import os
import re
from dataclasses import dataclass, field, fields
from decimal import Decimal

import yaml

from app import home
from app.insights import canon


@dataclass(frozen=True)
class TextSettings:
    extra_stopwords: tuple[str, ...] = ()
    min_token_len: int = 3


@dataclass(frozen=True)
class VocabSettings:
    min_df: int = 3
    max_df_ratio: float = 0.4
    min_reference: int = 100


@dataclass(frozen=True)
class SimilarSettings:
    top_k: int = 10
    probe_terms: int = 8
    min_shared: int = 3
    min_sim: float = 0.12


@dataclass(frozen=True)
class CitationSettings:
    damping: float = 0.85
    list_size: int = 5


@dataclass(frozen=True)
class TopicSettings:
    iterations: int = 12
    min_topic_sim: float = 0.08
    card_terms: int = 10
    exemplars: int = 5


@dataclass(frozen=True)
class TrendSettings:
    recent_weeks: int = 4
    baseline_weeks: int = 8
    top: int = 15


@dataclass(frozen=True)
class RankingSettings:
    novelty: float = 0.6
    grounding: float = 0.25
    heat: float = 0.15


@dataclass(frozen=True)
class DigestSettings:
    top: int = 10


@dataclass(frozen=True)
class Settings:
    text: TextSettings = field(default_factory=TextSettings)
    vocab: VocabSettings = field(default_factory=VocabSettings)
    similar: SimilarSettings = field(default_factory=SimilarSettings)
    citations: CitationSettings = field(default_factory=CitationSettings)
    topics: TopicSettings = field(default_factory=TopicSettings)
    trends: TrendSettings = field(default_factory=TrendSettings)
    ranking: RankingSettings = field(default_factory=RankingSettings)
    digests: DigestSettings = field(default_factory=DigestSettings)


# The sections in the order a file and dump() list them.
_SECTIONS = (("text", TextSettings), ("vocab", VocabSettings), ("similar", SimilarSettings),
             ("citations", CitationSettings), ("topics", TopicSettings), ("trends", TrendSettings),
             ("ranking", RankingSettings), ("digests", DigestSettings))

_DEFAULTS = {name: {f.name: f.default for f in fields(cls)} for name, cls in _SECTIONS}

# Allowed values: (lowest, lowest itself allowed, highest, highest itself allowed); None is no bound.
_RANGES = {
    ("text", "min_token_len"): (1, True, 20, True),
    ("vocab", "min_df"): (1, True, None, True),
    ("vocab", "max_df_ratio"): (0, False, 1, True),
    ("vocab", "min_reference"): (1, True, None, True),
    ("similar", "top_k"): (1, True, 100, True),
    ("similar", "probe_terms"): (1, True, 100, True),
    ("similar", "min_shared"): (1, True, 100, True),
    ("similar", "min_sim"): (0, True, 1, True),
    ("citations", "damping"): (0, False, 1, False),
    ("citations", "list_size"): (1, True, 50, True),
    ("topics", "iterations"): (1, True, 100, True),
    ("topics", "min_topic_sim"): (0, True, 1, True),
    ("topics", "card_terms"): (1, True, 50, True),
    ("topics", "exemplars"): (0, True, 20, True),
    ("trends", "recent_weeks"): (1, True, 52, True),
    ("trends", "baseline_weeks"): (1, True, 104, True),
    ("trends", "top"): (1, True, 100, True),
    ("ranking", "novelty"): (0, True, None, True),
    ("ranking", "grounding"): (0, True, None, True),
    ("ranking", "heat"): (0, True, None, True),
    ("digests", "top"): (1, True, 100, True),
}

_WORD = re.compile(r"[a-z][a-z0-9-]*")


def load() -> Settings:
    """The settings in force: defaults, then the project's file, then the home's file."""
    values = {name: dict(keys) for name, keys in _DEFAULTS.items()}
    for path in (home.ROOT / "insights.yaml", home.settings_file()):
        if os.path.isfile(path):
            _apply(values, os.path.abspath(path))
    return Settings(**{name: cls(**values[name]) for name, cls in _SECTIONS})


def as_dict(settings: Settings) -> dict:
    """All eight sections with all their keys, as plain JSON values."""
    return {name: {f.name: _plain(name, f.name, getattr(getattr(settings, name), f.name)) for f in fields(cls)}
            for name, cls in _SECTIONS}


def digest(settings: Settings) -> str:
    return canon.sha256_json(as_dict(settings))


def dump(settings: Settings) -> str:
    """The settings as YAML that load() reads back to the same values.

    Written by hand rather than with yaml.dump: its output is not fixed from one PyYAML to the
    next, and a few values have to be spelled so that YAML reads them back as they were.
    """
    plain = as_dict(settings)
    lines = []
    for name, cls in _SECTIONS:
        lines.append(name + ":")
        for f in fields(cls):
            lines.append("  %s: %s" % (f.name, _yaml_value(plain[name][f.name])))
    return "\n".join(lines) + "\n"


def _apply(values: dict, path: str) -> None:
    """Check one settings file and lay its values over those collected so far."""
    with open(path, "rb") as f:
        data = f.read()
    try:
        document = yaml.safe_load(data)
    except yaml.YAMLError:
        _refuse(path, "not valid YAML")
    if document is None:
        return
    if not isinstance(document, dict):
        _refuse(path, "not a mapping")
    for section, keys in document.items():
        if section not in values:
            _refuse(path, "unknown section %s" % section)
        if keys is None:
            continue
        if not isinstance(keys, dict):
            _refuse(path, "not a mapping")
        for key, value in keys.items():
            if key not in values[section]:
                _refuse(path, "unknown setting %s.%s" % (section, key))
            values[section][key] = _checked(path, section, key, value)


def _checked(path: str, section: str, key: str, value):
    name = "%s.%s" % (section, key)
    default = _DEFAULTS[section][key]
    if isinstance(default, tuple):
        if not isinstance(value, list) or not all(isinstance(w, str) and _WORD.fullmatch(w) for w in value):
            # Unquoted no, yes, on, off, true, false and null are not strings to YAML and end here.
            _refuse(path, name + ": expected a list of words")
        return tuple(sorted(set(value)))
    if isinstance(default, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            _refuse(path, name + ": expected a number")
        try:
            value = float(value)
        except OverflowError:
            _refuse(path, name + ": out of range")
        if not math.isfinite(value):
            _refuse(path, name + ": out of range")
    elif isinstance(value, bool) or not isinstance(value, int):
        _refuse(path, name + ": expected an integer")
    low, low_allowed, high, high_allowed = _RANGES[(section, key)]
    if low is not None and (value < low or (value == low and not low_allowed)):
        _refuse(path, name + ": out of range")
    if high is not None and (value > high or (value == high and not high_allowed)):
        _refuse(path, name + ": out of range")
    return value


def _plain(section: str, key: str, value):
    default = _DEFAULTS[section][key]
    if isinstance(default, tuple):
        return list(value)
    if isinstance(default, float):
        return float(value)
    return value


def _yaml_value(value) -> str:
    if isinstance(value, list):
        if not value:
            return "[]"
        return "[" + ", ".join('"%s"' % word for word in value) + "]"
    if isinstance(value, float):
        text = repr(value)
        if "e" in text:
            # YAML reads 1e-05 as a string; written out in full it stays a number.
            text = format(Decimal(text), "f")
        return text
    return str(value)


def _refuse(path: str, problem: str):
    from app.cli import CliError

    raise CliError("%s: %s" % (path, problem), 2)
