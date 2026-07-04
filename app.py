"""Editorial Board Invitation Tool - Streamlit App

A unified tool for finding academic authors and sending editorial board invitations.
"""

import asyncio
import json
import os
import re
import threading
import time
import streamlit as st
import pandas as pd

try:
    from streamlit_searchbox import st_searchbox
except ImportError:  # Manual-entry fallback keeps the dashboard usable.
    st_searchbox = None

from config import (
    COUNTRIES,
    DEFAULT_H_INDEX_MIN,
    DEFAULT_H_INDEX_MAX,
    DEFAULT_MAX_RESULTS,
)
from openalex_client import OpenAlexClient, OpenAlexRequestError
from orcid_async import fetch_emails_async
from openai_email_async import AsyncOpenAIEmailClient
from progress_manager import StateManager
from author_filters import author_matches_any_specialty, dedupe_authors
from bulk_email_jobs import cap_bulk_recipients, prepare_bulk_recipients
from bulk_email_worker import BulkEmailWorker
from disciplines import ALL_DISCIPLINES
from email_sender import EmailSender
from templates import (
    get_template_names,
    format_template,
    format_recent_publications,
    INVITATION_TYPE_EDITORIAL,
    INVITATION_TYPE_PUBLICATION,
    TEMPLATE_BOARD_MEMBER,
    TEMPLATE_MANAGING_EDITOR,
    TEMPLATE_EDITOR_IN_CHIEF,
)
from pdf_generator import generate_invitation_pdf, PUBLISHER_INFO
from db_client import get_storage as get_db_storage
from db_client import (
    RUN_STATUS_ACTIVE,
    RUN_STATUS_IDLE,
    RUN_STATUS_PAUSED,
    EMAIL_STATUS_FOUND,
    BULK_JOB_STATUS_QUEUED,
    BULK_JOB_STATUS_RUNNING,
)
from journal_presets import normalize_journal_preset_config

WORKFLOW_AUTHOR = "author"
WORKFLOW_EDITORIAL = "editorial"

AUTHOR_SOURCE_OPENALEX = "openalex"
AUTHOR_SOURCE_DATABASE = "database"
AUTHOR_SOURCE_BOTH = "both"

QUARTILE_OPTIONS = ["", "Q1", "Q2", "Q3", "Q4"]
INDEXING_OPTIONS = ["", "Not indexed", "Scopus", "Web of Science", "DOAJ", "Other"]
INVITATION_GOAL_OPTIONS = ["Regular submission", "Special issue", "Review article", "Fast-track consideration"]


# Page config
st.set_page_config(
    page_title="Editorial Board Invitation Tool",
    page_icon="📬",
    layout="wide"
)


@st.cache_data(ttl=300, show_spinner=False)
def _openalex_topic_options(searchterm: str):
    """Search OpenAlex topics and return searchbox label/value pairs."""
    try:
        topics = OpenAlexClient().search_topic_suggestions(searchterm, limit=12)
    except Exception:
        return []
    options = []
    for topic in topics:
        context = " · ".join(v for v in (topic.get("subfield"), topic.get("field")) if v)
        count = f"{int(topic.get('works_count') or 0):,} works"
        label = f"{topic['name']} — {context} ({count})" if context else f"{topic['name']} ({count})"
        options.append((label, topic))
    return options

# Initialize state manager
state_mgr = StateManager()

# Initialize email sender as cached resource (shared across all users for rotation)
@st.cache_resource
def get_email_sender():
    """Get cached EmailSender instance (shared across all users for round-robin rotation)."""
    try:
        return EmailSender()
    except FileNotFoundError:
        return None

email_sender = get_email_sender()
EMAIL_AVAILABLE = email_sender is not None


@st.cache_resource
def start_bulk_email_daemon():
    """Start one in-process daemon that drains durable bulk email jobs."""
    stop_event = threading.Event()

    def _run():
        worker = None
        while not stop_event.is_set():
            try:
                if worker is None:
                    worker = BulkEmailWorker()
                did_work = worker.process_next()
                if not did_work:
                    stop_event.wait(5)
            except Exception as exc:
                print(f"[bulk-email] daemon error: {exc}", flush=True)
                worker = None
                stop_event.wait(10)

    thread = threading.Thread(target=_run, name="bulk-email-daemon", daemon=True)
    thread.start()
    print("[bulk-email] in-process daemon started", flush=True)
    return stop_event


# Load saved state
if 'app_state' not in st.session_state:
    st.session_state.app_state = state_mgr.load_state()

if 'fetching_emails' not in st.session_state:
    st.session_state.fetching_emails = False

if 'stop_fetching' not in st.session_state:
    st.session_state.stop_fetching = False

if 'selected_author' not in st.session_state:
    st.session_state.selected_author = None

if 'edited_email' not in st.session_state:
    st.session_state.edited_email = {'to': '', 'subject': '', 'body': ''}


def save_state():
    """Save current state to file."""
    state_mgr.save_state(st.session_state.app_state)


def _current_journal_preset_config() -> dict:
    """Return the current journal config restricted to preset-supported fields."""
    return normalize_journal_preset_config(st.session_state.app_state.get('journal_config', {}))


def _sync_journal_preset_widget_state(config: dict) -> None:
    """Prime sidebar widgets with a loaded journal preset before they render."""
    normalized = normalize_journal_preset_config(config)
    widget_keys = {
        "name": "journal_name",
        "issn": "journal_issn",
        "link": "journal_link",
        "location": "publisher_location",
        "editor_in_chief": "editor_name",
        "submission_link": "journal_submission_link",
        "cite_score": "journal_cite_score",
        "quartile": "journal_quartile",
        "indexing_status": "journal_indexing_status",
        "invitation_goal": "journal_invitation_goal",
        "scope": "journal_scope",
    }
    normalized["quartile"] = normalized["quartile"] if normalized["quartile"] in QUARTILE_OPTIONS else ""
    normalized["indexing_status"] = normalized["indexing_status"] if normalized["indexing_status"] in INDEXING_OPTIONS else "Other"
    normalized["invitation_goal"] = (
        normalized["invitation_goal"]
        if normalized["invitation_goal"] in INVITATION_GOAL_OPTIONS
        else "Regular submission"
    )
    for field, key in widget_keys.items():
        st.session_state[key] = normalized.get(field, "")


def _safe_select_index(options, value, default=0):
    """Return the index for a Streamlit selectbox value with a safe fallback."""
    try:
        return options.index(value)
    except ValueError:
        return default


def _invitation_type_label(invitation_type: str) -> str:
    """Return a user-friendly invitation type label."""
    if invitation_type == INVITATION_TYPE_PUBLICATION:
        return "Publication Submission"
    return "Editorial Role"


def _workflow_label(workflow: str) -> str:
    """Return the visible label for a top-level workflow tab."""
    if workflow == WORKFLOW_AUTHOR:
        return "Author Invitation"
    return "Editorial Invitation"


def _workflow_invitation_type(workflow: str) -> str:
    """Map UI workflow names to persistence invitation types."""
    if workflow == WORKFLOW_AUTHOR:
        return INVITATION_TYPE_PUBLICATION
    return INVITATION_TYPE_EDITORIAL


def _author_source_label(source_mode: str) -> str:
    """Return a readable label for an Author Invitation source mode."""
    if source_mode == AUTHOR_SOURCE_DATABASE:
        return "Database Emails"
    if source_mode == AUTHOR_SOURCE_BOTH:
        return "OpenAlex + Database Emails"
    return "OpenAlex"


def _database_email_source_label(source: str) -> str:
    """Return a readable label for database email source filters."""
    labels = {
        "all": "All database email records",
        "profiles": "Author profiles",
        "harvested": "Collected emails",
    }
    return labels.get(source, source or "Database")


def _publisher_display_label(publisher_id: str) -> str:
    """Return a stable label for the selected publisher/sender."""
    if not EMAIL_AVAILABLE:
        return publisher_id or "Unavailable"
    name = email_sender.get_publisher_name(publisher_id)
    email = email_sender.get_publisher_email(publisher_id)
    if name and email:
        return f"{name} <{email}>"
    return name or email or publisher_id or "Unknown publisher"


def _scope_key(scope: str, key: str) -> str:
    """Build a deterministic widget/session key scoped to one workflow tab."""
    return f"{scope}_{key}"


def _tracking_journal_name(invitation_type: str, journal_config: dict) -> str:
    """Publication invitations are tracked per journal; editorial legacy tracking is global."""
    if invitation_type == INVITATION_TYPE_PUBLICATION:
        return journal_config.get('name', '') or ''
    return ""


def _typed_invitation_key(orcid_id: str, invitation_type: str, journal_name: str = "") -> str:
    """Build a stable local key for offline typed invitation tracking."""
    return f"{invitation_type}::{journal_name or ''}::{orcid_id or ''}"


def _recipient_tracking_id(author: dict, email: str = "") -> str:
    """Return the stable identity used for sent tracking, falling back to email-only rows."""
    orcid_id = (author.get('orcid_id') or '').strip()
    if orcid_id:
        return orcid_id

    normalized_email = (email or author.get('email') or '').strip().lower()
    if normalized_email:
        return f"email:{normalized_email}"

    author_id = (author.get('author_id') or author.get('openalex_id') or '').strip().rstrip('/')
    if author_id:
        return f"openalex:{author_id.lower()}"

    profile_key = (author.get('profile_key') or '').strip().lower()
    return f"profile:{profile_key}" if profile_key else ""


def _clean_domain_label(value: str) -> str:
    """Normalize spacing for domain labels while preserving readable casing."""
    return " ".join((value or "").strip().split())


def _extract_author_domains(author: dict) -> set[str]:
    """Collect scientific-domain style labels from one author record."""
    domains: set[str] = set()

    scientific_domain = _clean_domain_label(author.get('scientific_domain', ''))
    if scientific_domain:
        domains.add(scientific_domain)

    discipline = _clean_domain_label(author.get('discipline', ''))
    if discipline:
        domains.add(discipline)

    specialty = _clean_domain_label(author.get('specialty', ''))
    if specialty:
        domains.add(specialty)

    for topic in author.get('all_topics') or []:
        topic_label = _clean_domain_label(topic)
        if topic_label:
            domains.add(topic_label)

    return domains


def _parse_scientific_domains_json(value: object) -> list[str]:
    """Parse scientific_domains_json into a normalized topic list."""
    parsed: list[object] = []
    if isinstance(value, list):
        parsed = value
    elif isinstance(value, str):
        normalized = value.strip()
        if normalized:
            try:
                loaded = json.loads(normalized)
                if isinstance(loaded, list):
                    parsed = loaded
            except Exception:
                parsed = []

    cleaned: list[str] = []
    for raw_item in parsed:
        text_value = _clean_domain_label(str(raw_item))
        if text_value:
            cleaned.append(text_value)
    return cleaned


def _parse_json_list(value: object) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, list) else []
        except Exception:
            return []
    return []


def _map_db_profile_row_to_author(row: dict) -> dict:
    """Convert one author_profiles row into the shared author result schema."""
    topics = _parse_scientific_domains_json(row.get('scientific_domains_json'))
    scientific_domain = _clean_domain_label(row.get('scientific_domain', ''))
    specialty = topics[0] if topics else ""
    discipline = scientific_domain or specialty or "Other"

    orcid_id = (row.get('orcid_id') or '').strip()
    email = (row.get('email') or '').strip()
    author_name = (row.get('author_name') or '').strip() or f"Author {orcid_id}"

    return {
        'profile_key': row.get('profile_key', ''),
        'author_id': row.get('openalex_id', ''),
        'name': author_name,
        'orcid_id': orcid_id,
        'orcid_url': f"https://orcid.org/{orcid_id}" if orcid_id else '',
        'h_index': None,
        'works_count': None,
        'cited_by_count': None,
        'institution': '',
        'country': '',
        'discipline': discipline,
        'specialty': specialty,
        'subfield': '',
        'all_topics': topics,
        'research_areas': ", ".join(topics[:3]) if topics else '',
        'email': email,
        'all_emails': email,
        'email_source': 'db_profile',
        'scientific_domain': scientific_domain,
        'source_origin': AUTHOR_SOURCE_DATABASE,
    }


def _map_database_email_row_to_author(row: dict) -> dict:
    """Convert a database email search row into the shared author result schema."""
    topics = _parse_scientific_domains_json(row.get('scientific_domains_json'))
    research_areas = _clean_domain_label(row.get('research_areas', ''))
    if research_areas and not topics:
        topics = [_clean_domain_label(value) for value in research_areas.split(",") if _clean_domain_label(value)]

    scientific_domain = _clean_domain_label(row.get('scientific_domain', ''))
    specialty = _clean_domain_label(row.get('specialty', '')) or (topics[0] if topics else "")
    discipline = _clean_domain_label(row.get('discipline', '')) or scientific_domain or specialty or "Other"
    source_table = (row.get('source_table') or '').strip()
    source_origin = AUTHOR_SOURCE_DATABASE
    email_source = f"db_{source_table}" if source_table else "db"

    orcid_id = (row.get('orcid_id') or '').strip()
    openalex_id = (row.get('openalex_id') or '').strip()
    email = (row.get('email') or '').strip()
    all_emails = (row.get('all_emails') or '').strip() or email
    author_name = (row.get('author_name') or '').strip() or f"Author {orcid_id or openalex_id or email}"

    return {
        'profile_key': row.get('profile_key', ''),
        'author_id': openalex_id,
        'name': author_name,
        'orcid_id': orcid_id,
        'orcid_url': f"https://orcid.org/{orcid_id}" if orcid_id else '',
        'h_index': row.get('h_index') or None,
        'works_count': row.get('works_count') or None,
        'cited_by_count': row.get('cited_by_count') or None,
        'institution': row.get('institution', '') or '',
        'country': row.get('country', '') or '',
        'discipline': discipline,
        'specialty': specialty,
        'subfield': row.get('subfield', '') or '',
        'all_topics': topics,
        'research_areas': research_areas or ", ".join(topics[:3]),
        'email': email,
        'all_emails': all_emails,
        'email_source': email_source,
        'scientific_domain': scientific_domain,
        'source_origin': source_origin,
        'source_table': source_table,
    }


@st.cache_data(ttl=60, show_spinner=False)
def _load_author_source_rows_from_db(limit: int) -> list[dict]:
    """Load Author Invitation candidates from author_profiles (ORCID + email only)."""
    if not db_storage.available:
        return []

    target_limit = max(1, int(limit or DEFAULT_MAX_RESULTS))
    rows: list[dict] = []
    offset = 0
    chunk_size = 5000

    while len(rows) < target_limit:
        remaining = target_limit - len(rows)
        current_limit = min(chunk_size, remaining)
        batch = db_storage.get_author_profile_candidates(limit=current_limit, offset=offset)
        if not batch:
            break

        rows.extend(batch)
        fetched = len(batch)
        offset += fetched
        if fetched < current_limit:
            break

    mapped_rows = [_map_db_profile_row_to_author(row) for row in rows]
    return [row for row in mapped_rows if row.get('orcid_id') and row.get('email')]


@st.cache_data(ttl=60, show_spinner=False)
def _search_database_email_rows(
    query: str,
    source: str,
    limit: int,
    require_email: bool,
    hide_suppressed: bool,
    countries: tuple[str, ...] = (),
) -> list[dict]:
    """Search stored database recipients and map them into author rows."""
    if not db_storage.available:
        return []
    rows = db_storage.search_database_email_recipients(
        query=query,
        source=source,
        limit=limit,
        require_email=require_email,
        hide_suppressed=hide_suppressed,
        countries=list(countries),
    )
    return [_map_database_email_row_to_author(row) for row in rows]


def _merge_author_source_results(openalex_rows: list[dict], db_rows: list[dict]) -> list[dict]:
    """Merge OpenAlex and DB rows by ORCID with deterministic field precedence."""
    merged_rows: list[dict] = []
    index_by_key: dict[str, dict] = {}

    def _merge_key(row: dict) -> str:
        orcid_id = (row.get('orcid_id') or '').strip().lower()
        if orcid_id:
            return f"orcid:{orcid_id}"
        email = (row.get('email') or '').strip().lower()
        if email:
            return f"email:{email}"
        author_id = (row.get('author_id') or row.get('openalex_id') or '').strip().rstrip('/').lower()
        if author_id:
            return f"openalex:{author_id}"
        profile_key = (row.get('profile_key') or '').strip().lower()
        return f"profile:{profile_key}" if profile_key else ""

    for openalex_row in openalex_rows:
        row = dict(openalex_row)
        row.setdefault('profile_key', '')
        row.setdefault('source_origin', AUTHOR_SOURCE_OPENALEX)
        row.setdefault('scientific_domain', _clean_domain_label(row.get('discipline', '')))
        merged_rows.append(row)
        key = _merge_key(row)
        if key:
            index_by_key[key] = row

    for db_row in db_rows:
        key = _merge_key(db_row)
        existing = index_by_key.get(key) if key else None
        if not existing:
            merged_rows.append(dict(db_row))
            if key:
                index_by_key[key] = merged_rows[-1]
            continue

        existing['source_origin'] = AUTHOR_SOURCE_BOTH
        if db_row.get('profile_key'):
            existing['profile_key'] = db_row.get('profile_key', '')

        if not existing.get('email') and db_row.get('email'):
            existing['email'] = db_row.get('email', '')
            existing['all_emails'] = db_row.get('all_emails', db_row.get('email', ''))
            existing['email_source'] = db_row.get('email_source', 'db_profile')

        if not existing.get('scientific_domain') and db_row.get('scientific_domain'):
            existing['scientific_domain'] = db_row.get('scientific_domain', '')

        existing_topics = existing.get('all_topics') or []
        db_topics = db_row.get('all_topics') or []
        merged_topics: list[str] = []
        for topic in existing_topics + db_topics:
            label = _clean_domain_label(topic)
            if label and label not in merged_topics:
                merged_topics.append(label)
        if merged_topics:
            existing['all_topics'] = merged_topics
            if not existing.get('specialty'):
                existing['specialty'] = merged_topics[0]

        if (not existing.get('discipline') or existing.get('discipline') == 'Other') and db_row.get('discipline'):
            existing['discipline'] = db_row.get('discipline', '')

    return merged_rows


