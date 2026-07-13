import app


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

        def bulk_upsert_harvested_authors(self, authors, run_id=1):
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
