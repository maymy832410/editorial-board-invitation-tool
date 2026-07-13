import app
from db_client import EMAIL_STATUS_PENDING, PostgresStorage


def test_source_selection_openalex_ignores_stale_database_rows():
    openalex_rows = [{"name": "OpenAlex Author", "orcid_id": "0000-0001"}]
    database_rows = [{"name": "Database Author", "orcid_id": "0000-0002"}]

    selected = app._select_author_source_results(
        app.AUTHOR_SOURCE_OPENALEX,
        openalex_rows,
        database_rows,
    )

    assert selected == openalex_rows


def test_source_selection_both_merges_database_email():
    openalex_rows = [{"name": "OpenAlex Author", "orcid_id": "0000-0001"}]
    database_rows = [{
        "name": "DB Author",
        "orcid_id": "0000-0001",
        "email": "author@example.com",
        "source_origin": app.AUTHOR_SOURCE_DATABASE,
    }]

    selected = app._select_author_source_results(
        app.AUTHOR_SOURCE_BOTH,
        openalex_rows,
        database_rows,
    )

    assert len(selected) == 1
    assert selected[0]["email"] == "author@example.com"
    assert selected[0]["source_origin"] == app.AUTHOR_SOURCE_BOTH


def test_source_selection_database_only_uses_database_rows():
    openalex_rows = [{"name": "OpenAlex Author", "orcid_id": "0000-0001"}]
    database_rows = [{"name": "Database Author", "orcid_id": "0000-0002"}]

    selected = app._select_author_source_results(
        app.AUTHOR_SOURCE_DATABASE,
        openalex_rows,
        database_rows,
    )

    assert selected == database_rows


def test_collect_openalex_authors_uses_prefilters_and_persists():
    calls = []

    class FakeClient:
        def fetch_author_batch(self, **kwargs):
            calls.append(kwargs)
            return {
                "results": [
                    {
                        "author_id": "https://openalex.org/A1",
                        "openalex_id": "https://openalex.org/A1",
                        "name": "Kept",
                        "orcid_id": "0000-0001",
                        "h_index": 20,
                        "country": "GB",
                        "discipline": "Medicine",
                        "specialty": "Oncology",
                    },
                    {
                        "author_id": "https://openalex.org/A2",
                        "openalex_id": "https://openalex.org/A2",
                        "name": "Filtered",
                        "orcid_id": "0000-0002",
                        "h_index": 22,
                        "country": "GB",
                        "discipline": "Computer Science",
                    },
                ],
                "next_cursor": None,
                "has_more": False,
            }

    class FakeStorage:
        available = True

        def __init__(self):
            self.persisted = []

        def persist_seed_batch(self, search_run_id, authors, next_cursor=None, has_more=False):
            self.search_run_id = search_run_id
            self.persisted.extend(authors)
            return len(authors)

    storage = FakeStorage()
    search_state = {
        "filters": {
            "h_index_min": 10,
            "h_index_max": 50,
            "include_country_codes": ["GB"],
            "exclude_country_codes": ["US"],
            "topic_ids": ["T1"],
            "require_orcid": True,
        },
        "total_count": 2,
        "max_results": 2,
        "jump_size": 250,
    }

    collected = app._collect_openalex_authors_for_state(
        search_state,
        client=FakeClient(),
        storage=storage,
        search_run_id=42,
        max_results=2,
        jump_size=250,
        disciplines=["Medicine"],
    )

    assert calls[0]["h_index_min"] == 10
    assert calls[0]["include_country_codes"] == ["GB"]
    assert calls[0]["topic_ids"] == ["T1"]
    assert [author["name"] for author in collected] == ["Kept"]
    assert storage.persisted[0]["openalex_id"] == "https://openalex.org/A1"
    assert storage.persisted[0]["orcid_id"] == "0000-0001"
    assert storage.persisted[0]["source_origin"] == app.AUTHOR_SOURCE_OPENALEX
    assert storage.search_run_id == 42


def test_persist_found_email_updates_harvested_and_profile(monkeypatch):
    class FakeStorage:
        available = True

        def __init__(self):
            self.harvested = []
            self.email_updates = []
            self.profiles = []

        def upsert_harvested_author(self, author):
            self.harvested.append(author)
            return True

        def update_harvest_email(self, **kwargs):
            self.email_updates.append(kwargs)
            return True

        def upsert_author_profile(self, **kwargs):
            self.profiles.append(kwargs)
            return True

    storage = FakeStorage()
    monkeypatch.setattr(app, "db_storage", storage)

    saved = app._persist_found_author_email(
        {
            "author_id": "https://openalex.org/A1",
            "orcid_id": "0000-0001",
            "name": "Author One",
            "discipline": "Medicine",
        },
        "author@example.com",
        source="orcid",
    )

    assert saved is True
    assert storage.harvested[0]["author_id"] == "https://openalex.org/A1"
    assert storage.email_updates[0]["status"] == app.EMAIL_STATUS_FOUND
    assert storage.email_updates[0]["email"] == "author@example.com"
    assert storage.profiles[0]["email"] == "author@example.com"
    assert storage.profiles[0]["openalex_id"] == "https://openalex.org/A1"


