"""Insights frame: settings, as-of values, canonical writers, ln, work counters and pruning."""
import argparse
import builtins
import dataclasses
import hashlib
import os
from datetime import date, timedelta

import pytest

from app import home
from app.insights import asof, canon, config, work
from app.insights import cli as insights_cli

DEFAULT_YAML = (
    "text:\n"
    "  extra_stopwords: []\n"
    "  min_token_len: 3\n"
    "vocab:\n"
    "  min_df: 3\n"
    "  max_df_ratio: 0.4\n"
    "  min_reference: 100\n"
    "similar:\n"
    "  top_k: 10\n"
    "  probe_terms: 8\n"
    "  min_shared: 3\n"
    "  min_sim: 0.12\n"
    "citations:\n"
    "  damping: 0.85\n"
    "  list_size: 5\n"
    "topics:\n"
    "  iterations: 12\n"
    "  min_topic_sim: 0.08\n"
    "  card_terms: 10\n"
    "  exemplars: 5\n"
    "trends:\n"
    "  recent_weeks: 4\n"
    "  baseline_weeks: 8\n"
    "  top: 15\n"
    "ranking:\n"
    "  novelty: 0.6\n"
    "  grounding: 0.25\n"
    "  heat: 0.15\n"
    "digests:\n"
    "  top: 10\n"
)

DEFAULT_JSON = (
    '{"citations":{"damping":0.85,"list_size":5},"digests":{"top":10},'
    '"ranking":{"grounding":0.25,"heat":0.15,"novelty":0.6},'
    '"similar":{"min_shared":3,"min_sim":0.12,"probe_terms":8,"top_k":10},'
    '"text":{"extra_stopwords":[],"min_token_len":3},'
    '"topics":{"card_terms":10,"exemplars":5,"iterations":12,"min_topic_sim":0.08},'
    '"trends":{"baseline_weeks":8,"recent_weeks":4,"top":15},'
    '"vocab":{"max_df_ratio":0.4,"min_df":3,"min_reference":100}}'
)

DEFAULT_DIGEST = "3fa1ecc15ba0293906b8cb8cb6f62a23110244dd998a74b6b8006c5c69230dc5"

# Written as a code point so that this file stays ASCII.
E_ACUTE = chr(0xE9)


@pytest.fixture
def lens_home(tmp_path, monkeypatch):
    """A home that does not exist yet; the code looks it up at every call."""
    path = tmp_path / "home"
    monkeypatch.setenv("ARXIVLENS_HOME", str(path))
    return path


