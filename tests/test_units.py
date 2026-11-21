"""Small pieces checked in-process: settings defaults, arXiv ids, analysis validation, feed entries."""
import feedparser
import pytest

from app.ids import split
from app.services.paper_analyzer import InvalidAnalysis, checked, validate_analysis
from conftest import new_home

GOOD = {
    "summary": "s", "key_contributions": ["c"], "methodology": "m",
    "limitations": "l", "keywords": ["k"], "novelty_score": 7,
}


def test_defaults_stay_on_the_real_services(monkeypatch, tmp_path):
    from app.config import Settings

    for name in ("DATABASE_URL", "ARXIV_RSS_BASE", "ARXIV_API_URL", "ARXIV_PDF_BASE", "ARXIV_API_DELAY_SECONDS",
                 "SCHEDULER_ENABLED", "PROCESSING_LEASE_SECONDS", "POLL_INTERVAL_HOURS",
                 "AZURE_BLOB_CONNECTION_STRING", "AZURE_STORAGE_ACCOUNT_NAME", "CATEGORIES_FILE"):
        monkeypatch.delenv(name, raising=False)
    defaults = Settings(_env_file=None)
    assert defaults.database_url == ""
    assert defaults.arxiv_pdf_base == ""
    assert "data_dir" not in Settings.model_fields
    assert defaults.arxiv_rss_base == "https://rss.arxiv.org/rss"
    assert defaults.arxiv_api_url == "https://export.arxiv.org/api/query"
    assert defaults.arxiv_api_delay_seconds == 3.0
    assert defaults.scheduler_enabled is True and defaults.poll_interval_hours == 6
    assert defaults.processing_lease_seconds == 600
    assert defaults.categories_file == "categories.yaml"
    assert defaults.blob_storage_configured is False

    assert Settings(_env_file=None, azure_storage_account_name="acct").blob_storage_configured is True
    assert Settings(_env_file=None, azure_blob_connection_string="x").blob_storage_configured is True
    assert Settings(_env_file=None, database_url="sqlite+aiosqlite:////tmp/x.db").is_sqlite is True
    assert Settings(_env_file=None, database_url="postgresql+asyncpg://localhost/x").is_sqlite is False
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    assert Settings(_env_file=None).scheduler_enabled is False

    # With DATABASE_URL empty the database is the SQLite file of the home.
    home = new_home(tmp_path / "home")
    home.run("db", "upgrade")
    assert (home.path / "lens.db").is_file()
    assert home.sql("SELECT count(*) FROM schema_version")[0][0] >= 1


def test_split_arxiv_id():
    rows = [
        ("2401.90001v1", ("2401.90001", 1)),
        ("2401.90001v12", ("2401.90001", 12)),
        ("2401.90001", ("2401.90001", None)),
        ("hep-th/9901901v2", ("hep-th/9901901", 2)),
        ("hep-th/9901901", ("hep-th/9901901", None)),
    ]
    for raw, expected in rows:
        assert split(raw) == expected, raw


def test_valid_analysis_passes():
    validate_analysis(dict(GOOD))
    validate_analysis({**GOOD, "limitations": None})
    validate_analysis({k: v for k, v in GOOD.items() if k != "limitations"})
    # What both sources hand on: the score within 1 to 10, limitations always present.
    assert checked({**GOOD, "novelty_score": 12})["novelty_score"] == 10
    assert checked({**GOOD, "novelty_score": 0})["novelty_score"] == 1
    assert checked({k: v for k, v in GOOD.items() if k != "limitations"})["limitations"] is None


@pytest.mark.parametrize("field,value", [
    ("keywords", "kw"), ("keywords", ["kw", 3]), ("keywords", None),
    ("key_contributions", "one"), ("key_contributions", {"a": 1}),
    ("summary", ["s"]), ("summary", None), ("methodology", 5),
    ("limitations", ["l"]), ("novelty_score", "7"), ("novelty_score", None),
    ("novelty_score", True), ("novelty_score", 7.5), ("novelty_score", [7]),
])
def test_analysis_field_of_the_wrong_type_is_rejected(field, value):
    with pytest.raises(InvalidAnalysis) as error:
        validate_analysis({**GOOD, field: value})
    assert field in str(error.value)


def test_analysis_with_missing_field_or_wrong_shape_is_rejected():
    with pytest.raises(InvalidAnalysis):
        validate_analysis({k: v for k, v in GOOD.items() if k != "keywords"})
    with pytest.raises(InvalidAnalysis):
        validate_analysis([GOOD])


def test_feed_entries_are_read_with_id_version_and_announce_type():
    from app.services.sources import _parse_entry

    def item(link, guid, description, announce=""):
        tag = f"<arxiv:announce_type>{announce}</arxiv:announce_type>" if announce else ""
        return (f"<item><title>T</title><link>{link}</link><description>{description}</description>"
                f'<guid isPermaLink="false">{guid}</guid>{tag}</item>')

    xml = (
        "<?xml version='1.0' encoding='UTF-8'?>"
        '<rss xmlns:arxiv="http://arxiv.org/schemas/atom" version="2.0"><channel><title>x</title>'
        + item("https://arxiv.org/abs/2401.90001", "oai:arXiv.org:2401.90001v3",
               "arXiv:2401.90001v3 Announce Type: replace\nAbstract: a", "replace")
        + item("https://arxiv.org/abs/2401.92345", "oai:arXiv.org:2401.92345v1",
               "arXiv:2401.92345v1 Announce Type: cross\nAbstract: a")
        + item("https://arxiv.org/abs/hep-th/9901901", "oai:arXiv.org:hep-th/9901901v2",
               "arXiv:hep-th/9901901v2 Announce Type: replace-cross\nAbstract: a", "replace-cross")
        + item("https://arxiv.org/abs/2401.90002", "", "Abstract: a")
        + item("https://example.org/nothing", "", "nothing")
        + "</channel></rss>"
    )
    parsed = [_parse_entry(e) for e in feedparser.parse(xml).entries]
    assert [(a.arxiv_id, a.version, a.kind) if a else None for a in parsed] == [
        ("2401.90001", 3, "replace"),
        ("2401.92345", 1, "cross"),
        ("hep-th/9901901", 2, "replace-cross"),
        ("2401.90002", None, "new"),
        None,
    ]