def test_build_email_fetch_candidates_uses_full_filtered_set():
    authors = [
        {
            "author_id": "https://openalex.org/A1",
            "name": "Visible Page Author",
            "orcid_id": "0000-0001",
            "email": "",
        },
        {
            "author_id": "https://openalex.org/A2",
            "name": "Collected Later Batch Author",
            "orcid_id": "0000-0002",
            "email": None,
        },
        {
            "author_id": "https://openalex.org/A3",
            "name": "Already Has Email",
            "orcid_id": "0000-0003",
            "email": "has@example.com",
        },
        {
            "author_id": "https://openalex.org/A4",
            "name": "Already Processed",
            "orcid_id": "0000-0004",
            "email": "",
        },
    ]

    candidates = app._build_email_fetch_candidates(authors, {"0000-0004"})

    assert [candidate["orcid_id"] for candidate in candidates] == ["0000-0001", "0000-0002"]
    assert candidates[1]["openalex_id"] == "https://openalex.org/A2"


def test_persist_missing_email_updates_harvested_without_dropping_metadata(monkeypatch):
    class FakeStorage:
        available = True

        def __init__(self):
            self.harvested = []
            self.email_updates = []

        def upsert_harvested_author(self, author):
            self.harvested.append(author)
            return True

        def update_harvest_email(self, **kwargs):
            self.email_updates.append(kwargs)
            return True

    storage = FakeStorage()
    monkeypatch.setattr(app, "db_storage", storage)

    saved = app._persist_missing_author_email(
        {
            "author_id": "https://openalex.org/A1",
            "orcid_id": "0000-0001",
            "name": "Author One",
            "discipline": "Medicine",
            "country": "GB",
        },
        app.EMAIL_STATUS_NO_EMAIL,
    )

    assert saved is True
    assert storage.harvested[0]["discipline"] == "Medicine"
    assert storage.harvested[0]["country"] == "GB"
    assert storage.email_updates[0]["status"] == app.EMAIL_STATUS_NO_EMAIL
    assert storage.email_updates[0]["email_source"] == "orcid"


def test_rate_limited_email_persist_keeps_author_retryable(monkeypatch):
    class FakeStorage:
        available = True

        def __init__(self):
            self.harvested = []
            self.email_updates = []

        def upsert_harvested_author(self, author):
            self.harvested.append(author)
            return True

        def update_harvest_email(self, **kwargs):
            self.email_updates.append(kwargs)
            return True

    storage = FakeStorage()
    monkeypatch.setattr(app, "db_storage", storage)

    saved = app._persist_rate_limited_author_email(
        {
            "author_id": "https://openalex.org/A1",
            "orcid_id": "0000-0001",
            "name": "Author One",
        }
    )

    assert saved is True
    assert storage.harvested[0]["author_id"] == "https://openalex.org/A1"
    assert storage.email_updates[0]["status"] == app.EMAIL_STATUS_PENDING
    assert storage.email_updates[0]["email_source"] == "orcid"
    assert storage.email_updates[0]["next_retry_at"] is not None


def test_harvested_row_maps_to_openalex_author():
    author = app._map_harvested_row_to_author({
        "openalex_id": "https://openalex.org/A1",
        "orcid_id": "0000-0001",
        "author_name": "Author One",
        "email": "author@example.com",
        "all_emails": "author@example.com",
        "email_source": "orcid",
        "email_status": app.EMAIL_STATUS_FOUND,
        "h_index": 25,
        "institution": "Example University",
        "country": "GB",
        "discipline": "Medicine",
        "specialty": "Oncology",
        "all_topics_json": '["Oncology", "Cancer Biology"]',
    })

    assert author["source_origin"] == app.AUTHOR_SOURCE_OPENALEX
    assert author["openalex_id"] == "https://openalex.org/A1"
    assert author["email"] == "author@example.com"
    assert author["all_topics"] == ["Oncology", "Cancer Biology"]


def test_collection_config_from_search_state_uses_prefilters():
    config = app._collection_config_from_search_state({
        "filters": {
            "disciplines": ["Medicine"],
            "exclude_country_codes": ["US"],
            "keyword_tags": ["cancer"],
            "topic_ids": ["T1"],
            "h_index_min": 10,
            "h_index_max": 80,
        }
    })

    assert config["disciplines"] == ["Medicine"]
    assert config["exclude_countries"] == ["US"]
    assert config["keyword_tags"] == "cancer"
    assert config["topic_ids"] == ["T1"]
    assert config["h_index_min"] == 10


def test_email_fetch_criteria_ignores_display_email_filter_only():
    display_criteria = {
        "search_text": "oncology",
        "disciplines": ["Medicine"],
        "include_countries": ["GB"],
        "email_filter": "with",
        "hide_sent": True,
    }

    fetch_criteria = app._criteria_for_openalex_email_fetch(display_criteria)

    assert fetch_criteria["email_filter"] == ""
    assert fetch_criteria["search_text"] == "oncology"
    assert fetch_criteria["disciplines"] == ["Medicine"]
    assert fetch_criteria["include_countries"] == ["GB"]
    assert fetch_criteria["hide_sent"] is True
    assert display_criteria["email_filter"] == "with"


def test_collection_filter_clause_includes_post_filters():
    storage = PostgresStorage.__new__(PostgresStorage)

    clause, params = storage._collection_filter_clause(
        {
            "search_text": "oncology",
            "disciplines": ["Medicine"],
            "include_countries": ["GB"],
            "exclude_countries": ["US"],
            "specialties": ["Cancer"],
            "include_domains": ["Biology"],
            "exclude_domains": ["Physics"],
            "email_filter": "without",
            "hide_sent": True,
            "sent_orcids": ["0000-0001"],
            "hide_retracted": True,
            "retracted_names": ["Retracted Author"],
        },
        pending_only=True,
    )

    assert "h.email_status = %s" in clause
    assert "h.discipline = ANY(%s)" in clause
    assert "h.country = ANY(%s)" in clause
    assert "h.email = ''" in clause
    assert EMAIL_STATUS_PENDING in params
    assert ["Medicine"] in params
    assert ["GB"] in params