def _put(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _parser():
    parser = argparse.ArgumentParser(prog="app")
    insights_cli.register(parser.add_subparsers(dest="command"))
    return parser


def _refusal(call):
    """Status and message of the error a call ends with."""
    with pytest.raises(Exception) as caught:
        call()
    error = caught.value
    assert type(error).__name__ == "CliError", repr(error)
    return getattr(error, "status", None), error.args[0]


def _tree(folder):
    """Relative paths of every file under a folder."""
    found = set()
    for parent, _dirs, names in os.walk(folder):
        for name in names:
            found.add(os.path.relpath(os.path.join(parent, name), folder).replace(os.sep, "/"))
    return found


def test_default_settings_and_layers(lens_home, tmp_path, monkeypatch, capsys):
    settings = config.load()
    assert config.dump(settings) == DEFAULT_YAML
    assert canon.dumps(config.as_dict(settings)).decode("utf-8") == DEFAULT_JSON
    assert config.digest(settings) == DEFAULT_DIGEST
    assert not lens_home.exists()

    project = (home.ROOT / "insights.yaml").read_bytes().decode("utf-8")
    first, second, rest = project.split("\n", 2)
    assert first.startswith("# ") and second.startswith("# ")
    assert rest == DEFAULT_YAML

    args = _parser().parse_args(["insights", "config"])
    assert args.func(args) == 0
    assert capsys.readouterr().out == DEFAULT_YAML

    # A file that says nothing changes nothing.
    for text in ("", "# only a comment\n", "text:\n", "text:\nsimilar:\n"):
        _put(lens_home / "insights.yaml", text)
        assert config.digest(config.load()) == DEFAULT_DIGEST, repr(text)

    root = tmp_path / "project"
    _put(root / "insights.yaml", "similar:\n  top_k: 20\n  min_shared: 4\ntrends:\n  top: 9\n")
    monkeypatch.setattr(home, "ROOT", root)
    settings = config.load()
    assert (settings.similar.top_k, settings.similar.min_shared, settings.trends.top) == (20, 4, 9)

    _put(lens_home / "insights.yaml", "similar:\n  top_k: 7\n")
    settings = config.load()
    assert settings.similar.top_k == 7
    assert settings.similar.min_shared == 4
    assert settings.trends.top == 9
    assert settings.similar.probe_terms == 8
    assert settings.similar.min_sim == 0.12


def test_settings_round_trip_through_their_yaml_form(lens_home):
    _put(lens_home / "insights.yaml",
         "text:\n  extra_stopwords: [zeta, alpha, zeta]\n"
         "similar:\n  min_sim: 1\n"
         "topics:\n  min_topic_sim: 0.00001\n"
         "ranking:\n  novelty: 1.0e+16\n")
    settings = config.load()
    assert settings.text.extra_stopwords == ("alpha", "zeta")
    assert settings.similar.min_sim == 1.0 and isinstance(settings.similar.min_sim, float)
    assert settings.topics.min_topic_sim == 0.00001
    text = config.dump(settings)
    assert '  extra_stopwords: ["alpha", "zeta"]\n' in text
    assert "  min_sim: 1.0\n" in text
    assert "  min_topic_sim: 0.00001\n" in text
    assert "  novelty: 10000000000000000\n" in text
    assert "e-0" not in text and "e+" not in text
    assert "\t" not in text and " \n" not in text and "\n\n" not in text and "#" not in text

    others = [
        settings,
        config.Settings(),
        dataclasses.replace(config.Settings(), vocab=config.VocabSettings(min_df=1, max_df_ratio=1.0, min_reference=7)),
        dataclasses.replace(config.Settings(), citations=config.CitationSettings(damping=0.5, list_size=50),
                            text=config.TextSettings(extra_stopwords=("b-2", "on"), min_token_len=20)),
    ]
    for number, wanted in enumerate(others):
        _put(lens_home / "insights.yaml", config.dump(wanted))
        assert config.digest(config.load()) == config.digest(wanted), number

    _put(lens_home / "insights.yaml", DEFAULT_YAML.replace("  card_terms: 10\n", "  card_terms: 9\n"))
    assert config.load().topics.card_terms == 9
    _put(lens_home / "insights.yaml", DEFAULT_YAML.replace("  extra_stopwords: []\n", "  extra_stopwords: [zzzz]\n"))
    assert config.load().text.extra_stopwords == ("zzzz",)


def test_settings_files_that_are_refused(lens_home, tmp_path, capsys):
    rows = [
        ("text: [one\n", "not valid YAML"),
        ("- a\n- b\n", "not a mapping"),
        ("plain words\n", "not a mapping"),
        ("text: 3\n", "not a mapping"),
        ("words:\n  a: 1\n", "unknown section words"),
        ("text:\n  stop: [a]\n", "unknown setting text.stop"),
        ("vocab:\n  min_df: 2.5\n", "vocab.min_df: expected an integer"),
        ("vocab:\n  min_df: true\n", "vocab.min_df: expected an integer"),
        ("trends:\n  top: '9'\n", "trends.top: expected an integer"),
        ("similar:\n  min_sim: high\n", "similar.min_sim: expected a number"),
        ("similar:\n  min_sim: yes\n", "similar.min_sim: expected a number"),
        ("text:\n  extra_stopwords: [alpha, no]\n", "text.extra_stopwords: expected a list of words"),
        ("text:\n  extra_stopwords: [Alpha]\n", "text.extra_stopwords: expected a list of words"),
        ("text:\n  extra_stopwords: alpha\n", "text.extra_stopwords: expected a list of words"),
        ("text:\n  min_token_len: 0\n", "text.min_token_len: out of range"),
        ("text:\n  min_token_len: 21\n", "text.min_token_len: out of range"),
        ("vocab:\n  max_df_ratio: 0\n", "vocab.max_df_ratio: out of range"),
        ("citations:\n  damping: 1\n", "citations.damping: out of range"),
        ("topics:\n  exemplars: 21\n", "topics.exemplars: out of range"),
        ("ranking:\n  heat: -0.5\n", "ranking.heat: out of range"),
        ("ranking:\n  heat: .inf\n", "ranking.heat: out of range"),
    ]
    path = lens_home / "insights.yaml"
    for text, problem in rows:
        _put(path, text)
        status, message = _refusal(config.load)
        assert (status, message) == (2, "%s: %s" % (os.path.abspath(path), problem)), text
        assert "\n" not in message, text

    # The commands stop before anything is written.
    out = tmp_path / "out"
    parser = _parser()
    for argv in (["insights", "build", "--out", str(out)], ["insights", "config"]):
        args = parser.parse_args(argv)
        status, message = _refusal(lambda: args.func(args))
        assert status == 2 and message.endswith(": ranking.heat: out of range"), argv
        assert capsys.readouterr().out == "", argv
    assert not out.exists()
    assert _tree(lens_home) == {"insights.yaml"}


def test_as_of_values_and_week_labels():
    table = [
        (date(2026, 9, 10), date(2026, 9, 7), "2026-W37", date(2026, 9, 1), "2026-09"),
        (date(2026, 8, 31), date(2026, 8, 31), "2026-W36", date(2026, 8, 1), "2026-08"),
        (date(2026, 9, 6), date(2026, 8, 31), "2026-W36", date(2026, 9, 1), "2026-09"),
        (date(2027, 1, 3), date(2026, 12, 28), "2026-W53", date(2027, 1, 1), "2027-01"),
        (date(2027, 1, 4), date(2027, 1, 4), "2027-W01", date(2027, 1, 1), "2027-01"),
        (date(2026, 1, 1), date(2025, 12, 29), "2026-W01", date(2026, 1, 1), "2026-01"),
    ]
    for day, week_start, week, month_start, month in table:
        value = asof.of(day)
        assert (value.day, value.week_start, value.week, value.month_start, value.month) == (
            day, week_start, week, month_start, month), day
        assert asof.monday_of(day) == week_start, day

    # A week is closed exactly when every one of its days lies before the as-of day's week.
    value = asof.of(date(2026, 9, 10))
    for offset in range(-30, 30):
        day = date(2026, 9, 10) + timedelta(days=offset)
        closed = asof.monday_of(day) < value.week_start
        assert closed == (asof.monday_of(day) + timedelta(days=6) < value.week_start), day
        assert closed == (day < date(2026, 9, 7)), day

    mondays = []
    day = date(2026, 1, 5)
    while day.year < 2028:
        mondays.append(day)
        day += timedelta(days=7)
    labels = [asof.week_label(monday) for monday in mondays]
    for monday, label in zip(mondays, labels):
        assert asof.week_monday(label) == monday, label
    assert labels == sorted(labels)
    assert len(set(labels)) == len(labels)
    assert "2026-W53" in labels and asof.week_label(date(2025, 12, 29)) == "2026-W01"
    with pytest.raises(ValueError):
        asof.week_monday("2026-37")

    assert asof.day_of("2026-09-03") == date(2026, 9, 3)
    assert asof.day_of("2026-09-03T23:59:59Z") == date(2026, 9, 3)
    assert asof.month_label(date(2026, 1, 31)) == "2026-01"


def test_canonical_bytes():
    data = canon.dumps({"b": 1, "a": [0.5, E_ACUTE]})
    assert data == ('{"a":[0.5,"' + E_ACUTE + '"],"b":1}').encode("utf-8")
    assert len(data) == 22
    assert b"\xc3\xa9" in data
    assert canon.sha256_bytes(data) == "1db1b927104717cdd21c5b44ae8fd0ce4c5ceb51bd7701ee6b4df373b0a954d4"
    assert canon.sha256_json({"a": [0.5, E_ACUTE], "b": 1}) == canon.sha256_bytes(data)
    assert canon.dumps({"z": {"b": [{"d": 1, "c": 2}], "a": None}, "y": True}) == (
        b'{"y":true,"z":{"a":null,"b":[{"c":2,"d":1}]}}')
    assert canon.dumps([0.0, 1.0, 10]) == b"[0.0,1.0,10]"
    for bad in (float("nan"), float("inf"), {"x": [float("-inf")]}):
        with pytest.raises(ValueError):
            canon.dumps(bad)


def _counting(calls, name, real):
    def counted(*args, **kwargs):
        calls.append(name)
        return real(*args, **kwargs)

    return counted


def _file_system_calls(monkeypatch, action) -> list[str]:
    """The calls that change the file system while action runs."""
    calls = []
    real_open = builtins.open

    def counted_open(file, mode="r", *args, **kwargs):
        if isinstance(mode, str) and set(mode) & set("wax+"):
            calls.append("open")
        return real_open(file, mode, *args, **kwargs)

    with monkeypatch.context() as patch:
        for name in ("replace", "rename", "unlink", "remove", "rmdir", "mkdir"):
            patch.setattr(os, name, _counting(calls, name, getattr(os, name)))
        patch.setattr(builtins, "open", counted_open)
        action()
    return calls


def test_writers_write_whole_files(tmp_path, monkeypatch):
    folder = tmp_path / "out"
    folder.mkdir()

    digest, size = canon.write_json(folder / "a.json", {"k": [1, "x"]})
    assert (folder / "a.json").read_bytes() == b'{"k":[1,"x"]}\n'
    assert (digest, size) == (hashlib.sha256(b'{"k":[1,"x"]}\n').hexdigest(), 14)

    rows = [{"arxiv_id": "2608.90002", "n": 2}, {"arxiv_id": "2608.90001", "n": 1}]
    digest, size = canon.write_jsonl(folder / "rows.jsonl", rows)
    wanted = b'{"arxiv_id":"2608.90002","n":2}\n{"arxiv_id":"2608.90001","n":1}\n'
    assert (folder / "rows.jsonl").read_bytes() == wanted
    assert (digest, size) == (hashlib.sha256(wanted).hexdigest(), len(wanted))

    digest, size = canon.write_jsonl(folder / "none.jsonl", [])
    assert (folder / "none.jsonl").read_bytes() == b""
    assert (digest, size) == (hashlib.sha256(b"").hexdigest(), 0)

    digest, size = canon.write_text(folder / "page.md", "# t" + E_ACUTE + "\n")
    assert (folder / "page.md").read_bytes() == b"# t\xc3\xa9\n"
    assert size == 6
    for bad in ("no line feed", "two line feeds\n\n", ""):
        with pytest.raises(ValueError):
            canon.write_text(folder / "bad.md", bad)
    assert not (folder / "bad.md").exists()

    canon.write_json(folder / "a.json", {"k": 2})
    assert (folder / "a.json").read_bytes() == b'{"k":2}\n'

    for name, action in [
        ("json", lambda: canon.write_json(folder / "a.json", {"k": 3})),
        ("jsonl", lambda: canon.write_jsonl(folder / "rows.jsonl", rows)),
        ("text", lambda: canon.write_text(folder / "page.md", "x\n")),
    ]:
        assert _file_system_calls(monkeypatch, action) == ["open", "replace"], name

    assert not [name for name in _tree(tmp_path) if name.endswith(".tmp")]


def test_ln_of_whole_numbers():
    assert canon.ln(1) == 0.0
    assert canon.ln(2) == 0.6931471805599453
    assert canon.ln(3) == 1.0986122886681098
    assert canon.ln(51) == 3.9318256327243257
    assert round(canon.ln(6), 6) == 1.791759
    for bad in (0, -3, True, False, 2.0, "2", None):
        with pytest.raises(ValueError):
            canon.ln(bad)
    first = canon.ln(1000)
    assert canon.ln(1000) is first
    assert round(first, 6) == 6.907755


def test_work_counters():
    work.reset()
    assert len(work.NAMES) == 18
    assert list(work.counts()) == list(work.NAMES)
    assert set(work.counts().values()) == {0}
    work.count("text.read")
    work.count("text.read", 3)
    work.count("digests.pages")
    counts = work.counts()
    assert counts["text.read"] == 4 and counts["digests.pages"] == 1
    assert [name for name, value in counts.items() if value] == ["text.read", "digests.pages"]
    counts["text.read"] = 99
    assert work.counts()["text.read"] == 4
    with pytest.raises(KeyError):
        work.count("text.unknown")
    assert work.counts()["text.read"] == 4
    work.reset()
    assert list(work.counts().items()) == [(name, 0) for name in work.NAMES]


def test_prune_removes_what_the_build_did_not_write(lens_home, tmp_path, monkeypatch):
    from app.insights import pipeline

    out = tmp_path / "out"
    for rel in ("trends/cs.CL.json", "trends/cs.CV.json.tmp", "digests/cs.AI/2026-W30.md",
                "digests/stat.ML/2026-W30.md", "digests/old/deep/page.md", "notes.txt", "papers.jsonl"):
        _put(out / rel, "left\n")
    (out / "digests" / "empty").mkdir()

    ctx = pipeline.Context(asof.of(date(2026, 9, 10)), config.load(), out)
    ctx.write_json("trends/cs.LG.json", {"feed": "cs.LG"})
    ctx.write_json("authors.json", {"weeks": []})
    ctx.write_text("digests/cs.AI/2026-W31.md", "# cs.AI week 2026-W31\n")
    assert ctx.files["trends/cs.LG.json"] == {"bytes": 17, "sha256": hashlib.sha256(b'{"feed":"cs.LG"}\n').hexdigest()}

    removed = []

    def recording(name, real):
        def call(path):
            removed.append((name, os.path.relpath(path, out).replace(os.sep, "/")))
            return real(path)

        return call

    with monkeypatch.context() as patch:
        patch.setattr(os, "unlink", recording("unlink", os.unlink))
        patch.setattr(os, "rmdir", recording("rmdir", os.rmdir))
        ctx.prune("trends")
        ctx.prune("digests")
    assert removed == [
        ("unlink", "trends/cs.CL.json"),
        ("unlink", "trends/cs.CV.json.tmp"),
        ("unlink", "digests/cs.AI/2026-W30.md"),
        ("unlink", "digests/old/deep/page.md"),
        ("unlink", "digests/stat.ML/2026-W30.md"),
        ("rmdir", "digests/old/deep"),
        ("rmdir", "digests/empty"),
        ("rmdir", "digests/old"),
        ("rmdir", "digests/stat.ML"),
    ]
    assert _tree(out) == {"authors.json", "digests/cs.AI/2026-W31.md", "notes.txt", "papers.jsonl", "trends/cs.LG.json"}
    assert sorted(os.listdir(out / "digests")) == ["cs.AI"]

    # A build that wrote nothing under a folder leaves no folder; a missing folder is no error.
    again = pipeline.Context(asof.of(date(2026, 9, 10)), config.load(), out)
    again.write_json("trends/cs.LG.json", {"feed": "cs.LG"})
    again.prune("digests")
    again.prune("digests")
    again.prune("nothing")
    assert not (out / "digests").exists()
    assert _tree(out) == {"authors.json", "notes.txt", "papers.jsonl", "trends/cs.LG.json"}