def _enrich_db_source_domains_from_openalex(authors: list[dict], max_rows: int = 50) -> tuple[int, int]:
    """Enrich missing scientific domains for visible DB-source rows via strict ORCID matching."""
    if not db_storage.available:
        return 0, 0

    targets: list[dict] = []
    seen_profile_keys: set[str] = set()
    for author in authors:
        profile_key = (author.get('profile_key') or '').strip()
        if not profile_key or profile_key in seen_profile_keys:
            continue
        if author.get('source_origin') not in {AUTHOR_SOURCE_DATABASE, AUTHOR_SOURCE_BOTH}:
            continue
        if not author.get('orcid_id') or author.get('scientific_domain'):
            continue
        seen_profile_keys.add(profile_key)
        targets.append(author)

    if not targets:
        return 0, 0

    client = OpenAlexClient()
    attempted = 0
    updated = 0

    for author in targets[:max_rows]:
        attempted += 1
        try:
            fetched = client.fetch_author_by_orcid(author.get('orcid_id', ''))
        except OpenAlexRequestError:
            continue
        if not fetched:
            continue

        scientific_domain = _clean_domain_label(fetched.get('discipline', ''))
        topics = [
            _clean_domain_label(topic)
            for topic in (fetched.get('all_topics') or [])
            if _clean_domain_label(topic)
        ][:8]
        openalex_id = (fetched.get('author_id') or '').strip()

        saved = db_storage.update_profile_openalex(
            profile_key=author.get('profile_key', ''),
            openalex_id=openalex_id,
            scientific_domain=scientific_domain,
            scientific_domains=topics,
        )
        if not saved:
            continue

        updated += 1
        author['author_id'] = openalex_id or author.get('author_id', '')
        author['scientific_domain'] = scientific_domain
        if scientific_domain:
            author['discipline'] = scientific_domain
        if topics:
            author['all_topics'] = topics
            if not author.get('specialty'):
                author['specialty'] = topics[0]

    return updated, attempted


def _hydrate_result_emails_from_db(authors: list[dict]) -> bool:
    """Fill missing result profile fields from database records using ORCID IDs."""
    if not db_storage.available or not authors:
        return False

    missing_orcids = sorted({
        author.get('orcid_id', '')
        for author in authors
        if author.get('orcid_id') and (not author.get('email') or not author.get('scientific_domain'))
    })
    if not missing_orcids:
        return False

    profile_emails: dict[str, str] = {}
    profile_domains: dict[str, str] = {}
    sent_emails: dict[str, str] = {}
    sent_domains: dict[str, str] = {}

    try:
        with db_storage._get_cursor() as cur:
            cur.execute(
                f"""
                SELECT orcid_id, email, COALESCE(scientific_domain, '') AS scientific_domain
                FROM {db_storage.PROFILE_TABLE_NAME}
                WHERE orcid_id = ANY(%s)
                  AND (email <> '' OR COALESCE(scientific_domain, '') <> '');
                """,
                (missing_orcids,),
            )
            for row in cur.fetchall():
                orcid_id = row.get('orcid_id', '')
                email = row.get('email', '')
                scientific_domain = row.get('scientific_domain', '')
                if orcid_id and email:
                    profile_emails[orcid_id] = email
                if orcid_id and scientific_domain:
                    profile_domains[orcid_id] = scientific_domain

            try:
                cur.execute(
                    f"""
                    SELECT orcid_id, email, COALESCE(scientific_domain, '') AS scientific_domain
                    FROM {db_storage.TABLE_NAME}
                    WHERE orcid_id = ANY(%s)
                      AND (email <> '' OR COALESCE(scientific_domain, '') <> '');
                    """,
                    (missing_orcids,),
                )
            except Exception:
                cur.execute(
                    f"""
                    SELECT orcid_id, email, '' AS scientific_domain
                    FROM {db_storage.TABLE_NAME}
                    WHERE orcid_id = ANY(%s)
                      AND email <> '';
                    """,
                    (missing_orcids,),
                )
            for row in cur.fetchall():
                orcid_id = row.get('orcid_id', '')
                email = row.get('email', '')
                scientific_domain = row.get('scientific_domain', '')
                if orcid_id and email:
                    sent_emails[orcid_id] = email
                if orcid_id and scientific_domain:
                    sent_domains[orcid_id] = scientific_domain
    except Exception:
        return False

    changed = False
    for author in authors:
        orcid_id = author.get('orcid_id', '')
        if not orcid_id:
            continue

        if not author.get('email'):
            recovered_email = profile_emails.get(orcid_id) or sent_emails.get(orcid_id)
            if recovered_email:
                author['email'] = recovered_email
                author['email_source'] = 'db'
                changed = True

        if not author.get('scientific_domain'):
            recovered_domain = profile_domains.get(orcid_id) or sent_domains.get(orcid_id)
            if recovered_domain:
                author['scientific_domain'] = recovered_domain
                changed = True

    return changed


def _get_recent_publications(author: dict, limit: int = 3, force_refresh: bool = False) -> list:
    """Fetch and cache recent OpenAlex publications for one author."""
    if not force_refresh and author.get('recent_publications_cached'):
        return author.get('recent_publications', []) or []

    author_id = author.get('author_id')
    if not author_id:
        author['recent_publications'] = []
        author['recent_publications_cached'] = True
        return []

    client = OpenAlexClient()
    publications = client.fetch_recent_works(author_id, limit=limit)
    author['recent_publications'] = publications
    author['recent_publications_cached'] = True

    # Keep cached publications with the visible results batch when possible.
    for result in st.session_state.app_state.get('search_results', []):
        if result.get('orcid_id') == author.get('orcid_id'):
            result['recent_publications'] = publications
            result['recent_publications_cached'] = True
            break
    _sync_current_batch_cache()
    save_state()
    return publications


def _get_search_pagination_state():
    """Return the active OpenAlex search pagination state."""
    return st.session_state.app_state.setdefault('search_pagination', {})


def _get_result_limit(search_state):
    """Return the capped result count for the active search."""
    total_count = int(search_state.get('total_count', 0) or 0)
    max_results = int(search_state.get('max_results', total_count) or total_count)
    if total_count <= 0:
        return 0
    return min(total_count, max_results) if max_results > 0 else total_count


def _set_current_batch(search_state, batch_payload, start_cursor):
    """Persist the currently visible batch in session and saved app state."""
    checkpoints = search_state.setdefault('checkpoints', {'0': '*'})
    batch_cache = search_state.setdefault('batch_cache', {})

    for author in batch_payload['results']:
        author.setdefault('email', None)

    next_cursor = batch_payload.get('next_cursor')
    if next_cursor:
        checkpoints[str(batch_payload['batch_index'] + 1)] = next_cursor

    current_count = batch_payload.get('count', 0)
    search_state['active'] = True
    search_state['current_batch_index'] = batch_payload['batch_index']
    search_state['current_cursor'] = start_cursor
    search_state['next_cursor'] = next_cursor
    search_state['current_batch_results'] = batch_payload['results']
    search_state['current_range_start'] = batch_payload['start_index'] + 1 if current_count else 0
    search_state['current_range_end'] = batch_payload['end_index']
    batch_cache[str(batch_payload['batch_index'])] = batch_payload['results']

    st.session_state.app_state['search_results'] = batch_payload['results']
    st.session_state.results_page = 0
    st.session_state.filtered_authors = []


def _sync_current_batch_cache():
    """Keep the cached current batch aligned with the visible search results."""
    search_state = _get_search_pagination_state()
    if not search_state.get('active'):
        return

    batch_index = int(search_state.get('current_batch_index', 0) or 0)
    results = st.session_state.app_state.get('search_results', [])
    search_state.setdefault('batch_cache', {})[str(batch_index)] = results
    search_state['current_batch_results'] = results


def load_search_batch(target_batch_index, jump_size=None, reset=False):
    """Load one cursor-based batch for the active search, using known checkpoints."""
    search_state = _get_search_pagination_state()
    search_filters = search_state.get('filters')
    if not search_filters:
        return False

    current_jump_size = int(search_state.get('jump_size', 250) or 250)
    if jump_size is not None:
        jump_size = int(jump_size)
    else:
        jump_size = current_jump_size

    if reset or jump_size != current_jump_size:
        search_state['jump_size'] = jump_size
        search_state['checkpoints'] = {'0': '*'}
        search_state['current_batch_index'] = 0
        search_state['current_cursor'] = '*'
        search_state['next_cursor'] = None
        search_state['current_batch_results'] = []
        target_batch_index = 0

    total_limit = _get_result_limit(search_state)
    if total_limit <= 0:
        search_state['total_batches'] = 0
        st.session_state.app_state['search_results'] = []
        save_state()
        return True

    total_batches = (total_limit + jump_size - 1) // jump_size
    target_batch_index = max(0, min(int(target_batch_index), max(total_batches - 1, 0)))
    search_state['jump_size'] = jump_size
    search_state['total_batches'] = total_batches

    checkpoints = search_state.setdefault('checkpoints', {'0': '*'})
    batch_cache = search_state.setdefault('batch_cache', {})
    known_batches = sorted(int(batch) for batch in checkpoints)
    start_batch = max(batch for batch in known_batches if batch <= target_batch_index)
    start_cursor = checkpoints[str(start_batch)]

    cache_key = str(target_batch_index)
    if not reset and cache_key in batch_cache:
        cached_results = batch_cache[cache_key]
        current_payload = {
            'results': cached_results,
            'next_cursor': checkpoints.get(str(target_batch_index + 1)),
            'batch_index': target_batch_index,
            'batch_size': jump_size,
            'count': len(cached_results),
            'start_index': target_batch_index * jump_size,
            'end_index': (target_batch_index * jump_size) + len(cached_results),
            'has_more': bool(checkpoints.get(str(target_batch_index + 1))),
        }
        _set_current_batch(search_state, current_payload, checkpoints.get(cache_key, '*'))
        save_state()
        return True

    client = OpenAlexClient()
    cursor = start_cursor
    current_payload = None

    for batch_index in range(start_batch, target_batch_index + 1):
        remaining = total_limit - (batch_index * jump_size)
        if remaining <= 0:
            break

        batch_payload = client.fetch_author_batch(
            h_index_min=search_filters.get('h_index_min'),
            h_index_max=search_filters.get('h_index_max'),
            include_country_codes=search_filters.get('include_country_codes'),
            exclude_country_codes=search_filters.get('exclude_country_codes'),
            topic_ids=search_filters.get('topic_ids'),
            require_orcid=search_filters.get('require_orcid', True),
            cursor=cursor,
            batch_size=min(jump_size, remaining),
            batch_index=batch_index,
        )

        if batch_index == target_batch_index:
            current_payload = batch_payload

        next_cursor = batch_payload.get('next_cursor')
        if next_cursor:
            checkpoints[str(batch_index + 1)] = next_cursor
            cursor = next_cursor
        else:
            cursor = None
            break

    if current_payload is None:
        return False

    _set_current_batch(search_state, current_payload, start_cursor)
    save_state()
    return True


# PostgreSQL storage for persistent sent tracking
@st.cache_resource
def get_db():
    """Get cached PostgreSQL storage instance."""
    return get_db_storage()

db_storage = get_db()
if EMAIL_AVAILABLE and db_storage.available:
    start_bulk_email_daemon()


def _import_sent_csv(uploaded_file):
    """Import sent invitations from an uploaded CSV file."""
    import csv, io
    try:
        text = uploaded_file.read().decode("utf-8")
        reader = csv.DictReader(io.StringIO(text))
        rows = []
        for row in reader:
            orcid_id = row.get("orcid_id", "").strip()
            if not orcid_id:
                continue
            rows.append((
                orcid_id,
                row.get("author_name", "").strip(),
                row.get("email", "").strip(),
                row.get("publisher", "").strip(),
                row.get("sent_at", "").strip() or None,
            ))
        if not rows:
            st.warning("No valid rows found in CSV.")
            return
        with st.spinner(f"Importing {len(rows)} sent invitations..."):
            import psycopg2.extras
            with db_storage._get_cursor() as cur:
                psycopg2.extras.execute_values(
                    cur,
                    """INSERT INTO sent_invitations (orcid_id, author_name, email, publisher, sent_at)
                       VALUES %s ON CONFLICT (orcid_id) DO NOTHING""",
                    rows,
                    page_size=1000,
                )
                typed_rows = [
                    (orcid_id, "editorial", author_name, email, publisher, "", "", "", "", sent_at)
                    for orcid_id, author_name, email, publisher, sent_at in rows
                ]
                psycopg2.extras.execute_values(
                    cur,
                    """INSERT INTO author_invitations
                       (orcid_id, invitation_type, author_name, email, publisher, journal_name,
                        template_id, cite_score, quartile, sent_at)
                       VALUES %s ON CONFLICT (orcid_id, invitation_type, journal_name) DO NOTHING""",
                    typed_rows,
                    page_size=1000,
                )
        st.success(f"Imported {len(rows)} sent invitations.")
        st.rerun()
    except Exception as e:
        st.error(f"Import failed: {e}")


