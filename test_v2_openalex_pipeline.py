from fastapi.testclient import TestClient

from openalex_client import OpenAlexClient
from v2 import app as v2_app


def test_parse_author_exposes_v2_openalex_id_key():
    parsed = OpenAlexClient()._parse_author({
        "id": "https://openalex.org/A123",
        "display_name": "Ada Lovelace",
        "orcid": "https://orcid.org/0000-0001-0000-000X",
        "summary_stats": {"h_index": 42},
        "last_known_institutions": [],
        "topics": [],
    })

    assert parsed["author_id"] == "https://openalex.org/A123"
    assert parsed["openalex_id"] == "https://openalex.org/A123"


def test_v2_form_helpers_preserve_multiple_filters():
    form = {
        "countries_exclude": ["United States, Canada", "GB"],
        "disciplines": ["Medicine", "Computer Science"],
    }

    assert v2_app._form_values(form, "disciplines") == ["Medicine", "Computer Science"]
    assert v2_app._country_codes(v2_app._form_values(form, "countries_exclude")) == ["US", "CA", "GB"]


def test_v2_openalex_search_uses_existing_client_methods(monkeypatch):
    calls = {}

    class FakeOpenAlexClient:
        def search_topics(self, keywords):
            calls["keywords"] = keywords
            return ["T1", "T2"], []

        def fetch_author_batch(self, **kwargs):
            calls["fetch"] = kwargs
            return {
                "results": [{
                    "author_id": "https://openalex.org/A1",
                    "name": "Author One",
                    "orcid_id": "0000-0001",
                    "h_index": 12,
                    "country": "FR",
                    "discipline": "Medicine",
                    "specialty": "Oncology",
                }],
                "next_cursor": "next-cursor",
            }

        def get_total_count(self, **kwargs):
            calls["count"] = kwargs
            return 123

    monkeypatch.setattr(v2_app, "OpenAlexClient", FakeOpenAlexClient)
    monkeypatch.setattr(v2_app, "get_db", lambda: type("DB", (), {
        "available": False,
        "is_email_suppressed": lambda self, email: False,
        "is_author_notified": lambda self, orcid_id, invitation_type, journal_name: False,
    })())
    monkeypatch.setattr(v2_app, "get_or_create_session", lambda request, response: v2_app.SessionData("test", {}))

    client = TestClient(v2_app.app)
    response = client.post(
        "/search/openalex",
        data={
            "keywords": "cancer\nimmunology",
            "h_index_min": "5",
            "h_index_max": "50",
            "countries_exclude": ["United States", "Canada"],
            "disciplines": ["Medicine"],
            "jump_size": "250",
        },
    )

    assert response.status_code == 200
    assert calls["keywords"] == ["cancer", "immunology"]
    assert calls["fetch"]["topic_ids"] == ["T1", "T2"]
    assert calls["fetch"]["exclude_country_codes"] == ["US", "CA"]
    assert calls["count"]["exclude_country_codes"] == ["US", "CA"]
    assert "Author One" in response.text