def _import_retraction_csv(uploaded_file):
    """Import retraction watch data from an uploaded CSV file."""
    import csv, io
    try:
        existing = db_storage.get_retracted_count()
        if existing > 0:
            st.warning(f"retracted_authors already has {existing} rows. Clear first to re-import.")
            return
        text = uploaded_file.read().decode("utf-8", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        total = 0
        batch = []
        batch_size = 5000
        progress = st.progress(0, text="Reading retraction data...")
        import psycopg2.extras
        for i, row in enumerate(reader):
            authors_str = row.get("Author", "")
            if not authors_str:
                continue
            record_id = row.get("Record ID", "").strip()
            journal = row.get("Journal", "").strip()
            publisher = row.get("Publisher", "").strip()
            retraction_date = row.get("RetractionDate", "").strip()
            reason = row.get("Reason", "").strip()
            for name in authors_str.split(";"):
                name = name.strip()
                if not name:
                    continue
                batch.append((name, name.lower(), record_id, journal, publisher, retraction_date, reason))
            if len(batch) >= batch_size:
                with db_storage._get_cursor() as cur:
                    psycopg2.extras.execute_values(
                        cur,
                        """INSERT INTO retracted_authors
                           (author_name, author_name_lower, record_id, journal, publisher, retraction_date, reason)
                           VALUES %s""",
                        batch,
                        page_size=2000,
                    )
                total += len(batch)
                batch = []
                progress.progress(min(i / 70000, 0.99), text=f"Inserted {total} author entries...")
        if batch:
            with db_storage._get_cursor() as cur:
                psycopg2.extras.execute_values(
                    cur,
                    """INSERT INTO retracted_authors
                       (author_name, author_name_lower, record_id, journal, publisher, retraction_date, reason)
                       VALUES %s""",
                    batch,
                    page_size=2000,
                )
            total += len(batch)
        progress.progress(1.0, text="Done!")
        st.success(f"Imported {total} retracted author entries.")
        st.rerun()
    except Exception as e:
        st.error(f"Import failed: {e}")


def get_sent_invitations(
    invitation_type: str = INVITATION_TYPE_EDITORIAL,
    journal_name: str = ""
) -> set:
    """Get sent ORCID IDs for the selected invitation type, merged with local state."""
    journal_filter = journal_name if invitation_type == INVITATION_TYPE_PUBLICATION else None
    db_sent = db_storage.get_all_sent(invitation_type, journal_filter) if db_storage.available else set()

    local_sent = st.session_state.app_state.get('sent_invitations', set())
    if isinstance(local_sent, list):
        local_sent = set(local_sent)

    local_records = st.session_state.app_state.get('sent_invitation_records', [])
    typed_local = {
        record.get('orcid_id')
        for record in local_records
        if record.get('invitation_type') == invitation_type
        and record.get('journal_name', '') == (journal_name or '')
    }

    if invitation_type == INVITATION_TYPE_EDITORIAL:
        typed_local |= local_sent

    return db_sent | {orcid for orcid in typed_local if orcid}


def is_author_notified(
    orcid_id: str,
    invitation_type: str = INVITATION_TYPE_EDITORIAL,
    journal_name: str = ""
) -> bool:
    """Check if author was notified for the selected invitation type."""
    journal_filter = journal_name if invitation_type == INVITATION_TYPE_PUBLICATION else None
    if db_storage.available:
        return db_storage.is_sent(orcid_id, invitation_type, journal_filter)
    
    sent = st.session_state.app_state.get('sent_invitations', set())
    if isinstance(sent, list):
        sent = set(sent)
    if invitation_type == INVITATION_TYPE_EDITORIAL and orcid_id in sent:
        return True

    local_records = st.session_state.app_state.get('sent_invitation_records', [])
    expected_key = _typed_invitation_key(orcid_id, invitation_type, journal_name)
    for record in local_records:
        if _typed_invitation_key(
            record.get('orcid_id'),
            record.get('invitation_type'),
            record.get('journal_name', '')
        ) == expected_key:
            return True
    return False


def is_recipient_suppressed(email: str, orcid_id: str = "", profile_key: str = "") -> bool:
    """Check whether a recipient should be blocked from future sends."""
    if db_storage.available:
        return db_storage.is_recipient_suppressed(email, orcid_id=orcid_id, profile_key=profile_key)
    return False


def mark_author_notified(
    orcid_id: str,
    author_name: str = "",
    email: str = "",
    publisher: str = "",
    invitation_type: str = INVITATION_TYPE_EDITORIAL,
    journal_name: str = "",
    template_id: str = "",
    cite_score: str = "",
    quartile: str = ""
) -> bool:
    """Mark author as notified in DB and local state. Returns True if DB save succeeded."""
    db_ok = True
    if db_storage.available:
        db_ok = db_storage.mark_sent(
            orcid_id,
            author_name,
            email,
            publisher,
            invitation_type=invitation_type,
            journal_name=journal_name if invitation_type == INVITATION_TYPE_PUBLICATION else "",
            template_id=template_id,
            cite_score=cite_score,
            quartile=quartile,
        )

    # Always update local state (backup / offline)
    sent = st.session_state.app_state.get('sent_invitations', set())
    if isinstance(sent, list):
        sent = set(sent)
    if invitation_type == INVITATION_TYPE_EDITORIAL:
        sent.add(orcid_id)
    st.session_state.app_state['sent_invitations'] = sent

    local_records = st.session_state.app_state.setdefault('sent_invitation_records', [])
    record = {
        'orcid_id': orcid_id,
        'invitation_type': invitation_type,
        'journal_name': journal_name if invitation_type == INVITATION_TYPE_PUBLICATION else ""
    }
    if record not in local_records:
        local_records.append(record)

    save_state()
    return db_ok


def _public_app_base_url() -> str:
    """Return the public base URL used in outbound email links."""
    return (
        os.environ.get("PUBLIC_APP_BASE_URL")
        or os.environ.get("PUBLIC_ASSET_BASE_URL")
        or "https://editorial-board-app-production.up.railway.app"
    ).strip().rstrip("/")


def _build_unsubscribe_url(email: str, orcid_id: str = "", profile_key: str = "") -> str:
    """Build a durable unsubscribe URL for one recipient."""
    normalized_email = (email or "").strip().lower()
    if not normalized_email:
        return ""

    suppression = None
    if db_storage.available:
        suppression = db_storage.register_unsubscribe_token(
            normalized_email,
            orcid_id=orcid_id,
            profile_key=profile_key,
        )
    if not suppression:
        return ""
    token = suppression.get("unsubscribe_token", "")
    if not token:
        return ""
    return f"{_public_app_base_url()}/?action=unsubscribe&token={token}"


def _get_query_params() -> dict:
    """Read Streamlit query params in a version-tolerant way."""
    try:
        params = dict(st.query_params)
    except Exception:
        try:
            params = st.experimental_get_query_params()
        except Exception:
            params = {}

    flat: dict[str, str] = {}
    for key, value in params.items():
        if isinstance(value, list):
            flat[key] = value[0] if value else ""
        else:
            flat[key] = value
    return flat


def _handle_unsubscribe_request() -> bool:
    """Process a public unsubscribe request and stop the app when handled."""
    params = _get_query_params()
    action = (params.get("action") or "").strip().lower()
    token = (params.get("token") or "").strip()
    if action != "unsubscribe" or not token:
        return False

    st.title("Unsubscribe")
    if not db_storage.available:
        st.error("Unsubscribe is temporarily unavailable because the database is offline.")
        st.stop()

    record = db_storage.get_email_suppression_by_token(token)
    if not record:
        st.error("This unsubscribe link is invalid or has expired.")
        st.stop()

    updated = db_storage.suppress_recipient(
        record.get("email_lower", ""),
        orcid_id=record.get("orcid_id", ""),
        profile_key=record.get("profile_key", ""),
        reason="Unsubscribed by recipient",
        source="unsubscribe_link",
    )
    if not updated:
        st.error("We could not complete the unsubscribe request. Please contact the editorial office.")
        st.stop()

    email = updated.get("email_lower", "")
    if email:
        st.success(f"{email} has been removed from future email invitations.")
        st.caption("We keep a minimal suppression record so we do not email you again.")
    else:
        st.success("You have been removed from future email invitations.")
    st.stop()


_handle_unsubscribe_request()


def _extract_email_from_text(value: str) -> str:
    """Find the first email address in a pasted unsubscribe request."""
    match = re.search(r"[\w.+%-]+@[\w.-]+\.[A-Za-z]{2,}", value or "")
    return match.group(0).lower() if match else ""


def _purge_local_suppressed_email(email: str) -> None:
    """Remove a suppressed email from current Streamlit session caches."""
    normalized = (email or "").strip().lower()
    if not normalized:
        return
    changed = False
    for author in st.session_state.app_state.get("search_results", []) or []:
        author_email = (author.get("email") or "").strip().lower()
        all_emails = (author.get("all_emails") or "").strip().lower()
        if author_email == normalized or normalized in all_emails:
            author["email"] = ""
            author["all_emails"] = ""
            author["email_source"] = ""
            author["email_status"] = "suppressed"
            changed = True
    if changed:
        save_state()


def render_manual_unsubscribe_tool() -> None:
    """Render an admin tool for recipient unsubscribe replies."""
    st.subheader("Manual Unsubscribe")
    if not db_storage.available:
        st.caption("Database connection is required.")
        return

    with st.form("manual_unsubscribe_form", clear_on_submit=True):
        request_text = st.text_area(
            "Email or pasted reply",
            placeholder="name@example.com or paste the unsubscribe reply",
            height=90,
        )
        submitted = st.form_submit_button("Suppress and purge")

    if not submitted:
        return

    email = _extract_email_from_text(request_text)
    if not email:
        st.warning("No valid email address was found.")
        return

    suppressed = db_storage.suppress_recipient(
        email,
        reason="Manual unsubscribe request",
        source="manual_admin",
    )
    if not suppressed:
        st.error("Could not suppress this recipient. Please check the database connection.")
        return

    _purge_local_suppressed_email(email)
    st.success(f"{email} is suppressed and purged from mailing sources.")


@st.dialog("Send Invitation Email", width="large")
def email_dialog(author: dict, filters: dict):
    """Dialog for composing and sending invitation email to a specific author."""
    
    journal_config = st.session_state.app_state.get('journal_config', {})
    publisher_id = filters.get('publisher', 'brevo')
    author_key = author.get('orcid_id') or author.get('author_id') or author.get('name', 'author')
    invitation_type = filters.get('invitation_type', INVITATION_TYPE_EDITORIAL)
    if invitation_type not in {INVITATION_TYPE_EDITORIAL, INVITATION_TYPE_PUBLICATION}:
        invitation_type = INVITATION_TYPE_EDITORIAL

    workflow = WORKFLOW_AUTHOR if invitation_type == INVITATION_TYPE_PUBLICATION else WORKFLOW_EDITORIAL
    st.caption(f"Workflow: {_workflow_label(workflow)}")
    tracking_journal_name = _tracking_journal_name(invitation_type, journal_config)
    
    tracking_id = _recipient_tracking_id(author)
    is_already_notified = is_author_notified(
        tracking_id,
        invitation_type=invitation_type,
        journal_name=tracking_journal_name
    )
    
    # WARNING BANNER for already notified authors
    if is_already_notified:
        st.error(f"⚠️ WARNING: This author has ALREADY been sent a {_invitation_type_label(invitation_type)} invitation for this tracking scope.")
    initial_recipient_suppressed = is_recipient_suppressed(
        author.get('email', ''),
        author.get('orcid_id', ''),
        author.get('profile_key', ''),
    )
    if initial_recipient_suppressed:
        st.warning("This recipient has unsubscribed before. Sending is blocked.")
    
    # Author info header
    st.markdown(f"### To: **{author['name']}**")
    if author.get('institution'):
        st.caption(f"{author.get('institution')} | H-index: {author.get('h_index', 'N/A')}")
    if invitation_type == INVITATION_TYPE_PUBLICATION:
        st.caption("Publication invitation subjects include the author name automatically.")
    
    st.divider()

    recent_publications_text = ""
    if invitation_type == INVITATION_TYPE_PUBLICATION:
        if not journal_config.get('submission_link'):
            st.warning("Add a journal submission link in the sidebar before sending publication invitations.")

        include_publications = st.checkbox(
            "Mention recent OpenAlex publications",
            value=True,
            key=f"dialog_include_publications_{author_key}"
        )
        if include_publications:
            refresh_publications = st.button(
                "Refresh recent publications",
                key=f"dialog_refresh_publications_{author_key}"
            )
            with st.spinner("Loading recent publications from OpenAlex..."):
                recent_publications = _get_recent_publications(author, limit=3, force_refresh=refresh_publications)
            if recent_publications:
                with st.expander("Recent publications used for personalization", expanded=True):
                    for publication in recent_publications:
                        year = publication.get('year') or 'N/A'
                        source = publication.get('source') or 'Unknown source'
                        st.write(f"- {publication.get('title', '')} ({year}, {source})")
                recent_publications_text = format_recent_publications(recent_publications)
            else:
                st.caption("No recent OpenAlex publications were available; the template will still send without that section.")
    
    # Template selection
    template_names = get_template_names(invitation_type)
    col_type, col_scopus = st.columns([2, 1])
    with col_type:
        template_id = st.selectbox(
            "Template",
            options=list(template_names.keys()),
            format_func=lambda x: template_names[x],
            key=f"dialog_template_{invitation_type}_{author_key}"
        )
    with col_scopus:
        if invitation_type == INVITATION_TYPE_EDITORIAL:
            scopus_indexed = st.checkbox("Journal is Scopus indexed", value=False, key=f"dialog_scopus_{author_key}")
        else:
            scopus_indexed = False
            st.caption("Publication templates use the journal metadata fields.")
    
    # Format template - publisher name and location come from PUBLISHER_INFO (follows selected publisher)
    pub_info = PUBLISHER_INFO.get(publisher_id, {})
    publisher_name = pub_info.get('name') or (email_sender.get_publisher_name(publisher_id) if EMAIL_AVAILABLE else "")
    publisher_location = pub_info.get('location') or journal_config.get('location', '')
    sender_email = email_sender.get_publisher_email(publisher_id) if EMAIL_AVAILABLE else ""
    
    formatted = format_template(
        template_id=template_id,
        author_name=author['name'],
        journal_name=journal_config.get('name', ''),
        journal_issn=journal_config.get('issn', ''),
        journal_link=journal_config.get('link', ''),
        editor_in_chief_name=journal_config.get('editor_in_chief', ''),
        publisher_name=publisher_name,
        sender_email=sender_email,
        publisher_location=publisher_location,
        scopus_indexed=scopus_indexed,
        journal_submission_link=journal_config.get('submission_link', ''),
        journal_cite_score=journal_config.get('cite_score', ''),
        journal_quartile=journal_config.get('quartile', ''),
        journal_indexing_status=journal_config.get('indexing_status', ''),
        author_specialty=author.get('specialty') or author.get('research_areas') or '',
        author_recent_publications=recent_publications_text,
        journal_scope=journal_config.get('scope', ''),
        invitation_goal=journal_config.get('invitation_goal', '')
    )
    
    # Editable email fields
    to_email = st.text_input(
        "To (Email)",
        value=author.get('email', ''),
        key=f"dialog_to_{author_key}_{invitation_type}"
    )
    recipient_suppressed = is_recipient_suppressed(
        to_email,
        author.get('orcid_id', ''),
        author.get('profile_key', ''),
    )
    if recipient_suppressed and not initial_recipient_suppressed:
        st.warning("This email address is suppressed. Sending is blocked.")
    subject = st.text_input(
        "Subject",
        value=formatted['subject'],
        key=f"dialog_subject_{author_key}_{invitation_type}_{template_id}_{scopus_indexed}_{bool(recent_publications_text)}"
    )
    
    body = st.text_area(
        "Email Body",
        value=formatted['body'],
        height=300,
        key=f"dialog_body_{author_key}_{invitation_type}_{template_id}_{scopus_indexed}_{bool(recent_publications_text)}"
    )
    
    # PDF option
    col1, col2 = st.columns(2)
    with col1:
        attach_pdf = st.checkbox("Attach PDF invitation letter", value=True, key=f"dialog_pdf_{author_key}_{invitation_type}")
    
    # Preview PDF
    if attach_pdf:
        with st.expander("Preview PDF"):
            try:
                pdf_bytes = generate_invitation_pdf(
                    publisher_id=publisher_id,
                    recipient_name=author['name'],
                    email_body=body,
                    subject=subject,
                    journal_name=journal_config.get('name', ''),
                    journal_link=journal_config.get('link', '')
                )
                st.download_button(
                    "Download PDF Preview",
                    data=pdf_bytes,
                    file_name="Publication_Invitation_Preview.pdf" if invitation_type == INVITATION_TYPE_PUBLICATION else "Invitation_Letter_Preview.pdf",
                    mime="application/pdf"
                )
            except Exception as e:
                st.error(f"PDF error: {str(e)}")
    
    st.divider()
    
    # Confirmation checkbox for already notified authors
    confirm_resend = True  # Default to allowed
    if is_already_notified:
        confirm_resend = st.checkbox(
            f"I confirm I want to send ANOTHER {_invitation_type_label(invitation_type)} invitation to this author",
            value=False,
            key=f"dialog_confirm_resend_{invitation_type}_{author_key}"
        )
    
    # Action buttons
    col1, col2 = st.columns(2)
    
    with col1:
        if st.button("Cancel", use_container_width=True):
            st.rerun()
    
    with col2:
        send_disabled = (
            not EMAIL_AVAILABLE
            or not to_email
            or recipient_suppressed
            or (is_already_notified and not confirm_resend)
        )
        if st.button("Send Email", type="primary", use_container_width=True, disabled=send_disabled):
            with st.spinner("Sending..."):
                pdf_bytes = None
                if attach_pdf:
                    try:
                        pdf_bytes = generate_invitation_pdf(
                            publisher_id=publisher_id,
                            recipient_name=author['name'],
                            email_body=body,
                            subject=subject,
                            journal_name=journal_config.get('name', ''),
                            journal_link=journal_config.get('link', '')
                        )
                    except Exception as e:
                        st.error(f"PDF generation failed: {str(e)}")
                        pdf_bytes = None
                
                success, msg = email_sender.send_email(
                    publisher_id=publisher_id,
                    to_email=to_email,
                    subject=subject,
                    body=body,
                    to_name=author['name'],
                    pdf_attachment=pdf_bytes,
                    attachment_filename="Publication_Invitation_Letter.pdf" if invitation_type == INVITATION_TYPE_PUBLICATION else "Invitation_Letter.pdf",
                    journal_name=journal_config.get('name', ''),
                    journal_link=journal_config.get('link', ''),
                    submission_link=journal_config.get('submission_link', ''),
                    invitation_type=invitation_type,
                    scopus_indexed=scopus_indexed,
                    journal_cite_score=journal_config.get('cite_score', ''),
                    journal_quartile=journal_config.get('quartile', ''),
                    unsubscribe_url=_build_unsubscribe_url(to_email, author.get('orcid_id', '')),
                )
                
                if success:
                    send_tracking_id = _recipient_tracking_id(author, to_email)
                    db_ok = mark_author_notified(
                        send_tracking_id,
                        author_name=author.get('name', ''),
                        email=to_email,
                        publisher=publisher_id,
                        invitation_type=invitation_type,
                        journal_name=tracking_journal_name,
                        template_id=template_id,
                        cite_score=journal_config.get('cite_score', ''),
                        quartile=journal_config.get('quartile', '')
                    )
                    purge_requested = bool(filters.get('suppress_after_send'))
                    purge_ok = False
                    if purge_requested and db_storage.available:
                        purge_ok = bool(db_storage.suppress_recipient(
                            to_email,
                            orcid_id=author.get('orcid_id', ''),
                            profile_key=author.get('profile_key', ''),
                            reason="Invitation delivered; prevent duplicate outreach",
                            source="automatic_post_send",
                        ))
                        if purge_ok:
                            _purge_local_suppressed_email(to_email)
                    if purge_requested and purge_ok:
                        st.success(f"Email sent to {to_email} and removed from future outreach!")
                    else:
                        st.success(f"Email sent to {to_email}!")
                    if not db_ok:
                        st.warning("Sent status could not be saved to the database; it may not persist across sessions.")
                    if purge_requested and db_storage.available and not purge_ok:
                        st.warning("The email was sent, but automatic suppression and purge did not complete.")
                    time.sleep(1)
                    st.rerun()
                else:
                    st.error(f"Failed: {msg}")
    
    if not EMAIL_AVAILABLE:
        st.warning("Email sending not configured. Add email_credentials.json.")


@st.dialog("Confirm Background Bulk Send", width="large")
def bulk_send_preview_dialog(payload: dict, confirmation_key: str):
    """Preview one sample bulk email and require explicit confirmation."""
    batch = payload.get('batch') or []
    invitation_type = payload.get('invitation_type', INVITATION_TYPE_EDITORIAL)
    publisher_id = payload.get('publisher_id', 'brevo')
    journal_config = payload.get('journal_config', {}) or {}
    bulk_template_strategy = payload.get('bulk_template_strategy', 'Use selected template')
    selected_bulk_template = payload.get('selected_bulk_template', TEMPLATE_BOARD_MEMBER)
    bulk_scopus_indexed = bool(payload.get('bulk_scopus_indexed', False))
    bulk_attach_pdf = bool(payload.get('bulk_attach_pdf', True))
    bulk_include_cached_publications = bool(payload.get('bulk_include_cached_publications', False))
    dialog_key = payload.get('dialog_key', 'bulk_preview')

    if not batch:
        st.warning("No eligible authors in this bulk batch.")
        if st.button("Close", use_container_width=True, key=f"bulk_preview_close_{dialog_key}"):
            st.rerun()
        return

    bulk_template_names = get_template_names(invitation_type)
    template_name = bulk_template_names.get(selected_bulk_template, selected_bulk_template)
    sample_author = batch[0]
    st.warning(
        f"You are about to queue **{len(batch)}** {_invitation_type_label(invitation_type).lower()} emails. "
        f"The worker will send them in the background, so the browser can be closed."
    )
    st.caption(
        f"Sample recipient: {sample_author.get('name', 'Author')} "
        f"<{sample_author.get('email', '')}> | Template: {template_name} | "
        f"Publisher: {_publisher_display_label(publisher_id)} | "
        f"PDF attachment: {'Yes' if bulk_attach_pdf else 'No'}"
    )

    col_cancel, col_confirm = st.columns(2)
    with col_cancel:
        if st.button("Cancel", use_container_width=True, key=f"bulk_preview_cancel_{dialog_key}"):
            st.rerun()
    with col_confirm:
        if st.button(
            f"Queue {len(batch)} Background Emails",
            type="primary",
            use_container_width=True,
            disabled=not EMAIL_AVAILABLE,
            key=f"bulk_preview_confirm_{dialog_key}",
        ):
            st.session_state[confirmation_key] = payload
            st.rerun()


def render_sidebar():
    """Render the sidebar with all configuration options."""
    
    with st.sidebar:
        st.title("Configuration")
        
        # Database Status Indicator
        db_status = db_storage.get_status()
        if db_status["available"]:
            st.success("🟢 Database: Connected")
        else:
            st.error(f"🔴 Database: Offline")
            if db_status["error"]:
                st.caption(f"Error: {db_status['error'][:50]}...")
        
        st.divider()
        render_manual_unsubscribe_tool()
        st.divider()

        publisher_options = {}
        if EMAIL_AVAILABLE:
            publishers = email_sender.get_publishers()
            publisher_options = {p['id']: f"{p['name']}" for p in publishers}

        # Saved Journal Presets
        st.subheader("Saved Journal Presets")
        if not db_storage.available:
            st.info("Journal presets require PostgreSQL. Current draft fields still work for this session.")
        else:
            st.caption("Load a saved setup, or edit the fields below and save/update it here.")
            presets = db_storage.list_journal_presets()
            preset_labels = {
                str(preset.get("id")): preset.get("preset_name", f"Preset {preset.get('id')}")
                for preset in presets
            }
            preset_by_id = {str(preset.get("id")): preset for preset in presets}
            selected_preset_id = st.selectbox(
                "Load saved preset",
                options=[""] + list(preset_labels.keys()),
                format_func=lambda value: "Select preset" if not value else preset_labels.get(value, value),
                key="journal_preset_select",
            )
            selected_preset = preset_by_id.get(selected_preset_id)
            if selected_preset and st.session_state.get("journal_preset_name_source") != selected_preset_id:
                st.session_state["journal_preset_name"] = selected_preset.get("preset_name", "")
                st.session_state["journal_preset_name_source"] = selected_preset_id

            preset_name = st.text_input(
                "Preset name",
                placeholder="e.g., Babylonian Journal of Internet of Things",
                key="journal_preset_name",
            ).strip()

            col_load, col_save = st.columns(2)
            with col_load:
                load_clicked = st.button("Load", use_container_width=True, disabled=not bool(selected_preset))
            with col_save:
                save_clicked = st.button("Save as new", use_container_width=True, disabled=not bool(preset_name))

            col_update, col_delete = st.columns(2)
            with col_update:
                update_clicked = st.button(
                    "Update selected",
                    use_container_width=True,
                    disabled=not (bool(selected_preset) and bool(preset_name)),
                )
            with col_delete:
                delete_clicked = st.button("Delete selected", use_container_width=True, disabled=not bool(selected_preset))

            if load_clicked and selected_preset:
                loaded_config = normalize_journal_preset_config(selected_preset.get("journal_config", {}))
                loaded_publisher = (selected_preset.get("publisher_id") or "").strip()
                if publisher_options and loaded_publisher not in publisher_options:
                    st.warning("Preset loaded, but its saved publisher is not configured in this app.")
                elif loaded_publisher:
                    st.session_state.app_state["publisher"] = loaded_publisher
                    st.session_state["publisher_select"] = loaded_publisher
                st.session_state.app_state["journal_config"] = loaded_config
                _sync_journal_preset_widget_state(loaded_config)
                save_state()
                st.success(f"Loaded preset: {selected_preset.get('preset_name')}")
                st.rerun()

            if save_clicked:
                new_id = db_storage.create_journal_preset(
                    preset_name=preset_name,
                    publisher_id=st.session_state.app_state.get("publisher", "brevo"),
                    journal_config=_current_journal_preset_config(),
                )
                if new_id:
                    st.success("Preset saved.")
                    st.rerun()
                else:
                    st.error("Could not save preset. Check that the name is unique.")

            if update_clicked and selected_preset:
                updated = db_storage.update_journal_preset(
                    preset_id=int(selected_preset["id"]),
                    preset_name=preset_name,
                    publisher_id=st.session_state.app_state.get("publisher", "brevo"),
                    journal_config=_current_journal_preset_config(),
                )
                if updated:
                    st.success("Preset updated.")
                    st.rerun()
                else:
                    st.error("Could not update preset. Check that the name is unique.")

            if delete_clicked and selected_preset:
                deleted = db_storage.delete_journal_preset(int(selected_preset["id"]))
                if deleted:
                    st.success("Preset deleted.")
                    st.rerun()
                else:
                    st.error("Could not delete preset.")

        st.divider()
        
        # Publisher Selection
        st.subheader("Publisher")
        
        if EMAIL_AVAILABLE:
            current_publisher = st.session_state.app_state.get('publisher', 'brevo')
            if publisher_options and current_publisher not in publisher_options:
                current_publisher = list(publisher_options.keys())[0]
                st.session_state.app_state['publisher'] = current_publisher
                save_state()
            
            selected_publisher = st.selectbox(
                "Select Publisher",
                options=list(publisher_options.keys()),
                format_func=lambda x: publisher_options[x],
                index=list(publisher_options.keys()).index(current_publisher) if current_publisher in publisher_options else 0,
                key="publisher_select"
            )
            
            if selected_publisher != current_publisher:
                st.session_state.app_state['publisher'] = selected_publisher
                save_state()
            
            if st.button("Test Email Connection", use_container_width=True):
                with st.spinner("Testing..."):
                    success, msg = email_sender.test_connection(selected_publisher)
                if success:
                    st.success(msg)
                else:
                    st.error(msg)
            
            st.caption("Send test email to any address:")
            test_email_to = st.text_input(
                "Send test email to",
                placeholder="e.g. you@example.com",
                key="test_email_to",
                label_visibility="collapsed"
            )
            if st.button("Send test email", use_container_width=True):
                if not test_email_to or not test_email_to.strip():
                    st.warning("Enter an email address.")
                else:
                    test_email_to = test_email_to.strip()
                    with st.spinner("Sending test email..."):
                        success, msg = email_sender.send_email(
                            selected_publisher,
                            to_email=test_email_to,
                            subject="Test email from Editorial Board Invitation Tool",
                            body="This is a test email. If you received this, the sender is configured correctly.",
                            to_name="Test recipient",
                            journal_name="",
                            journal_link="",
                            submission_link="",
                            invitation_type=INVITATION_TYPE_EDITORIAL,
                            scopus_indexed=False,
                            journal_cite_score="",
                            journal_quartile="",
                        )
                    if success:
                        st.success(f"Test email sent to {test_email_to}.")
                    else:
                        st.error(msg)
        else:
            st.warning("Email credentials not found. Create email_credentials.json")
            selected_publisher = 'brevo'

        st.divider()
        
        # Journal Configuration
        st.subheader("Editorial Invitation Settings")
        st.caption("Used by the Editorial Invitation tab.")
        
        journal_config = st.session_state.app_state.get('journal_config', {})
        
        journal_name = st.text_input(
            "Journal Name",
            value=journal_config.get('name', ''),
            placeholder="e.g., SHIFAA Journal",
            key="journal_name"
        )
        
        journal_issn = st.text_input(
            "ISSN",
            value=journal_config.get('issn', ''),
            placeholder="e.g., 1234-5678",
            key="journal_issn"
        )
        
        journal_link = st.text_input(
            "Journal Website",
            value=journal_config.get('link', ''),
            placeholder="e.g., https://journal.example.com",
            key="journal_link"
        )
        
        publisher_location = st.text_input(
            "Publisher Location",
            value=journal_config.get('location', ''),
            placeholder="e.g., Dubai - UAE",
            key="publisher_location"
        )
        
        editor_name = st.text_input(
            "Editor-in-Chief Name",
            value=journal_config.get('editor_in_chief', ''),
            placeholder="e.g., Prof. John Smith",
            key="editor_name"
        )

        st.divider()
        st.subheader("Author Invitation Settings")
        st.caption("Used by the Author Invitation tab.")

        st.markdown("**Publication Invitation Details**")

        submission_link = st.text_input(
            "Journal Submission Link",
            value=journal_config.get('submission_link', ''),
            placeholder="e.g., https://journal.example.com/submit",
            key="journal_submission_link"
        )

        col_metric1, col_metric2 = st.columns(2)
        with col_metric1:
            cite_score = st.text_input(
                "CiteScore",
                value=journal_config.get('cite_score', ''),
                placeholder="e.g., 2.4",
                key="journal_cite_score"
            )
        with col_metric2:
            quartile = st.selectbox(
                "Quartile",
                options=QUARTILE_OPTIONS,
                index=_safe_select_index(QUARTILE_OPTIONS, journal_config.get('quartile', '')),
                key="journal_quartile"
            )

        indexing_status = st.selectbox(
            "Indexing Status",
            options=INDEXING_OPTIONS,
            index=_safe_select_index(INDEXING_OPTIONS, journal_config.get('indexing_status', '')),
            key="journal_indexing_status"
        )

        invitation_goal = st.selectbox(
            "Invitation Goal",
            options=INVITATION_GOAL_OPTIONS,
            index=_safe_select_index(INVITATION_GOAL_OPTIONS, journal_config.get('invitation_goal', 'Regular submission')),
            key="journal_invitation_goal"
        )

        journal_scope = st.text_area(
            "Journal Scope / Fit Note",
            value=journal_config.get('scope', ''),
            placeholder="Optional: short description of the journal scope or current call for papers",
            height=80,
            key="journal_scope"
        )
        
        # Auto-save journal config on change
        new_config = {
            'name': journal_name,
            'issn': journal_issn,
            'link': journal_link,
            'location': publisher_location,
            'editor_in_chief': editor_name,
            'submission_link': submission_link,
            'cite_score': cite_score,
            'quartile': quartile,
            'indexing_status': indexing_status,
            'invitation_goal': invitation_goal,
            'scope': journal_scope,
        }
        
        if new_config != journal_config:
            st.session_state.app_state['journal_config'] = new_config
            save_state()
        
        st.divider()
        
        # Search Filters
        st.subheader("Search Filters")
        
        search_params = st.session_state.app_state.get('search_params', {})
        
        # Keyword Tags - NEW
        st.markdown("**Keyword Tags** (comma-separated)")
        keyword_tags = st.text_area(
            "Keywords",
            value=search_params.get('keyword_tags', ''),
            placeholder="media, journalism, cinema, broadcasting, news",
            help="Enter keywords to search - uses OR logic to find matching topics",
            height=80,
            key="keyword_tags",
            label_visibility="collapsed"
        )
        
        st.markdown("**H-Index Range**")
        col1, col2 = st.columns(2)
        with col1:
            h_min = st.number_input(
                "Min",
                min_value=0,
                max_value=500,
                value=search_params.get('h_index_min', DEFAULT_H_INDEX_MIN),
                key="h_min"
            )
        with col2:
            h_max = st.number_input(
                "Max",
                min_value=0,
                max_value=500,
                value=search_params.get('h_index_max', DEFAULT_H_INDEX_MAX),
                key="h_max"
            )
        
        countries_to_include = st.multiselect(
            "Include Countries",
            options=list(COUNTRIES.keys()),
            default=search_params.get('include_countries', []),
            key="include_countries",
            help="Show authors from any selected country; leave empty for all countries"
        )

        countries_to_exclude = st.multiselect(
            "Exclude Countries",
            options=list(COUNTRIES.keys()),
            default=search_params.get('exclude_countries', []),
            key="exclude_countries",
            help="Authors from these countries will be excluded from results"
        )

        selected_disciplines = st.multiselect(
            "Filter by Discipline",
            options=ALL_DISCIPLINES,
            default=[d for d in search_params.get('disciplines', []) if d in ALL_DISCIPLINES],
            key="sidebar_discipline_filter",
            help="Show only results in the selected disciplines"
        )
        
        max_results = st.number_input(
            "Max Results",
            min_value=10,
            max_value=100000,
            value=search_params.get('max_results', DEFAULT_MAX_RESULTS),
            step=100,
            key="max_results"
        )

        jump_size = st.selectbox(
            "Jump Size",
            options=[250, 500, 1000],
            index=[250, 500, 1000].index(search_params.get('jump_size', 250)),
            key="jump_size",
            help="Load and navigate OpenAlex results in batches of this size"
        )
        
        st.divider()
        
        # Speed Settings
        st.subheader("Email Fetch Settings")
        
        concurrent = st.slider(
            "Concurrent requests",
            min_value=1,
            max_value=20,
            value=10,
            help="Higher = faster but more risk of rate limiting",
            key="concurrent"
        )
        
        delay = st.slider(
            "Batch delay (seconds)",
            min_value=0.5,
            max_value=5.0,
            value=1.0,
            step=0.5,
            key="delay"
        )
        
        st.divider()
        
        # Email Search Options
        st.subheader("Email Search Options")
        
        st.caption("ORCID API is always used first (free)")
        
        use_tavily = st.checkbox(
            "Enable Tavily search",
            value=False,
            help="Tavily + GPT-4o-mini extraction (lower cost)",
            key="use_tavily"
        )
        
        use_openai_web = st.checkbox(
            "Enable OpenAI web search",
            value=False,
            help="OpenAI Responses API with web_search (fallback, higher cost)",
            key="use_openai_web"
        )
        
        if use_tavily or use_openai_web:
            st.caption("🔍 Searches faculty pages, Google Scholar, ResearchGate")
        
        st.divider()
        
        # Data Import Section
        with st.expander("📥 Data Import (Admin)"):
            st.caption("Import CSV data into the database")
            
            # Sent Invitations Import
            sent_file = st.file_uploader(
                "Sent Invitations CSV",
                type=["csv"],
                help="CSV with columns: orcid_id, author_name, email, publisher, sent_at",
                key="sent_csv_upload"
            )
            if sent_file and st.button("Import Sent Invitations", key="btn_import_sent"):
                _import_sent_csv(sent_file)
            
            st.divider()
            
            # Retraction Watch Import
            retract_file = st.file_uploader(
                "Retraction Watch CSV",
                type=["csv"],
                help="CSV with columns: Author (semicolon-separated), Record ID, Journal, Publisher, RetractionDate, Reason",
                key="retract_csv_upload"
            )
            if retract_file and st.button("Import Retraction Watch", key="btn_import_retract"):
                _import_retraction_csv(retract_file)
            
            # Show current counts
            try:
                sent_count = db_storage.get_sent_count()
                retracted_count = db_storage.get_retracted_count()
                st.info(f"DB: {sent_count} sent invitations, {retracted_count} retracted authors")
            except Exception:
                pass
        
        st.divider()
        
        # Reset Button
        if st.button("Reset All Data", type="secondary", use_container_width=True):
            if st.session_state.get('confirm_reset'):
                state_mgr.reset_all()
                st.session_state.app_state = state_mgr.load_state()
                st.session_state.selected_author = None
                st.session_state.confirm_reset = False
                st.rerun()
            else:
                st.session_state.confirm_reset = True
                st.warning("Click again to confirm reset")
        
        return {
            'keyword_tags': keyword_tags,
            'h_min': h_min,
            'h_max': h_max,
            'include_countries': countries_to_include,
            'exclude_countries': countries_to_exclude,
            'disciplines': selected_disciplines,
            'max_results': max_results,
            'jump_size': jump_size,
            'concurrent': concurrent,
            'delay': delay,
            'publisher': selected_publisher,
            'use_tavily': use_tavily,
            'use_openai_web': use_openai_web
        }


def render_database_email_search_panel(filters: dict, ui_scope: str, author_source_mode: str) -> dict:
    """Render the always-visible database search controls for Author Invitation."""
    journal_config = st.session_state.app_state.get('journal_config', {})
    invitation_type = filters.get('invitation_type', INVITATION_TYPE_PUBLICATION)
    tracking_journal_name = _tracking_journal_name(invitation_type, journal_config)
    sent_invitations = get_sent_invitations(invitation_type, tracking_journal_name)
    panel_state = {
        'results': [],
        'total': 0,
        'active': False,
        'query': '',
    }

    st.markdown("### Database Email Search")
    st.info("Search saved email recipients here. Matching people will appear in the send table below with email preview before sending.")
    st.caption("Search by name, email, affiliation, country, ORCID/OpenAlex, discipline, specialty, or research area.")

    if not db_storage.available:
        st.warning("Database email search requires PostgreSQL.")
        return panel_state

    db_search_cols = st.columns([2.4, 1.2, 1, 1, 1])
    with db_search_cols[0]:
        db_search_query = st.text_input(
            "Search database emails",
            placeholder="Name, email, affiliation, ORCID, country, specialty...",
            key=_scope_key(ui_scope, "database_email_search_query"),
        ).strip()
    with db_search_cols[1]:
        db_search_source = st.selectbox(
            "Database source",
            options=["all", "profiles", "harvested"],
            format_func=_database_email_source_label,
            key=_scope_key(ui_scope, "database_email_search_source"),
        )
    with db_search_cols[2]:
        db_require_email = st.checkbox(
            "With email",
            value=True,
            key=_scope_key(ui_scope, "database_email_require_email"),
        )
    with db_search_cols[3]:
        db_hide_suppressed = st.checkbox(
            "Hide suppressed",
            value=True,
            key=_scope_key(ui_scope, "database_email_hide_suppressed"),
        )
    with db_search_cols[4]:
        db_hide_sent = st.checkbox(
            "Hide sent",
            value=True,
            key=_scope_key(ui_scope, "database_email_hide_sent"),
        )
    st.caption("Database email search is uncapped and returns all matching stored email records.")

    db_source_results = _search_database_email_rows(
        query=db_search_query,
        source=db_search_source,
        limit=0,
        require_email=db_require_email,
        hide_suppressed=db_hide_suppressed,
        countries=tuple(
            sorted(COUNTRIES[name] for name in filters.get('include_countries', []) if name in COUNTRIES)
        ),
    )
    if db_hide_sent:
        db_source_results = [
            row for row in db_source_results
            if _recipient_tracking_id(row) not in sent_invitations
        ]

    db_search_active = (
        author_source_mode in {AUTHOR_SOURCE_DATABASE, AUTHOR_SOURCE_BOTH}
        or bool(db_search_query)
    )
    if author_source_mode == AUTHOR_SOURCE_OPENALEX and not db_search_query:
        st.caption("Tip: type here to show database recipients below, or switch Author Source to Database Emails.")

    panel_state.update({
        'results': db_source_results,
        'total': len(db_source_results),
        'active': db_search_active,
        'query': db_search_query,
    })
    return panel_state


def render_search_section(filters, ui_scope: str):
    """Render the search and results section."""
    if ui_scope == WORKFLOW_AUTHOR:
        invitation_type = st.selectbox(
            "Invitation Purpose",
            options=[INVITATION_TYPE_PUBLICATION, INVITATION_TYPE_EDITORIAL],
            format_func=_invitation_type_label,
            key=_scope_key(ui_scope, "invitation_purpose"),
            help="Choose whether individual and bulk sends invite a publication submission or an editorial role.",
        )
        filters['invitation_type'] = invitation_type
        filters['suppress_after_send'] = True
    else:
        invitation_type = filters.get('invitation_type', INVITATION_TYPE_EDITORIAL)
    is_author_workflow = ui_scope == WORKFLOW_AUTHOR
    author_source_mode = AUTHOR_SOURCE_OPENALEX

    if is_author_workflow:
        source_modes = [AUTHOR_SOURCE_OPENALEX, AUTHOR_SOURCE_DATABASE, AUTHOR_SOURCE_BOTH]
        persisted_source_mode = st.session_state.app_state.get('author_source_mode', AUTHOR_SOURCE_BOTH)
        author_source_mode = st.selectbox(
            "Author Source",
            options=source_modes,
            index=_safe_select_index(source_modes, persisted_source_mode, default=2),
            format_func=_author_source_label,
            key=_scope_key(ui_scope, "author_source_mode_select"),
            help="Choose whether Author Invitation candidates come from OpenAlex, your database emails, or both."
        )
        if author_source_mode != persisted_source_mode:
            st.session_state.app_state['author_source_mode'] = author_source_mode
            save_state()
        filters['author_source_mode'] = author_source_mode
        filters['database_email_panel'] = render_database_email_search_panel(filters, ui_scope, author_source_mode)
        st.divider()

    st.header("Search Authors")
    st.caption(f"Active workflow: {_invitation_type_label(invitation_type)}")
    if is_author_workflow:
        st.caption(f"Source mode: {_author_source_label(author_source_mode)}")

    # Text search filter — visible immediately for finding specific authors
    search_results_key = _scope_key(ui_scope, "search_results_text")
    search_query = st.text_input(
        "Search within results",
        placeholder="Type to filter by name, email, affiliation, country, ORCID, discipline, or specialty...",
        key=search_results_key,
        help="Filters the currently displayed authors by matching text against multiple fields.",
    ).strip().lower()
    if search_query:
        st.caption(f"Searching for: **{search_query}**")

    if is_author_workflow and author_source_mode == AUTHOR_SOURCE_DATABASE:
        search_button_label = "Load Database Authors"
    elif is_author_workflow and author_source_mode == AUTHOR_SOURCE_BOTH:
        search_button_label = "Search OpenAlex + Merge DB"
    else:
        search_button_label = "Search OpenAlex"
    
    # Search button
    col1, col2, col3 = st.columns([2, 1, 1])
    
    with col1:
        search_clicked = st.button(
            search_button_label,
            type="primary",
            use_container_width=True,
            key=_scope_key(ui_scope, "search_openalex")
        )
    
    with col2:
        stop_clicked = st.button("Stop", use_container_width=True, key=_scope_key(ui_scope, "stop_search"))
    
    with col3:
        pass  # Reserved for future use
    
    if stop_clicked:
        st.session_state.stop_fetching = True
    
    # Handle search
    if search_clicked:
        if is_author_workflow and author_source_mode == AUTHOR_SOURCE_DATABASE:
            st.session_state.app_state['search_pagination'] = {'active': False}
            st.session_state.app_state['search_results'] = []
            st.session_state.app_state['processed_orcids'] = set()
            st.session_state[_scope_key(ui_scope, "filtered_authors")] = []
            st.session_state[_scope_key(ui_scope, "results_page")] = 0
            save_state()
            st.success("Loaded database-source author candidates.")
        else:
            run_search(filters, ui_scope)
    
    # Display results (returns filtered list for email fetching)
    display_results(filters, ui_scope)


def run_search(filters, ui_scope: str):
    """Execute the author search with keyword-based topic filtering."""
    
    include_country_codes = [COUNTRIES[c] for c in filters.get('include_countries', []) if c in COUNTRIES]
    exclude_country_codes = [
        COUNTRIES[c] for c in filters['exclude_countries']
        if c in COUNTRIES and c not in set(filters.get('include_countries', []))
    ] or None
    
    client = OpenAlexClient()
    
    # Parse keyword tags
    keyword_tags = filters.get('keyword_tags', '')
    keywords = [k.strip() for k in keyword_tags.split(',') if k.strip()]
    
    topic_ids = None
    
    # Step 1: Search for topics if keywords provided
    if keywords:
        with st.spinner(f"Searching topics for: {', '.join(keywords)}..."):
            topic_ids, topic_details = client.search_topics(keywords, max_per_keyword=3, max_total=25)
        
        if topic_ids:
            msg = f"Found {len(topic_ids)} matching topics"
            if len(topic_ids) >= 25:
                msg += " (limited to 25 for API compatibility)"
            st.success(msg)
            
            # Show some matching topics
            with st.expander("View matching topics", expanded=False):
                for t in topic_details[:15]:
                    st.write(f"- **{t['name']}** ({t['works_count']:,} works) - from '{t['keyword']}'")
        else:
            st.warning("No topics found for the given keywords. Searching without topic filter.")
    
    # Show search info
    search_info = f"H-index: {filters['h_min']}-{filters['h_max']}"
    if filters.get('include_countries'):
        search_info += f" | Including: {', '.join(filters['include_countries'])}"
    if filters['exclude_countries']:
        search_info += f" | Excluding: {', '.join(filters['exclude_countries'])}"
    if keywords:
        search_info += f" | Keywords: {', '.join(keywords[:3])}{'...' if len(keywords) > 3 else ''}"
    st.info(f"Searching: {search_info}")
    
    # Step 2: Get total count with topic filter
    with st.spinner("Counting matching authors..."):
        total_count = client.get_total_count(
            h_index_min=filters['h_min'],
            h_index_max=filters['h_max'],
            include_country_codes=include_country_codes or None,
            exclude_country_codes=exclude_country_codes,
            topic_ids=topic_ids,
            require_orcid=True
        )
    
    if total_count == 0:
        st.warning("No authors found. Try adjusting filters or keywords.")
        return
    
    visible_limit = min(total_count, filters['max_results'])
    st.success(f"Found {total_count:,} authors. Loading the first {filters['jump_size']:,}-result batch out of {visible_limit:,} visible results...")

    search_state = {
        'active': True,
        'filters': {
            'h_index_min': filters['h_min'],
            'h_index_max': filters['h_max'],
            'include_country_codes': include_country_codes or None,
            'exclude_country_codes': exclude_country_codes,
            'topic_ids': topic_ids,
            'require_orcid': True,
        },
        'total_count': total_count,
        'max_results': filters['max_results'],
        'jump_size': filters['jump_size'],
        'total_batches': 0,
        'current_batch_index': 0,
        'current_cursor': '*',
        'next_cursor': None,
        'checkpoints': {'0': '*'},
        'batch_cache': {},
        'current_batch_results': [],
    }

    st.session_state.app_state['search_pagination'] = search_state
    st.session_state.app_state['search_results'] = []
    st.session_state.app_state['processed_orcids'] = set()
    st.session_state[_scope_key(ui_scope, "filtered_authors")] = []
    st.session_state[_scope_key(ui_scope, "results_page")] = 0
    st.session_state.app_state['search_params'] = {
        'keyword_tags': keyword_tags,
        'h_index_min': filters['h_min'],
        'h_index_max': filters['h_max'],
        'include_countries': filters.get('include_countries', []),
        'exclude_countries': filters['exclude_countries'],
        'disciplines': filters.get('disciplines', []),
        'author_source_mode': filters.get('author_source_mode', st.session_state.app_state.get('author_source_mode', AUTHOR_SOURCE_BOTH)),
        'max_results': filters['max_results'],
        'jump_size': filters['jump_size'],
    }

    try:
        with st.spinner("Loading first OpenAlex batch..."):
            loaded = load_search_batch(0, jump_size=filters['jump_size'], reset=True)

        if not loaded:
            st.warning("Unable to load the first OpenAlex batch for this search.")
            return

        current_batch = st.session_state.app_state.get('search_results', [])
        search_state = _get_search_pagination_state()
        st.success(
            f"Loaded results {search_state.get('current_range_start', 0):,}-"
            f"{search_state.get('current_range_end', 0):,} of {visible_limit:,}."
        )

        _sync_current_batch_cache()
        save_state()

    except Exception as e:
        st.error(f"Error: {str(e)}")


def run_email_fetch_filtered(filters, ui_scope: str):
    """Fetch emails ONLY for currently filtered authors.
    
    Uses ORCID API first, then falls back to OpenAI inference for missing emails.
    """
    
    # Get filtered authors from session state
    filtered_authors_key = _scope_key(ui_scope, "filtered_authors")
    filtered_authors = st.session_state.get(filtered_authors_key, st.session_state.get('filtered_authors', []))
    if not filtered_authors:
        st.warning("No filtered authors to process.")
        return
    
    processed = st.session_state.app_state.get('processed_orcids', set())
    if isinstance(processed, list):
        processed = set(processed)
    
    # Get only filtered authors without emails
    to_process = [
        {'orcid_id': a['orcid_id'], 'name': a['name'], 'institution': a.get('institution'), 
         'country': a.get('country'), 'specialty': a.get('specialty')}
        for a in filtered_authors
        if a.get('orcid_id') and a['orcid_id'] not in processed and not a.get('email')
    ]
    
    if not to_process:
        st.info("All authors already processed.")
        return
    
    st.session_state.stop_fetching = False
    use_tavily = filters.get('use_tavily', True)
    use_openai_web = filters.get('use_openai_web', True)
    
    # Progress display
    progress_bar = st.progress(0)
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        processed_metric = st.empty()
    with col2:
        found_metric = st.empty()
    with col3:
        openai_metric = st.empty()
    with col4:
        speed_metric = st.empty()
    
    status_text = st.empty()
    
    # Process in batches
    batch_size = filters['concurrent'] * 5
    total = len(to_process)
    orcid_emails_found = 0
    openai_emails_found = 0
    start_time = time.time()
    
    for batch_start in range(0, total, batch_size):
        if st.session_state.stop_fetching:
            st.warning("Stopped by user")
            break
        
        batch = to_process[batch_start:batch_start + batch_size]
        status_text.text(f"Fetching from ORCID (batch {batch_start // batch_size + 1})...")
        
        # Run async ORCID fetch
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        try:
            batch_results = loop.run_until_complete(
                fetch_emails_async(
                    batch,
                    max_concurrent=filters['concurrent'],
                    delay_between_batches=filters['delay']
                )
            )
            
            # Update results from ORCID
            authors_without_email = []
            for result in batch_results:
                orcid_id = result.get('orcid_id')
                email = result.get('email')
                
                if orcid_id:
                    # Update in search results
                    for author in st.session_state.app_state['search_results']:
                        if author.get('orcid_id') == orcid_id:
                            if email:
                                author['email'] = email
                                author['email_source'] = 'orcid'
                                orcid_emails_found += 1
                            else:
                                # Track authors without email for OpenAI fallback
                                authors_without_email.append(author)
                            break
                    
                    processed.add(orcid_id)
            
            # Web search fallback for authors without ORCID email
            if (use_tavily or use_openai_web) and authors_without_email:
                status_text.text(f"Searching web for emails ({len(authors_without_email)} authors)...")
                
                async def fetch_web_emails():
                    async with AsyncOpenAIEmailClient(
                        max_concurrent=min(5, filters['concurrent']),
                        delay_between_requests=0.5
                    ) as client:
                        return await client.fetch_emails_batch(
                            authors_without_email,
                            use_tavily=use_tavily,
                            use_openai_web=use_openai_web
                        )
                
                web_results = loop.run_until_complete(fetch_web_emails())
                
                # Update with web search results
                for result in web_results:
                    email = result.get('email')
                    if email:
                        orcid_id = result.get('orcid_id')
                        for author in st.session_state.app_state['search_results']:
                            if author.get('orcid_id') == orcid_id:
                                author['email'] = email
                                author['all_emails'] = result.get('all_emails', email)
                                author['email_source'] = result.get('email_source', 'web_search')
                                author['email_confidence'] = result.get('email_confidence', 'unknown')
                                openai_emails_found += 1
                                break
                
        finally:
            loop.close()
        
        st.session_state.app_state['processed_orcids'] = processed
        _sync_current_batch_cache()
        
        # Update metrics
        processed_count = batch_start + len(batch)
        progress_bar.progress(min(processed_count / total, 1.0))
        processed_metric.metric("Processed", f"{processed_count}/{total}")
        found_metric.metric("ORCID Emails", orcid_emails_found)
        openai_metric.metric("Web Found", openai_emails_found)
        
        elapsed = time.time() - start_time
        speed = processed_count / elapsed if elapsed > 0 else 0
        speed_metric.metric("Speed", f"{speed:.1f}/sec")
        
        # Auto-save
        save_state()
    
    total_found = orcid_emails_found + openai_emails_found
    st.session_state.stop_fetching = False
    
    # Save result message and rerun to ensure display refreshes with updated emails
    st.session_state.last_fetch_result = (
        f"Found {total_found} emails from {total} authors "
        f"({orcid_emails_found} ORCID + {openai_emails_found} Web)"
    )
    save_state()
    st.rerun()


def _enqueue_bulk_send(payload: dict) -> None:
    """Create a durable background bulk send job."""
    if not db_storage.available:
        st.session_state.last_bulk_enqueue_result = "Database is required for background bulk sending."
        st.rerun()
    publisher_id = (payload.get('publisher_id') or '').strip()
    if not EMAIL_AVAILABLE or publisher_id not in email_sender.credentials:
        st.session_state.last_bulk_enqueue_result = (
            f"Bulk send cancelled: invalid selected publisher '{publisher_id or 'missing'}'."
        )
        st.rerun()

    invitation_type = payload.get('invitation_type', INVITATION_TYPE_EDITORIAL)
    selected_template_id = payload.get('selected_bulk_template', TEMPLATE_BOARD_MEMBER)
    template_strategy = payload.get('bulk_template_strategy', 'Use selected template')
    tracking_journal_name = payload.get('tracking_journal_name', '')
    retracted_names = db_storage.get_retracted_names()
    payload_batch = payload.get('batch') or []
    active_bulk_keys = db_storage.get_active_bulk_recipient_keys(payload_batch)
    sent_invitations = get_sent_invitations(invitation_type, tracking_journal_name)
    batch = prepare_bulk_recipients(
        payload_batch,
        is_already_sent=lambda recipient_id: (
            recipient_id in sent_invitations
            or recipient_id in active_bulk_keys["identities"]
        ),
        is_suppressed=lambda email, orcid_id: (
            db_storage.is_recipient_suppressed(email, orcid_id=orcid_id)
            or (email or '').strip().lower() in active_bulk_keys["emails"]
        ),
        retracted_names=retracted_names,
    )
    batch = cap_bulk_recipients(batch)

    if not batch:
        st.session_state.last_bulk_enqueue_result = "No eligible authors remained for this bulk send."
        st.rerun()

    job_id = db_storage.create_bulk_email_job(
        recipients=batch,
        invitation_type=invitation_type,
        publisher_id=publisher_id,
        journal_name=tracking_journal_name,
        template_id=selected_template_id,
        template_strategy=template_strategy,
        scopus_indexed=bool(payload.get('bulk_scopus_indexed', False)),
        attach_pdf=bool(payload.get('bulk_attach_pdf', True)),
        include_publications=bool(payload.get('bulk_include_cached_publications', False)),
        journal_config={
            **(payload.get('journal_config', {}) or {}),
            "suppress_after_send": bool(payload.get('suppress_after_send')),
        },
    )
    if job_id:
        st.session_state.last_bulk_enqueue_result = f"Queued background bulk email job #{job_id} with {len(batch)} recipients."
    else:
        st.session_state.last_bulk_enqueue_result = "Could not queue the background bulk email job."
    st.rerun()


def _render_bulk_job_status(ui_scope: str) -> None:
    """Show progress for recent background bulk email jobs."""
    if not db_storage.available:
        st.info("Background bulk sending requires PostgreSQL. Configure DATABASE_URL to enable it.")
        return

    if st.session_state.get('last_bulk_enqueue_result'):
        message = st.session_state.pop('last_bulk_enqueue_result')
        if message.startswith("Queued"):
            st.success(message)
        else:
            st.warning(message)

    jobs = db_storage.get_recent_bulk_email_jobs(limit=5)
    if not jobs:
        st.caption("No background bulk send jobs yet.")
        return

    st.markdown("**Background Send Progress**")
    for job in jobs:
        total = int(job.get('total_count') or 0)
        sent = int(job.get('sent_count') or 0)
        failed = int(job.get('failed_count') or 0)
        skipped = int(job.get('skipped_count') or 0)
        done = sent + failed + skipped
        progress = (done / total) if total else 0
        status = job.get('status') or ''
        label = (
            f"Job #{job.get('id')} | {status.title()} | "
            f"{_publisher_display_label(job.get('publisher_id', ''))} | "
            f"{done}/{total} processed ({sent} sent, {failed} failed, {skipped} skipped)"
        )
        st.progress(min(progress, 1.0), text=label)
        detail_cols = st.columns([2, 2, 1])
        with detail_cols[0]:
            if job.get('last_recipient'):
                st.caption(f"Last recipient: {job.get('last_recipient')}")
        with detail_cols[1]:
            if job.get('last_error'):
                st.caption(f"Last error: {job.get('last_error')}")
            elif job.get('last_provider_response'):
                st.caption(f"Provider: {job.get('last_provider_response')}")
        with detail_cols[2]:
            if status in {BULK_JOB_STATUS_QUEUED, BULK_JOB_STATUS_RUNNING}:
                if st.button("Cancel", key=_scope_key(ui_scope, f"cancel_bulk_job_{job.get('id')}")):
                    db_storage.cancel_bulk_email_job(int(job.get('id')))
                    st.rerun()

    if st.button("Refresh Bulk Progress", key=_scope_key(ui_scope, "refresh_bulk_jobs")):
        st.rerun()


def display_results(filters, ui_scope: str):
    """Display search results with selection and filtering."""
    
    openalex_results = st.session_state.app_state.get('search_results', [])
    search_state = _get_search_pagination_state()
    pending_dialog_author_key = _scope_key(ui_scope, "pending_email_dialog_author")
    pending_dialog_filters_key = _scope_key(ui_scope, "pending_email_dialog_filters")
    pending_bulk_dialog_key = _scope_key(ui_scope, "pending_bulk_send_dialog")
    confirmed_bulk_send_key = _scope_key(ui_scope, "confirmed_bulk_send_payload")
    journal_config = st.session_state.app_state.get('journal_config', {})
    invitation_type = filters.get('invitation_type', INVITATION_TYPE_EDITORIAL)
    tracking_journal_name = _tracking_journal_name(invitation_type, journal_config)
    sent_invitations = get_sent_invitations(invitation_type, tracking_journal_name)

    confirmed_bulk_payload = st.session_state.pop(confirmed_bulk_send_key, None)
    if isinstance(confirmed_bulk_payload, dict):
        _enqueue_bulk_send(confirmed_bulk_payload)

    is_author_workflow = ui_scope == WORKFLOW_AUTHOR
    author_source_mode = filters.get(
        'author_source_mode',
        st.session_state.app_state.get('author_source_mode', AUTHOR_SOURCE_BOTH),
    )

    db_panel = filters.get('database_email_panel') if isinstance(filters.get('database_email_panel'), dict) else {}
    db_source_results = db_panel.get('results') or []
    db_source_total = int(db_panel.get('total') or len(db_source_results))
    db_search_active = bool(db_panel.get('active'))
    db_search_query = db_panel.get('query') or ''

    if is_author_workflow and author_source_mode == AUTHOR_SOURCE_DATABASE:
        results = db_source_results
    elif is_author_workflow and (author_source_mode == AUTHOR_SOURCE_BOTH or db_search_active):
        results = _merge_author_source_results(openalex_results, db_source_results)
    else:
        results = openalex_results
    
    empty_results_message = ""
    if not results:
        if is_author_workflow and author_source_mode == AUTHOR_SOURCE_DATABASE:
            empty_results_message = "No database email records match the current search and filters."
        elif db_search_active:
            empty_results_message = "No database email records match the current search and filters."
        else:
            empty_results_message = "No results yet. Use the search button above."

    if is_author_workflow and (author_source_mode in {AUTHOR_SOURCE_DATABASE, AUTHOR_SOURCE_BOTH} or db_search_active):
        db_count = len(db_source_results)
        openalex_count = len(openalex_results)
        st.caption(
            f"Source counts: OpenAlex={openalex_count:,}, "
            f"DatabaseLoaded={db_count:,}, Displayed={len(results):,}."
        )

        missing_domain_rows = [
            row for row in results
            if row.get('source_origin') in {AUTHOR_SOURCE_DATABASE, AUTHOR_SOURCE_BOTH}
            and row.get('profile_key')
            and not row.get('scientific_domain')
        ]
        if missing_domain_rows:
            enrich_count = min(len(missing_domain_rows), 50)
            enrich_clicked = st.button(
                f"Enrich Missing Domains from OpenAlex ({enrich_count} visible)",
                key=_scope_key(ui_scope, "enrich_missing_domains"),
                use_container_width=False,
            )
            st.caption("Uses strict ORCID matching and writes scientific_domain back to author_profiles.")
            if enrich_clicked:
                with st.spinner("Enriching visible database-source authors from OpenAlex..."):
                    updated_count, attempted_count = _enrich_db_source_domains_from_openalex(results, max_rows=50)
                if updated_count > 0:
                    st.success(f"Enriched {updated_count} of {attempted_count} attempted rows.")
                else:
                    st.info("No visible rows were enriched. Ensure ORCID values exist and are valid in OpenAlex.")
                st.rerun()
    
    # Show email fetch result message from previous run
    if st.session_state.get('last_fetch_result'):
        st.success(st.session_state.pop('last_fetch_result'))

    total_visible_results = _get_result_limit(search_state) if search_state.get('active') else len(results)

    show_openalex_batch_controls = search_state.get('active') and (
        not is_author_workflow or author_source_mode == AUTHOR_SOURCE_OPENALEX
    )

    if show_openalex_batch_controls:
        current_jump_size = int(search_state.get('jump_size', 250) or 250)
        selected_jump_size = st.selectbox(
            "Batch Jump Size",
            options=[250, 500, 1000],
            index=[250, 500, 1000].index(current_jump_size),
            key=_scope_key(ui_scope, "batch_jump_size_control"),
            help="Reload OpenAlex result batches using the selected jump size"
        )
        if selected_jump_size != current_jump_size:
            load_search_batch(0, jump_size=selected_jump_size, reset=True)
            st.rerun()

        total_batches = max(int(search_state.get('total_batches', 0) or 0), 1)
        current_batch_index = int(search_state.get('current_batch_index', 0) or 0)
        current_start = int(search_state.get('current_range_start', 0) or 0)
        current_end = int(search_state.get('current_range_end', 0) or 0)

        st.caption(
            f"Visible OpenAlex range: {current_start:,}-{current_end:,} of {total_visible_results:,} | "
            f"Batch {current_batch_index + 1} of {total_batches}"
        )

        batch_prev_col, batch_num_col, batch_go_col, batch_next_col = st.columns([1, 1.2, 0.8, 1])
        with batch_prev_col:
            if st.button(
                "← Previous Batch",
                disabled=current_batch_index == 0,
                use_container_width=True,
                key=_scope_key(ui_scope, "prev_batch")
            ):
                load_search_batch(current_batch_index - 1)
                st.rerun()
        with batch_num_col:
            target_batch = st.number_input(
                "Jump to Batch",
                min_value=1,
                max_value=total_batches,
                value=current_batch_index + 1,
                step=1,
                key=_scope_key(ui_scope, "target_batch_number")
            )
        with batch_go_col:
            if st.button("Go", use_container_width=True, key=_scope_key(ui_scope, "go_batch")):
                load_search_batch(int(target_batch) - 1)
                st.rerun()
        with batch_next_col:
            if st.button(
                "Next Batch →",
                disabled=current_batch_index >= total_batches - 1,
                use_container_width=True,
                key=_scope_key(ui_scope, "next_batch")
            ):
                load_search_batch(current_batch_index + 1)
                st.rerun()

        st.caption("OpenAlex deep paging is cursor-based, so batch jumps reuse saved checkpoints rather than direct page numbers.")
    
    filtered = results.copy()

    # Apply text search filter from top-of-page search input
    active_search_query = st.session_state.get(_scope_key(ui_scope, "search_results_text"), '').strip().lower()
    if active_search_query:
        filtered = [
            r for r in filtered
            if any(
                active_search_query in str(value).lower()
                for value in [
                    r.get('name', ''),
                    r.get('email', ''),
                    r.get('all_emails', ''),
                    r.get('institution', ''),
                    r.get('country', ''),
                    r.get('orcid_id', ''),
                    r.get('author_id', ''),
                    r.get('discipline', ''),
                    r.get('specialty', ''),
                    r.get('research_areas', ''),
                    r.get('subfield', ''),
                ]
                if value
            )
        ]

    # Reuse known database emails so invitations can be sent without refetching.
    if _hydrate_result_emails_from_db(filtered):
        if show_openalex_batch_controls:
            _sync_current_batch_cache()
        save_state()
    
    _render_bulk_job_status(ui_scope)
    
    # Get retracted author names from DB (lowercased set for fast matching)
    retracted_names = db_storage.get_retracted_names() if db_storage.available else set()
    
    # Tag each author with retraction and sent status
    for r in filtered:
        r['is_retracted'] = r.get('name', '').lower() in retracted_names

    # Apply sidebar discipline filter to the displayed results.
    sidebar_disciplines = [d for d in filters.get('disciplines', []) if d]
    if sidebar_disciplines:
        filtered = [r for r in filtered if r.get('discipline') in sidebar_disciplines]
    
    # Collect unique disciplines, specialties, and author domains from results.
    all_disciplines = set()
    all_specialties = set()
    all_author_domains = set()
    for r in filtered:
        if r.get('discipline'):
            all_disciplines.add(r['discipline'])
        if r.get('all_topics'):
            all_specialties.update(r['all_topics'])
        elif r.get('specialty'):
            all_specialties.add(r['specialty'])
        all_author_domains.update(_extract_author_domains(r))
    
    # Filter options - Row 1: Discipline and Specialty filters
    col_f1, col_f2 = st.columns(2)
    
    with col_f1:
        # Discipline filter (multiselect)
        selected_disciplines = st.multiselect(
            "Filter by Discipline",
            options=sorted(all_disciplines),
            default=[],
            key=_scope_key(ui_scope, "discipline_filter"),
            help="Filter by broad discipline category"
        )
    
    with col_f2:
        previous_specialties_key = _scope_key(ui_scope, "specialty_filter_multi_previous")
        specialty_filter_key = _scope_key(ui_scope, "specialty_filter_multi")
        specialty_options = sorted(all_specialties)
        specialty_search_key = _scope_key(ui_scope, "specialty_filter_search")
        specialty_search_text = st.text_input(
            "Search specialties",
            key=specialty_search_key,
            placeholder="e.g., IoT",
            help="Filter the specialty list, then select all matching options if needed."
        ).strip()
        specialty_search_lower = specialty_search_text.lower()
        visible_specialty_options = [
            option for option in specialty_options
            if not specialty_search_lower or specialty_search_lower in option.lower()
        ]
        current_specialty_values = st.session_state.get(specialty_filter_key, [])
        if current_specialty_values:
            valid_specialty_values = [
                value for value in current_specialty_values
                if value in specialty_options
            ]
            if valid_specialty_values != current_specialty_values:
                st.session_state[specialty_filter_key] = valid_specialty_values
                current_specialty_values = valid_specialty_values
        selected_not_visible = [
            value for value in current_specialty_values
            if value not in visible_specialty_options
        ]
        specialty_widget_options = visible_specialty_options + selected_not_visible

        specialty_action_cols = st.columns(2)
        with specialty_action_cols[0]:
            if st.button(
                "Select filtered specialties",
                key=_scope_key(ui_scope, "specialty_filter_select_all"),
                use_container_width=True,
                disabled=not visible_specialty_options,
            ):
                merged_specialties = list(dict.fromkeys(current_specialty_values + visible_specialty_options))
                st.session_state[specialty_filter_key] = merged_specialties
                st.session_state[_scope_key(ui_scope, "results_page")] = 0
                st.rerun()
        with specialty_action_cols[1]:
            if st.button(
                "Clear specialties",
                key=_scope_key(ui_scope, "specialty_filter_clear"),
                use_container_width=True,
                disabled=not current_specialty_values,
            ):
                st.session_state[specialty_filter_key] = []
                st.session_state[_scope_key(ui_scope, "results_page")] = 0
                st.rerun()

        selected_specialties = st.multiselect(
            "Filter by Specialty",
            options=specialty_widget_options,
            default=[],
            key=specialty_filter_key,
            help="Type to search, then select one or more research topics."
        )
        if specialty_search_text:
            st.caption(
                f"Showing {len(visible_specialty_options):,} of {len(specialty_options):,} specialties "
                f"matching '{specialty_search_text}'."
            )
        selected_specialty_signature = tuple(sorted(selected_specialties))
        if st.session_state.get(previous_specialties_key) != selected_specialty_signature:
            st.session_state[_scope_key(ui_scope, "results_page")] = 0
            st.session_state[previous_specialties_key] = selected_specialty_signature
    
    # Apply discipline filter
    if selected_disciplines:
        filtered = [r for r in filtered if r.get('discipline') in selected_disciplines]
    
    # Apply specialty filter
    matched_before_other_filters = len(filtered)
    if selected_specialties:
        filtered = [r for r in filtered if author_matches_any_specialty(r, selected_specialties)]
        matched_before_other_filters = len(filtered)
    st.caption(
        f"Selected specialties: {len(selected_specialties):,} | "
        f"Matched before other filters: {matched_before_other_filters:,}"
    )
    
    # Country exclusion post-filter (supplements API-level exclusion)
    all_countries = sorted({r.get('country') for r in results if r.get('country')})
    if all_countries:
        # Reverse-map codes to names for display
        code_to_name = {v: k for k, v in COUNTRIES.items()}
        country_options = [f"{code_to_name.get(c, c)} ({c})" for c in all_countries]
        excluded_display = st.multiselect(
            "Exclude Countries (post-filter)",
            options=country_options,
            default=[],
            key=_scope_key(ui_scope, "exclude_countries_postfilter"),
            help="Exclude authors from these countries in the results below"
        )
        if excluded_display:
            excluded_codes = {opt.split("(")[-1].rstrip(")") for opt in excluded_display}
            filtered = [r for r in filtered if r.get('country') not in excluded_codes]
    else:
        st.multiselect(
            "Exclude Countries (post-filter)",
            options=[],
            default=[],
            key=_scope_key(ui_scope, "exclude_countries_postfilter"),
            help="Exclude authors from these countries in the results below",
            disabled=True,
        )
        st.caption("Load or search authors to populate country filter options.")

    # Scientific-domain targeting filters (for example Computer Science, Biology).
    selected_domain_labels: list[str] = []
    excluded_domain_labels: list[str] = []
    if all_author_domains:
        col_domain1, col_domain2 = st.columns(2)
        with col_domain1:
            selected_domain_labels = st.multiselect(
                "Include Author Domains",
                options=sorted(all_author_domains),
                default=[],
                key=_scope_key(ui_scope, "include_author_domains_filter"),
                help="Show only authors whose scientific domains match the selected values."
            )
        with col_domain2:
            excluded_domain_labels = st.multiselect(
                "Exclude Author Domains",
                options=sorted(all_author_domains),
                default=[],
                key=_scope_key(ui_scope, "exclude_author_domains_filter"),
                help="Hide authors whose scientific domains match the selected values."
            )
    else:
        col_domain1, col_domain2 = st.columns(2)
        with col_domain1:
            st.multiselect(
                "Include Author Domains",
                options=[],
                default=[],
                key=_scope_key(ui_scope, "include_author_domains_filter"),
                help="Show only authors whose scientific domains match the selected values.",
                disabled=True,
            )
        with col_domain2:
            st.multiselect(
                "Exclude Author Domains",
                options=[],
                default=[],
                key=_scope_key(ui_scope, "exclude_author_domains_filter"),
                help="Hide authors whose scientific domains match the selected values.",
                disabled=True,
            )
        st.caption("Load or search authors to populate author domain filter options.")

    selected_domain_lookup = {label.lower() for label in selected_domain_labels}
    if selected_domain_lookup:
        filtered = [
            r for r in filtered
            if selected_domain_lookup.intersection({value.lower() for value in _extract_author_domains(r)})
        ]

    excluded_domain_lookup = {label.lower() for label in excluded_domain_labels}
    if excluded_domain_lookup:
        filtered = [
            r for r in filtered
            if not excluded_domain_lookup.intersection({value.lower() for value in _extract_author_domains(r)})
        ]
    
    # Filter options - Row 2: Checkboxes
    col_filter1, col_filter2, col_filter3 = st.columns(3)
    with col_filter1:
        show_only_with_email = st.checkbox(
            "Show only authors with email",
            value=False,
            key=_scope_key(ui_scope, "filter_email")
        )
    with col_filter2:
        show_only_not_sent = st.checkbox(
            f"Hide already sent ({_invitation_type_label(invitation_type)})",
            value=True,
            key=_scope_key(ui_scope, "filter_not_sent")
        )
    with col_filter3:
        hide_retracted = st.checkbox(
            "Hide retracted authors",
            value=True,
            key=_scope_key(ui_scope, "filter_retracted")
        )

    retracted_in_scope = sum(1 for r in filtered if r.get('is_retracted'))
    
    # Apply email filter
    if show_only_with_email:
        filtered = [r for r in filtered if r.get('email')]
    
    # Apply sent filter
    if show_only_not_sent:
        filtered = [r for r in filtered if _recipient_tracking_id(r) not in sent_invitations]
    
    # Apply retraction filter
    if hide_retracted:
        filtered = [r for r in filtered if not r.get('is_retracted')]
        if retracted_in_scope:
            st.caption(f"Hidden retracted authors: {retracted_in_scope:,}")

    before_dedupe_count = len(filtered)
    filtered = dedupe_authors(filtered)
    removed_duplicates = before_dedupe_count - len(filtered)
    if removed_duplicates:
        st.caption(f"Removed duplicate authors from filtered results: {removed_duplicates:,}")
    
    # Store filtered list in session state for email fetching
    st.session_state[_scope_key(ui_scope, "filtered_authors")] = filtered
    st.session_state.filtered_authors = filtered
    
    # Count authors without email in filtered list
    without_email = sum(1 for r in filtered if not r.get('email'))
    
    # Check how many have been processed already
    processed = st.session_state.app_state.get('processed_orcids', set())
    if isinstance(processed, list):
        processed = set(processed)
    already_processed = sum(1 for r in filtered if r.get('orcid_id') in processed)
    
    # Fetch Emails button - only for filtered authors
    st.divider()
    col_btn1, col_btn2 = st.columns([2, 1])
    with col_btn1:
        fetch_btn_label = f"Fetch Emails for {without_email} Filtered Authors" if without_email > 0 else "All Filtered Authors Have Emails"
        fetch_emails_clicked = st.button(
            fetch_btn_label,
            type="primary" if without_email > 0 else "secondary",
            use_container_width=True,
            disabled=without_email == 0,
            key=_scope_key(ui_scope, "fetch_emails")
        )
    with col_btn2:
        stop_clicked = st.button(
            "Stop Fetching",
            use_container_width=True,
            key=_scope_key(ui_scope, "stop_fetching")
        )
    
    # Show tip about email rates if many processed but few found
    if already_processed > 0 and without_email > 0:
        with_email_count = sum(1 for r in filtered if r.get('email'))
        rate = (with_email_count / already_processed * 100) if already_processed > 0 else 0
        if rate < 20:
            st.caption(
                f"ℹ️ ORCID public emails: ~10-15% of academics share their email publicly. "
                f"Enable **Tavily search** or **OpenAI web search** in sidebar for more results."
            )
    
    if stop_clicked:
        st.session_state.stop_fetching = True
    
    if fetch_emails_clicked:
        run_email_fetch_filtered(filters, ui_scope)
    
    # Summary metrics
    col1, col2, col3, col4, col5 = st.columns(5)
    with col1:
        st.metric("Total", len(filtered))
    with col2:
        with_email = sum(1 for r in filtered if r.get('email'))
        st.metric("With Email", with_email)
    with col3:
        st.metric("Without Email", without_email)
    with col4:
        retracted_count = sum(1 for r in filtered if r.get('is_retracted'))
        st.metric("Retracted", retracted_count)
    with col5:
        sent_count = sum(1 for r in filtered if _recipient_tracking_id(r) in sent_invitations)
        st.metric(f"Sent ({_invitation_type_label(invitation_type)})", sent_count)
    
    st.divider()

    # Results table with Send buttons
    st.subheader(f"Authors ({len(filtered)})")

    if not filtered:
        st.info(empty_results_message or "No authors match the current filters.")
        return

    invitation_counts: dict[str, int] = {}
    suppressed_keys = {"emails": set(), "orcids": set(), "profile_keys": set()}
    if db_storage.available:
        try:
            invitation_counts = db_storage.get_invitation_counts([
                row.get('orcid_id', '')
                for row in filtered
                if row.get('orcid_id')
            ])
        except Exception:
            invitation_counts = {}
        suppressed_keys = db_storage.get_suppressed_recipient_keys(filtered)

    def result_is_suppressed(row: dict) -> bool:
        return (
            (row.get('email') or '').strip().lower() in suppressed_keys["emails"]
            or (row.get('orcid_id') or '').strip().lower().replace('https://orcid.org/', '')
                in suppressed_keys["orcids"]
            or (row.get('profile_key') or '').strip() in suppressed_keys["profile_keys"]
        )
    
    # Prepare dataframe for export
    df_data = []
    for r in filtered:
        orcid_id = r.get('orcid_id', '')
        invited_count = int(invitation_counts.get(orcid_id, 0))
        source_origin = r.get('source_origin', AUTHOR_SOURCE_OPENALEX)
        status = ''
        if result_is_suppressed(r):
            status = 'SUPPRESSED'
        elif r.get('is_retracted'):
            status = '🚫 RETRACTED'
        elif _recipient_tracking_id(r) in sent_invitations:
            status = f"✅ SENT {_invitation_type_label(invitation_type).upper()}"
        df_data.append({
            'Name': r.get('name', ''),
            'H-Index': r.get('h_index', ''),
            'Specialty': r.get('specialty', '') or '',
            'Discipline': r.get('discipline', ''),
            'Email': r.get('email', '') or '',
            'All_Emails': r.get('all_emails', '') or r.get('email', '') or '',
            'Source': _author_source_label(source_origin),
            'Institution': r.get('institution', ''),
            'Country': r.get('country', ''),
            'Invited_Count': invited_count,
            'Status': status,
            'orcid_id': orcid_id,
            'all_topics': r.get('all_topics', [])
        })
    
    df = pd.DataFrame(df_data)
    
    # Custom CSS for row highlighting
    st.markdown("""
    <style>
    .notified-row {
        background-color: #d4edda !important;
        border-left: 4px solid #28a745;
        padding: 5px;
        margin: 2px 0;
        border-radius: 4px;
    }
    .retracted-row {
        background-color: #f8d7da !important;
        border-left: 4px solid #dc3545;
        padding: 5px;
        margin: 2px 0;
        border-radius: 4px;
    }
    .pending-row {
        background-color: #ffffff;
        padding: 5px;
        margin: 2px 0;
        border-radius: 4px;
    }
    .no-email-row {
        background-color: #f8f9fa;
        padding: 5px;
        margin: 2px 0;
        border-radius: 4px;
        opacity: 0.7;
    }
    </style>
    """, unsafe_allow_html=True)
    
    # Table header
    header_cols = st.columns([2.2, 0.6, 1.8, 1.3, 0.7, 2, 1])
    with header_cols[0]:
        st.markdown("**Name**")
    with header_cols[1]:
        st.markdown("**H-Index**")
    with header_cols[2]:
        st.markdown("**Specialty**")
    with header_cols[3]:
        st.markdown("**Discipline**")
    with header_cols[4]:
        st.markdown("**Country**")
    with header_cols[5]:
        st.markdown("**Email**")
    with header_cols[6]:
        st.markdown("**Send / Re-send**")
    
    st.divider()
    
    # Display rows with Send buttons (paginated)
    page_size = 25
    total_pages = (len(filtered) + page_size - 1) // page_size
    results_page_key = _scope_key(ui_scope, "results_page")
    if results_page_key not in st.session_state:
        st.session_state[results_page_key] = 0
    current_results_page = st.session_state[results_page_key]
    
    # Pagination controls
    if total_pages > 1:
        col_prev, col_page, col_next = st.columns([1, 2, 1])
        with col_prev:
            if st.button(
                "← Previous",
                disabled=current_results_page == 0,
                key=_scope_key(ui_scope, "prev_results_page")
            ):
                st.session_state[results_page_key] = max(0, current_results_page - 1)
                st.rerun()
        with col_page:
            st.markdown(f"<center>Page {current_results_page + 1} of {total_pages}</center>", unsafe_allow_html=True)
        with col_next:
            if st.button(
                "Next →",
                disabled=current_results_page >= total_pages - 1,
                key=_scope_key(ui_scope, "next_results_page")
            ):
                st.session_state[results_page_key] = min(total_pages - 1, current_results_page + 1)
                st.rerun()
    
    # Get current page of results
    start_idx = current_results_page * page_size
    end_idx = min(start_idx + page_size, len(filtered))
    page_results = filtered[start_idx:end_idx]
    
    # --- Bulk send controls ---
    filtered_with_email = [a for a in filtered if a.get('email')]
    active_bulk_keys = (
        db_storage.get_active_bulk_recipient_keys(filtered)
        if db_storage.available else {"identities": set(), "emails": set()}
    )
    eligible_bulk_authors = prepare_bulk_recipients(
        filtered,
        is_already_sent=lambda recipient_id: (
            recipient_id in sent_invitations
            or recipient_id in active_bulk_keys["identities"]
        ),
        is_suppressed=lambda email, orcid_id: (
            (email or '').strip().lower() in suppressed_keys["emails"]
            or (orcid_id or '').strip().lower().replace('https://orcid.org/', '')
                in suppressed_keys["orcids"]
            or (email or '').strip().lower() in active_bulk_keys["emails"]
        ),
        retracted_names={a.get('name', '').lower() for a in filtered if a.get('is_retracted')},
    )

    st.markdown(f"**Bulk Send Settings ({_invitation_type_label(invitation_type)})**")
    bulk_template_names = get_template_names(invitation_type)
    bulk_col1, bulk_col2, bulk_col3 = st.columns([1.4, 1.4, 1.2])
    with bulk_col1:
        selected_bulk_template = st.selectbox(
            "Bulk Template",
            options=list(bulk_template_names.keys()),
            format_func=lambda x: bulk_template_names[x],
            key=_scope_key(ui_scope, f"bulk_template_select_{invitation_type}")
        )
    with bulk_col2:
        if invitation_type == INVITATION_TYPE_PUBLICATION:
            bulk_template_strategy = "Use selected template"
            bulk_scopus_indexed = False
            st.caption("Publication template selected for every recipient in this batch.")
        else:
            bulk_template_strategy = "Use selected template"
            bulk_scopus_indexed = st.checkbox(
                "Journal is Scopus indexed",
                value=False,
                key=_scope_key(ui_scope, "bulk_scopus_indexed")
            )
    with bulk_col3:
        bulk_attach_pdf = st.checkbox(
            "Attach PDF",
            value=True,
            key=_scope_key(ui_scope, f"bulk_attach_pdf_{invitation_type}")
        )
        if invitation_type == INVITATION_TYPE_PUBLICATION:
            bulk_include_cached_publications = st.checkbox(
                "Use cached publications",
                value=True,
                key=_scope_key(ui_scope, "bulk_include_cached_publications"),
                help="Bulk sends use already-loaded OpenAlex publications only to avoid slow batch sends."
            )
        else:
            bulk_include_cached_publications = False
    
    col_count, col_note, col_bulk = st.columns([1.2, 1.2, 1.6])
    with col_count:
        eligible_batch_limit = min(1000, len(eligible_bulk_authors))
        default_bulk_count = eligible_batch_limit
        batch_size = st.number_input(
            "Recipients to queue",
            min_value=0,
            max_value=eligible_batch_limit,
            value=default_bulk_count,
            step=25,
            key=_scope_key(ui_scope, f"bulk_recipient_count_{invitation_type}"),
            help="Uses all current filters, not just the visible page, with a maximum of 1,000 recipients per batch."
        )
    with col_note:
        st.caption(
            f"Eligible: {len(eligible_bulk_authors):,} of {len(filtered_with_email):,} filtered authors with email. "
            "Already invited, actively queued, suppressed, and retracted authors are skipped."
        )

    with col_bulk:
        selected_bulk_publisher_id = (filters.get('publisher') or '').strip()
        selected_bulk_publisher_valid = EMAIL_AVAILABLE and selected_bulk_publisher_id in email_sender.credentials
        bulk_send_clicked = st.button(
            f"Queue Background Bulk ({int(batch_size)} emails)",
            type="primary" if batch_size > 0 and selected_bulk_publisher_valid and db_storage.available else "secondary",
            disabled=not (batch_size > 0 and selected_bulk_publisher_valid and db_storage.available),
            use_container_width=True,
            key=_scope_key(ui_scope, f"bulk_send_{current_results_page}")
        )
        if selected_bulk_publisher_valid:
            st.caption(f"Sender: {_publisher_display_label(selected_bulk_publisher_id)}")
        else:
            st.warning("Select a valid publisher before bulk sending.")
    if not db_storage.available:
        st.warning("Background bulk sending is disabled because PostgreSQL is not connected.")

    if bulk_send_clicked and batch_size > 0:
        st.session_state[pending_bulk_dialog_key] = {
            'batch': [dict(author) for author in eligible_bulk_authors[:int(batch_size)]],
            'invitation_type': invitation_type,
            'tracking_journal_name': tracking_journal_name,
            'publisher_id': selected_bulk_publisher_id,
            'selected_bulk_template': selected_bulk_template,
            'bulk_template_strategy': bulk_template_strategy,
            'bulk_scopus_indexed': bulk_scopus_indexed,
            'bulk_attach_pdf': bulk_attach_pdf,
            'bulk_include_cached_publications': bulk_include_cached_publications,
            'suppress_after_send': bool(filters.get('suppress_after_send')),
            'journal_config': dict(journal_config),
            'dialog_key': f"{ui_scope}_{current_results_page}_{int(time.time())}",
        }
        st.rerun()

    pending_bulk_payload = st.session_state.pop(pending_bulk_dialog_key, None)
    if isinstance(pending_bulk_payload, dict):
        bulk_send_preview_dialog(pending_bulk_payload, confirmation_key=confirmed_bulk_send_key)

    pending_author = st.session_state.pop(pending_dialog_author_key, None)
    pending_filters = st.session_state.pop(pending_dialog_filters_key, None)
    if isinstance(pending_author, dict):
        dialog_filters = dict(filters)
        if isinstance(pending_filters, dict):
            dialog_filters.update(pending_filters)
        email_dialog(pending_author, dialog_filters)
    
    # --- Display rows ---
    for idx, author in enumerate(page_results):
        orcid_id = author.get('orcid_id', '')
        invited_count = int(invitation_counts.get(orcid_id, 0))
        tracking_id = _recipient_tracking_id(author)
        is_notified = tracking_id in sent_invitations
        is_retracted = author.get('is_retracted', False)
        has_email = bool(author.get('email'))
        is_suppressed = is_recipient_suppressed(
            author.get('email', ''),
            orcid_id,
            author.get('profile_key', ''),
        )
        
        cols = st.columns([2.2, 0.6, 1.8, 1.3, 0.7, 2, 1])
        
        with cols[0]:
            name_display = author.get('name', '')
            source_origin = author.get('source_origin', AUTHOR_SOURCE_OPENALEX)
            if is_retracted:
                st.markdown(f"🚫 ~~{name_display}~~ :red[RETRACTED]")
            elif is_notified:
                st.markdown(f"✅ **{name_display}** :green[SENT {_invitation_type_label(invitation_type)}]")
            else:
                st.write(name_display)
            if source_origin != AUTHOR_SOURCE_OPENALEX:
                source_label = _author_source_label(source_origin)
                if author.get('source_table'):
                    source_label = f"{source_label} / {_database_email_source_label(author.get('source_table'))}"
                st.caption(f"Source: {source_label}")
            if author.get('institution'):
                st.caption(author.get('institution'))
            if invited_count > 0:
                st.caption(f"Invited: {invited_count}")
            if is_suppressed:
                st.caption("Suppressed")
        
        with cols[1]:
            st.write(author.get('h_index', ''))
        
        with cols[2]:
            specialty = author.get('specialty', '') or ''
            if len(specialty) > 30:
                specialty = specialty[:27] + "..."
            st.write(specialty)
        
        with cols[3]:
            st.write(author.get('discipline', ''))
        
        with cols[4]:
            country_code = author.get('country', '')
            st.write(country_code or '—')
        
        with cols[5]:
            email = author.get('email', '')
            all_emails = author.get('all_emails', '')
            if email:
                display_email = all_emails if all_emails else email
                if len(display_email) > 30:
                    email_display = display_email[:27] + "..."
                else:
                    email_display = display_email
                source = author.get('email_source', 'orcid')
                if source == 'web_search':
                    st.write(f"🔍 {email_display}")
                else:
                    st.write(email_display)
            else:
                st.caption("No email")
        
        with cols[6]:
            if is_suppressed:
                st.button(
                    "Suppressed",
                    disabled=True,
                    key=_scope_key(ui_scope, f"suppressed_{orcid_id}_{start_idx + idx}"),
                    use_container_width=True
                )
            elif is_retracted:
                st.button(
                    "🚫",
                    disabled=True,
                    key=_scope_key(ui_scope, f"retracted_{orcid_id}_{start_idx + idx}"),
                    use_container_width=True
                )
            elif has_email:
                if is_notified:
                    btn_label = "Re-send"
                    btn_type = "secondary"
                else:
                    btn_label = "Send"
                    btn_type = "primary"
                if st.button(
                    btn_label,
                    key=_scope_key(ui_scope, f"send_{orcid_id}_{start_idx + idx}"),
                    type=btn_type,
                    use_container_width=True
                ):
                    st.session_state[pending_dialog_author_key] = dict(author)
                    st.session_state[pending_dialog_filters_key] = {
                        'publisher': filters.get('publisher', 'brevo'),
                        'invitation_type': invitation_type,
                        'suppress_after_send': bool(filters.get('suppress_after_send')),
                    }
                    st.rerun()
            else:
                st.button(
                    "—",
                    disabled=True,
                    key=_scope_key(ui_scope, f"no_email_{start_idx + idx}"),
                    use_container_width=True
                )
    
    st.divider()
    
    # Export buttons
    col1, col2 = st.columns(2)
    with col1:
        csv = df.drop(columns=['orcid_id', 'all_topics']).to_csv(index=False)
        st.download_button(
            "Export CSV",
            data=csv,
            file_name="authors.csv",
            mime="text/csv",
            use_container_width=True,
            key=_scope_key(ui_scope, "export_csv")
        )
    with col2:
        df_with_email = df[df['Email'] != '']
        if not df_with_email.empty:
            csv_email = df_with_email.drop(columns=['orcid_id', 'all_topics']).to_csv(index=False)
            st.download_button(
                f"Export With Email ({len(df_with_email)})",
                data=csv_email,
                file_name="authors_with_email.csv",
                mime="text/csv",
                use_container_width=True,
                key=_scope_key(ui_scope, "export_with_email_csv")
            )


def render_invitation_section(filters):
    """Render the invitation template section with editable fields."""
    
    st.header("Send Invitation")
    
    selected = st.session_state.selected_author
    journal_config = st.session_state.app_state.get('journal_config', {})
    publisher_id = filters.get('publisher', 'brevo')
    
    # Check if ready
    if not selected:
        st.info("Select an author from the table above to send an invitation.")
        return
    
    if not journal_config.get('name'):
        st.warning("Please configure journal details in the sidebar.")
        return
    
    is_already_notified = is_author_notified(selected.get('orcid_id', ''))
    
    if is_already_notified:
        st.error("⚠️ WARNING: This author has ALREADY been notified! Sending again will result in a DUPLICATE invitation.")
    recipient_suppressed = is_recipient_suppressed(selected.get('email', ''), selected.get('orcid_id', ''))
    if recipient_suppressed:
        st.warning("This recipient has unsubscribed before. Sending is blocked.")
    
    # Template selection
    col1, col2 = st.columns([2, 1])
    
    with col1:
        template_names = get_template_names()
        template_id = st.selectbox(
            "Invitation Type",
            options=list(template_names.keys()),
            format_func=lambda x: template_names[x],
            key="template_select"
        )
        scopus_indexed = st.checkbox("Journal is Scopus indexed", value=False, key="main_scopus")
    
    with col2:
        st.markdown(f"**Selected Author:** {selected['name']}")
        if selected.get('email'):
            st.markdown(f"**Author Email:** {selected['email']}")
        else:
            st.warning("No email available")
    
    # Format template - publisher name and location come from PUBLISHER_INFO (follows selected publisher)
    pub_info = PUBLISHER_INFO.get(publisher_id, {})
    publisher_name = pub_info.get('name') or (email_sender.get_publisher_name(publisher_id) if EMAIL_AVAILABLE else "")
    publisher_location = pub_info.get('location') or journal_config.get('location', '')
    sender_email = email_sender.get_publisher_email(publisher_id) if EMAIL_AVAILABLE else ""
    
    formatted = format_template(
        template_id=template_id,
        author_name=selected['name'],
        journal_name=journal_config.get('name', ''),
        journal_issn=journal_config.get('issn', ''),
        journal_link=journal_config.get('link', ''),
        editor_in_chief_name=journal_config.get('editor_in_chief', ''),
        publisher_name=publisher_name,
        sender_email=sender_email,
        publisher_location=publisher_location,
        scopus_indexed=scopus_indexed
    )
    
    st.divider()
    
    # Editable email fields
    st.subheader("Email Content (Editable)")
    
    # Use author's orcid_id to create unique keys so fields update when author changes
    author_key = selected.get('orcid_id', 'none')
    
    # To field - editable for testing
    default_to = selected.get('email', '') or ''
    to_email = st.text_input(
        "To (Author Email - editable)",
        value=default_to,
        placeholder="Enter email address (change for testing)",
        key=f"email_to_{author_key}"
    )
    
    # Subject - editable
    subject = st.text_input(
        "Subject",
        value=formatted['subject'],
        key=f"email_subject_{author_key}_{template_id}_{scopus_indexed}"
    )
    
    # Body - editable
    body = st.text_area(
        "Email Body",
        value=formatted['body'],
        height=350,
        key=f"email_body_{author_key}_{template_id}_{scopus_indexed}"
    )
    
    st.divider()
    
    # PDF attachment option
    col_opt1, col_opt2 = st.columns(2)
    with col_opt1:
        attach_pdf = st.checkbox("Attach PDF invitation letter", value=True, key="attach_pdf")
    with col_opt2:
        if attach_pdf:
            st.caption("PDF will include publisher letterhead")
    
    # Preview PDF
    if attach_pdf:
        with st.expander("Preview PDF"):
            try:
                pdf_bytes = generate_invitation_pdf(
                    publisher_id=publisher_id,
                    recipient_name=selected['name'],
                    email_body=body,
                    subject=subject,
                    journal_name=journal_config.get('name', ''),
                    journal_link=journal_config.get('link', '')
                )
                st.download_button(
                    "Download PDF Preview",
                    data=pdf_bytes,
                    file_name="Invitation_Letter_Preview.pdf",
                    mime="application/pdf"
                )
                st.success(f"PDF ready ({len(pdf_bytes):,} bytes)")
            except Exception as e:
                st.error(f"PDF generation error: {str(e)}")
    
    st.divider()
    
    # Confirmation checkbox for already notified authors
    confirm_resend = True  # Default to allowed
    if is_already_notified:
        confirm_resend = st.checkbox(
            "I confirm I want to send ANOTHER invitation to this already-notified author",
            value=False,
            key="confirm_resend_main"
        )
    
    # Action buttons
    col1, col2, col3 = st.columns(3)
    
    with col1:
        # Copy subject to clipboard workaround
        st.text_input("Copy Subject:", value=subject, key="copy_subject", disabled=True)
    
    with col2:
        if st.button("Show Body for Copy", use_container_width=True):
            st.code(body, language=None)
    
    with col3:
        # Send button
        send_blocked = recipient_suppressed or (is_already_notified and not confirm_resend)
        if EMAIL_AVAILABLE and to_email and not send_blocked:
            send_label = "Send Email with PDF" if attach_pdf else "Send Email"
            if st.button(send_label, type="primary", use_container_width=True):
                with st.spinner("Sending..."):
                    pdf_bytes = None
                    if attach_pdf:
                        try:
                            pdf_bytes = generate_invitation_pdf(
                                publisher_id=publisher_id,
                                recipient_name=selected['name'],
                                email_body=body,
                                subject=subject,
                                journal_name=journal_config.get('name', ''),
                                journal_link=journal_config.get('link', '')
                            )
                        except Exception as e:
                            st.error(f"PDF generation failed: {str(e)}")
                            pdf_bytes = None
                    
                    success, msg = email_sender.send_email(
                        publisher_id=publisher_id,
                        to_email=to_email,
                        subject=subject,
                        body=body,
                        to_name=selected['name'],
                        pdf_attachment=pdf_bytes,
                        journal_name=journal_config.get('name', ''),
                        journal_link=journal_config.get('link', ''),
                        submission_link=journal_config.get('submission_link', ''),
                        invitation_type=INVITATION_TYPE_EDITORIAL,
                        scopus_indexed=scopus_indexed,
                        journal_cite_score=journal_config.get('cite_score', ''),
                        journal_quartile=journal_config.get('quartile', ''),
                        unsubscribe_url=_build_unsubscribe_url(to_email, selected.get('orcid_id', '')),
                    )
                
                if success:
                    db_ok = mark_author_notified(
                        selected['orcid_id'],
                        author_name=selected.get('name', ''),
                        email=to_email,
                        publisher=publisher_id
                    )
                    st.success(f"Email sent to {to_email}!")
                    if not db_ok:
                        st.warning("Sent status could not be saved to the database; it may not persist across sessions.")
                    if to_email != selected.get('email'):
                        st.info("Note: Email was sent to a different address; author is still marked as notified.")
                    st.rerun()
                else:
                    st.error(f"Failed: {msg}")
        elif not to_email:
            st.warning("No email address. Enter one above.")
        elif send_blocked:
            st.warning("Check the confirmation box above to re-send to this already-notified author.")
        else:
            st.warning("Email sending not available.")


def render_collection_panel():
    """Control panel for the background email-collection worker."""
    st.caption(
        "Background service that continuously harvests author metadata and emails "
        "into the database for future processing. The worker runs as a separate process."
    )

    if not db_storage.available:
        st.error("Database unavailable — the collection service requires PostgreSQL (DATABASE_URL).")
        return

    summary = db_storage.get_collection_summary()
    run = summary.get("run") or {}
    status = run.get("status") or RUN_STATUS_IDLE

    status_styles = {
        "active": ("🟢", "ACTIVE"),
        "recovery": ("🟡", "RECOVERY"),
        "cooldown": ("🟠", "COOLDOWN"),
        "stopped_today": ("🔴", "STOPPED (next UTC day)"),
        "paused": ("⏸️", "PAUSED"),
        "idle": ("⚪", "IDLE / not started"),
    }
    icon, label = status_styles.get(status, ("⚪", status.upper()))

    header_col, refresh_col = st.columns([4, 1])
    with header_col:
        st.subheader(f"{icon} {label}")
    with refresh_col:
        if st.button("🔄 Refresh", key="collection_refresh"):
            st.rerun()

    # Live status metrics
    row1 = st.columns(4)
    row1[0].metric("Found this search", summary.get("emails_found_today", 0))
    row1[1].metric("Attempts this search", summary.get("attempts_today", 0))
    row1[2].metric("Hit rate", f"{summary.get('hit_rate', 0) * 100:.1f}%")
    row1[3].metric("ORCID 429 this search", summary.get("orcid_429_today", 0))

    row2 = st.columns(4)
    row2[0].metric("Queue pending", summary.get("queue_pending", 0))
    row2[1].metric("Available this search", summary.get("search_available", 0))
    row2[2].metric("Eff. concurrency", run.get("effective_concurrency", "—"))
    row2[3].metric("Eff. delay (s)", run.get("effective_delay", "—"))

    detail_bits = []
    if run.get("last_429_at"):
        detail_bits.append(f"Last 429: {run['last_429_at']}")
    if status == "cooldown" and run.get("cooldown_until"):
        detail_bits.append(f"Cooldown until: {run['cooldown_until']}")
    if status == "stopped_today" and run.get("stop_until"):
        detail_bits.append(f"Resumes: {run['stop_until']}")
    if run.get("seed_exhausted"):
        detail_bits.append("Seed cursor exhausted for current filters")
    if detail_bits:
        st.caption(" · ".join(str(b) for b in detail_bits))

    st.caption(f"{summary.get('total_collected', 0):,} emails stored across all searches")

    # Filters / configuration. Widgets are intentionally outside a form so the
    # OpenAlex typeahead and resume decision update as the draft changes.
    st.markdown("#### Targeting filters")
    source_id = run.get("search_run_id")
    if st.session_state.get("collection_config_source") != source_id:
        st.session_state["collection_config_source"] = source_id
        st.session_state["collection_keyword_tags"] = run.get("keyword_tags", "") or ""
        st.session_state["collection_disciplines"] = _parse_scientific_domains_json(run.get("disciplines_json"))
        st.session_state["collection_specialties_raw"] = ", ".join(
            _parse_scientific_domains_json(run.get("specialties_json"))
        )
        details = _parse_json_list(run.get("topic_details_json"))
        if not details:
            selected_topic_ids = _parse_scientific_domains_json(run.get("selected_topic_ids_json"))
            if not selected_topic_ids:
                selected_topic_ids = _parse_scientific_domains_json(run.get("topic_ids_json"))
            details = [
                {"id": topic_id, "name": topic_id, "subfield": "", "field": "", "works_count": 0}
                for topic_id in selected_topic_ids
            ]
        st.session_state["collection_selected_topics"] = details
        st.session_state["collection_h_min"] = int(run.get("h_index_min") or DEFAULT_H_INDEX_MIN)
        st.session_state["collection_h_max"] = int(run.get("h_index_max") or DEFAULT_H_INDEX_MAX)
        excluded_codes = set(_parse_scientific_domains_json(run.get("exclude_countries_json")))
        st.session_state["collection_excluded_countries"] = [
            name for name, code in COUNTRIES.items() if code in excluded_codes
        ]
        st.session_state["collection_concurrency"] = int(run.get("baseline_concurrency") or 2)
        st.session_state["collection_delay"] = float(run.get("baseline_delay") or 3.0)

    keyword_tags = st.text_input(
        "Keyword tags (comma-separated, resolved to OpenAlex topics)",
        key="collection_keyword_tags",
        help="e.g. machine learning, computer vision, genomics",
    )
    disciplines = st.multiselect(
        "Disciplines",
        options=ALL_DISCIPLINES,
        key="collection_disciplines",
        help="Authors must belong to one of these broad OpenAlex fields.",
    )

    st.markdown("**OpenAlex specialty topics**")
    if st_searchbox is not None:
        selected_topic = st_searchbox(
            _openalex_topic_options,
            key="collection_topic_searchbox",
            placeholder="Type at least 2 characters to search OpenAlex topics...",
            clear_on_submit=True,
            debounce=350,
        )
        if isinstance(selected_topic, dict) and selected_topic.get("id"):
            selected_topics = list(st.session_state.get("collection_selected_topics") or [])
            if selected_topic["id"] not in {topic.get("id") for topic in selected_topics}:
                selected_topics.append(selected_topic)
                st.session_state["collection_selected_topics"] = selected_topics
                st.rerun()
    else:
        st.info("Live topic suggestions are unavailable; manual specialty terms remain available below.")

    selected_topics = list(st.session_state.get("collection_selected_topics") or [])
    topic_by_label = {
        f"{topic.get('name') or topic.get('id')} [{topic.get('id')}]": topic
        for topic in selected_topics
    }
    kept_topic_labels = st.multiselect(
        "Selected topics (remove with ×)",
        options=list(topic_by_label),
        default=list(topic_by_label),
        key=f"collection_topic_chips_{source_id}",
    )
    selected_topics = [topic_by_label[label] for label in kept_topic_labels]
    st.session_state["collection_selected_topics"] = selected_topics

    with st.expander("Advanced: manual specialty substring terms"):
        specialties_raw = st.text_input(
            "Specialty terms (comma-separated substring match)",
            key="collection_specialties_raw",
            help="Retained for legacy or uncommon terms that do not appear in OpenAlex suggestions.",
        )

    col_a, col_b = st.columns(2)
    with col_a:
        h_index_min = st.number_input(
            "h-index min", min_value=0, max_value=500, key="collection_h_min"
        )
    with col_b:
        h_index_max = st.number_input(
            "h-index max", min_value=0, max_value=1000, key="collection_h_max"
        )
    exclude_countries = st.multiselect(
        "Exclude countries", options=list(COUNTRIES.keys()), key="collection_excluded_countries"
    )
    col_c, col_d = st.columns(2)
    with col_c:
        baseline_concurrency = st.slider(
            "Baseline concurrency", min_value=1, max_value=10, key="collection_concurrency"
        )
    with col_d:
        baseline_delay = st.slider(
            "Baseline delay (s)", min_value=1.0, max_value=10.0, step=0.5,
            key="collection_delay",
        )

    specialties = [s.strip() for s in specialties_raw.split(",") if s.strip()]
    exclude_codes = [COUNTRIES[name] for name in exclude_countries if name in COUNTRIES]
    draft_config = {
        "disciplines": disciplines,
        "specialties": specialties,
        "exclude_countries": exclude_codes,
        "keyword_tags": keyword_tags.strip(),
        "topic_ids": [topic.get("id") for topic in selected_topics if topic.get("id")],
        "topic_details": selected_topics,
        "h_index_min": int(h_index_min),
        "h_index_max": int(h_index_max),
    }
    checkpoint = db_storage.get_search_for_config(draft_config)
    checkpoint_complete = bool(
        checkpoint and checkpoint.get("seed_exhausted") and not int(checkpoint.get("pending_count") or 0)
    )

    st.markdown("#### Controls")
    ctrl = st.columns(4)
    primary_label = "⏯️ Resume search" if checkpoint else "▶️ Start new search"
    if checkpoint_complete:
        primary_label = "✅ Search complete"
    if ctrl[0].button(
        primary_label,
        key="collection_primary_action",
        use_container_width=True,
        type="primary",
        disabled=checkpoint_complete,
    ):
        activated = db_storage.activate_collection_search(
            draft_config,
            topic_details=selected_topics,
            baseline_concurrency=int(baseline_concurrency),
            baseline_delay=float(baseline_delay),
        )
        if activated:
            st.session_state["collection_config_source"] = None
            st.rerun()
        st.error("Could not start the search. Check the database connection.")
    if ctrl[1].button("⏸️ Pause", key="collection_pause", use_container_width=True):
        db_storage.set_run_status(RUN_STATUS_PAUSED)
        st.rerun()
    if ctrl[2].button("⏹️ Stop", key="collection_stop", use_container_width=True):
        db_storage.set_run_status(RUN_STATUS_IDLE)
        st.rerun()
    if checkpoint and ctrl[3].button(
        "🔄 Start over", key="collection_start_over", use_container_width=True
    ):
        st.session_state["collection_confirm_start_over"] = True

    if checkpoint and st.session_state.get("collection_confirm_start_over"):
        st.warning("Start over resets this search's cursor and metrics. Stored email outcomes are preserved.")
        confirm_cols = st.columns(2)
        if confirm_cols[0].button("Confirm start over", type="primary", use_container_width=True):
            activated = db_storage.activate_collection_search(
                draft_config,
                topic_details=selected_topics,
                start_over=True,
                baseline_concurrency=int(baseline_concurrency),
                baseline_delay=float(baseline_delay),
            )
            st.session_state["collection_confirm_start_over"] = False
            if activated:
                st.session_state["collection_config_source"] = None
                st.rerun()
            st.error("Could not start over. Check the database connection.")
        if confirm_cols[1].button("Cancel", use_container_width=True):
            st.session_state["collection_confirm_start_over"] = False
            st.rerun()

    # Recent collected authors
    st.markdown("#### Recently collected")
    recent = db_storage.get_recent_harvested(limit=25, status=EMAIL_STATUS_FOUND)
    if recent:
        recent_df = pd.DataFrame([
            {
                "Name": r.get("author_name"),
                "Email": r.get("email"),
                "ORCID": r.get("orcid_id"),
                "Discipline": r.get("discipline"),
                "Specialty": r.get("specialty"),
                "h-index": r.get("h_index"),
                "Country": r.get("country"),
                "Collected": r.get("updated_at"),
            }
            for r in recent
        ])
        st.dataframe(recent_df, use_container_width=True, hide_index=True)
    else:
        st.info("No emails collected yet. Start the worker and configure filters above.")


def main():
    """Main app entry point."""
    
    st.title("Editorial And Author Invitation Tool")
    st.caption("Find academic authors and send separate author or editorial invitations")
    
    # Render sidebar and get filters
    shared_filters = render_sidebar()

    views = [
        _workflow_label(WORKFLOW_AUTHOR),
        _workflow_label(WORKFLOW_EDITORIAL),
        "📥 Collection",
    ]
    active_view = st.radio(
        "Workspace",
        options=views,
        horizontal=True,
        key="active_workspace_view",
        label_visibility="collapsed",
    )

    if active_view == views[0]:
        st.caption("Publication-submission or editorial-role invitations with scientific-domain targeting.")
        author_filters = dict(shared_filters)
        author_filters['invitation_type'] = _workflow_invitation_type(WORKFLOW_AUTHOR)
        render_search_section(author_filters, WORKFLOW_AUTHOR)
    elif active_view == views[1]:
        st.caption("Editorial board-role invitations with editorial templates.")
        editorial_filters = dict(shared_filters)
        editorial_filters['invitation_type'] = _workflow_invitation_type(WORKFLOW_EDITORIAL)
        render_search_section(editorial_filters, WORKFLOW_EDITORIAL)
    else:
        render_collection_panel()


if __name__ == "__main__":
    main()
