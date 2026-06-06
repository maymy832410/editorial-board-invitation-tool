"""Editorial Board Invitation Tool - Streamlit App

A unified tool for finding academic authors and sending editorial board invitations.
"""

import asyncio
import json
import time
import streamlit as st
import pandas as pd

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
from disciplines import ALL_DISCIPLINES
from email_sender import EmailSender
from templates import (
    get_template_names,
    get_publication_template_ids,
    format_template,
    format_recent_publications,
    choose_rotating_template,
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
)

WORKFLOW_AUTHOR = "author"
WORKFLOW_EDITORIAL = "editorial"

AUTHOR_SOURCE_OPENALEX = "openalex"
AUTHOR_SOURCE_DATABASE = "database"
AUTHOR_SOURCE_BOTH = "both"


# Page config
st.set_page_config(
    page_title="Editorial Board Invitation Tool",
    page_icon="📬",
    layout="wide"
)

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


def _merge_author_source_results(openalex_rows: list[dict], db_rows: list[dict]) -> list[dict]:
    """Merge OpenAlex and DB rows by ORCID with deterministic field precedence."""
    merged_rows: list[dict] = []
    index_by_orcid: dict[str, dict] = {}

    for openalex_row in openalex_rows:
        row = dict(openalex_row)
        row.setdefault('profile_key', '')
        row.setdefault('source_origin', AUTHOR_SOURCE_OPENALEX)
        row.setdefault('scientific_domain', _clean_domain_label(row.get('discipline', '')))
        merged_rows.append(row)
        orcid_id = row.get('orcid_id', '')
        if orcid_id:
            index_by_orcid[orcid_id] = row

    for db_row in db_rows:
        orcid_id = db_row.get('orcid_id', '')
        if not orcid_id:
            continue

        existing = index_by_orcid.get(orcid_id)
        if not existing:
            merged_rows.append(dict(db_row))
            index_by_orcid[orcid_id] = merged_rows[-1]
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
    
    is_already_notified = is_author_notified(
        author.get('orcid_id', ''),
        invitation_type=invitation_type,
        journal_name=tracking_journal_name
    )
    
    # WARNING BANNER for already notified authors
    if is_already_notified:
        st.error(f"⚠️ WARNING: This author has ALREADY been sent a {_invitation_type_label(invitation_type)} invitation for this tracking scope.")
    
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
        send_disabled = not EMAIL_AVAILABLE or not to_email or (is_already_notified and not confirm_resend)
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
                )
                
                if success:
                    db_ok = mark_author_notified(
                        author['orcid_id'],
                        author_name=author.get('name', ''),
                        email=to_email,
                        publisher=publisher_id,
                        invitation_type=invitation_type,
                        journal_name=tracking_journal_name,
                        template_id=template_id,
                        cite_score=journal_config.get('cite_score', ''),
                        quartile=journal_config.get('quartile', '')
                    )
                    st.success(f"Email sent to {to_email}!")
                    if not db_ok:
                        st.warning("Sent status could not be saved to the database; it may not persist across sessions.")
                    time.sleep(1)
                    st.rerun()
                else:
                    st.error(f"Failed: {msg}")
    
    if not EMAIL_AVAILABLE:
        st.warning("Email sending not configured. Add email_credentials.json.")


@st.dialog("Confirm Bulk Send", width="large")
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
        f"You are about to send **{len(batch)}** {_invitation_type_label(invitation_type).lower()} emails. "
        f"Please confirm before proceeding."
    )
    st.caption(
        f"Sample recipient: {sample_author.get('name', 'Author')} "
        f"<{sample_author.get('email', '')}> | Template: {template_name} | "
        f"PDF attachment: {'Yes' if bulk_attach_pdf else 'No'}"
    )

    col_cancel, col_confirm = st.columns(2)
    with col_cancel:
        if st.button("Cancel", use_container_width=True, key=f"bulk_preview_cancel_{dialog_key}"):
            st.rerun()
    with col_confirm:
        if st.button(
            f"Confirm and Send {len(batch)} Emails",
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
        
        # Publisher Selection
        st.subheader("Publisher")
        
        if EMAIL_AVAILABLE:
            publishers = email_sender.get_publishers()
            publisher_options = {p['id']: f"{p['name']}" for p in publishers}
            
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
            quartile_options = ["", "Q1", "Q2", "Q3", "Q4"]
            quartile = st.selectbox(
                "Quartile",
                options=quartile_options,
                index=_safe_select_index(quartile_options, journal_config.get('quartile', '')),
                key="journal_quartile"
            )

        indexing_options = ["", "Not indexed", "Scopus", "Web of Science", "DOAJ", "Other"]
        indexing_status = st.selectbox(
            "Indexing Status",
            options=indexing_options,
            index=_safe_select_index(indexing_options, journal_config.get('indexing_status', '')),
            key="journal_indexing_status"
        )

        goal_options = ["Regular submission", "Special issue", "Review article", "Fast-track consideration"]
        invitation_goal = st.selectbox(
            "Invitation Goal",
            options=goal_options,
            index=_safe_select_index(goal_options, journal_config.get('invitation_goal', 'Regular submission')),
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


def render_search_section(filters, ui_scope: str):
    """Render the search and results section."""
    invitation_type = filters.get('invitation_type', INVITATION_TYPE_EDITORIAL)
    is_author_workflow = invitation_type == INVITATION_TYPE_PUBLICATION and ui_scope == WORKFLOW_AUTHOR
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

    st.header("Search Authors")
    st.caption(f"Active workflow: {_invitation_type_label(invitation_type)}")
    if is_author_workflow:
        st.caption(f"Source mode: {_author_source_label(author_source_mode)}")

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
    
    exclude_country_codes = [COUNTRIES[c] for c in filters['exclude_countries']] if filters['exclude_countries'] else None
    
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


def _execute_bulk_send(payload: dict, invitation_type, tracking_journal_name, filters):
    """Run a confirmed bulk send, rendering the progress bar at the top of the page."""
    from datetime import date as _date
    DAILY_CAP = 280
    if 'bulk_send_date' not in st.session_state or st.session_state.bulk_send_date != str(_date.today()):
        st.session_state.bulk_send_date = str(_date.today())
        st.session_state.bulk_sends_today = 0

    batch = list(payload.get('batch') or [])
    remaining_today_now = max(0, DAILY_CAP - st.session_state.bulk_sends_today)
    if remaining_today_now <= 0:
        st.warning("Bulk send cancelled: daily limit reached.")
        st.rerun()
    batch = batch[:remaining_today_now]
    if not batch:
        st.warning("Bulk send cancelled: no eligible authors remained.")
        st.rerun()

    payload_invitation_type = payload.get('invitation_type', invitation_type)
    payload_tracking_journal_name = payload.get('tracking_journal_name', tracking_journal_name)
    payload_publisher_id = payload.get('publisher_id', filters.get('publisher', 'brevo'))
    payload_selected_template = payload.get('selected_bulk_template', TEMPLATE_BOARD_MEMBER)
    payload_template_strategy = payload.get('bulk_template_strategy', 'Use selected template')
    payload_scopus_indexed = bool(payload.get('bulk_scopus_indexed', False))
    payload_attach_pdf = bool(payload.get('bulk_attach_pdf', True))
    payload_include_cached_publications = bool(payload.get('bulk_include_cached_publications', False))
    payload_journal_config = payload.get('journal_config', {}) or {}

    pub_info = PUBLISHER_INFO.get(payload_publisher_id, {})
    publisher_name = pub_info.get('name') or (email_sender.get_publisher_name(payload_publisher_id) if EMAIL_AVAILABLE else "")
    publisher_location = pub_info.get('location') or payload_journal_config.get('location', '')
    sender_email = email_sender.get_publisher_email(payload_publisher_id) if EMAIL_AVAILABLE else ""

    sent_ok = 0
    failed = 0
    errors = []

    st.subheader(f"Sending {len(batch)} {_invitation_type_label(payload_invitation_type).lower()} emails…")
    progress_bar = st.progress(0, text="Starting bulk send...")
    status_area = st.empty()

    for i, author in enumerate(batch):
        author_name = author.get('name', 'Unknown')
        to_email = author.get('email', '')

        status_area.info(f"Sending {i+1}/{len(batch)}: {author_name} ({to_email})")

        if payload_invitation_type == INVITATION_TYPE_PUBLICATION and payload_template_strategy == "Rotate publication templates":
            template_id = choose_rotating_template(get_publication_template_ids(), i)
        else:
            template_id = payload_selected_template

        recent_publications_text = ""
        if payload_invitation_type == INVITATION_TYPE_PUBLICATION and payload_include_cached_publications:
            recent_publications_text = format_recent_publications(author.get('recent_publications') or [])

        formatted = format_template(
            template_id=template_id,
            author_name=author_name,
            journal_name=payload_journal_config.get('name', ''),
            journal_issn=payload_journal_config.get('issn', ''),
            journal_link=payload_journal_config.get('link', ''),
            editor_in_chief_name=payload_journal_config.get('editor_in_chief', ''),
            publisher_name=publisher_name,
            sender_email=sender_email,
            publisher_location=publisher_location,
            scopus_indexed=payload_scopus_indexed,
            journal_submission_link=payload_journal_config.get('submission_link', ''),
            journal_cite_score=payload_journal_config.get('cite_score', ''),
            journal_quartile=payload_journal_config.get('quartile', ''),
            journal_indexing_status=payload_journal_config.get('indexing_status', ''),
            author_specialty=author.get('specialty') or author.get('research_areas') or '',
            author_recent_publications=recent_publications_text,
            journal_scope=payload_journal_config.get('scope', ''),
            invitation_goal=payload_journal_config.get('invitation_goal', ''),
        )

        pdf_bytes = None
        if payload_attach_pdf:
            try:
                pdf_bytes = generate_invitation_pdf(
                    publisher_id=payload_publisher_id,
                    recipient_name=author_name,
                    email_body=formatted['body'],
                    subject=formatted['subject'],
                    journal_name=payload_journal_config.get('name', ''),
                    journal_link=payload_journal_config.get('link', ''),
                )
            except Exception:
                pdf_bytes = None

        success, msg = email_sender.send_email(
            publisher_id=payload_publisher_id,
            to_email=to_email,
            subject=formatted['subject'],
            body=formatted['body'],
            to_name=author_name,
            pdf_attachment=pdf_bytes,
            attachment_filename="Publication_Invitation_Letter.pdf" if payload_invitation_type == INVITATION_TYPE_PUBLICATION else "Invitation_Letter.pdf",
            journal_name=payload_journal_config.get('name', ''),
            journal_link=payload_journal_config.get('link', ''),
            submission_link=payload_journal_config.get('submission_link', ''),
            invitation_type=payload_invitation_type,
            scopus_indexed=payload_scopus_indexed,
            journal_cite_score=payload_journal_config.get('cite_score', ''),
            journal_quartile=payload_journal_config.get('quartile', ''),
        )

        if success:
            sent_ok += 1
            st.session_state.bulk_sends_today += 1
            mark_author_notified(
                author.get('orcid_id', ''),
                author_name=author_name,
                email=to_email,
                publisher=payload_publisher_id,
                invitation_type=payload_invitation_type,
                journal_name=payload_tracking_journal_name,
                template_id=template_id,
                cite_score=payload_journal_config.get('cite_score', ''),
                quartile=payload_journal_config.get('quartile', ''),
            )
        else:
            failed += 1
            errors.append(f"{author_name}: {msg}")

        progress_bar.progress((i + 1) / len(batch), text=f"Sent {sent_ok}, failed {failed} of {len(batch)}")

        if i < len(batch) - 1:
            time.sleep(2)

    progress_bar.progress(1.0, text="Done!")
    if sent_ok > 0:
        st.success(f"Bulk send complete: {sent_ok}/{len(batch)} sent successfully.")
    if failed > 0:
        st.warning(f"{failed} failed:")
        for err in errors:
            st.caption(f"  - {err}")
    time.sleep(2)
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

    # Run a confirmed bulk send before any other rendering so the progress bar is visible at the top.
    confirmed_bulk_payload = st.session_state.pop(confirmed_bulk_send_key, None)
    if isinstance(confirmed_bulk_payload, dict):
        _execute_bulk_send(confirmed_bulk_payload, invitation_type, tracking_journal_name, filters)

    is_author_workflow = invitation_type == INVITATION_TYPE_PUBLICATION and ui_scope == WORKFLOW_AUTHOR
    author_source_mode = filters.get(
        'author_source_mode',
        st.session_state.app_state.get('author_source_mode', AUTHOR_SOURCE_BOTH),
    )

    db_source_results: list[dict] = []
    db_source_total = 0
    db_source_limit = int(filters.get('max_results', DEFAULT_MAX_RESULTS))
    if is_author_workflow and author_source_mode in {AUTHOR_SOURCE_DATABASE, AUTHOR_SOURCE_BOTH}:
        if db_storage.available:
            db_source_total = db_storage.count_author_profile_candidates()
        db_source_results = _load_author_source_rows_from_db(limit=db_source_limit)

    if is_author_workflow and author_source_mode == AUTHOR_SOURCE_DATABASE:
        results = db_source_results
    elif is_author_workflow and author_source_mode == AUTHOR_SOURCE_BOTH:
        results = _merge_author_source_results(openalex_results, db_source_results)
    else:
        results = openalex_results
    
    if not results:
        if is_author_workflow and author_source_mode == AUTHOR_SOURCE_DATABASE:
            st.info("No author_profiles rows with ORCID + email were found in the database source.")
        else:
            st.info("No results yet. Use the search button above.")
        return

    if is_author_workflow and author_source_mode in {AUTHOR_SOURCE_DATABASE, AUTHOR_SOURCE_BOTH}:
        db_count = len(db_source_results)
        openalex_count = len(openalex_results)
        st.caption(
            f"Source counts: OpenAlex={openalex_count:,}, "
            f"DatabaseLoaded={db_count:,}/{db_source_total:,}, Displayed={len(results):,}."
        )
        if db_source_total > db_count:
            st.info(
                f"Database source currently loads the first {db_count:,} of {db_source_total:,} rows "
                f"because Max Results is {db_source_limit:,}. Increase Max Results to load more."
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

    # Reuse known database emails so invitations can be sent without refetching.
    if _hydrate_result_emails_from_db(filtered):
        if show_openalex_batch_controls:
            _sync_current_batch_cache()
        save_state()
    
    # Get sent invitations from DB (persistent) for the active invitation workflow.
    sent_invitations = get_sent_invitations(invitation_type, tracking_journal_name)
    
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
        # Specialty filter (single select with search)
        selected_specialty = st.selectbox(
            "Filter by Specialty",
            options=["All Specialties"] + sorted(all_specialties),
            key=_scope_key(ui_scope, "specialty_filter"),
            help="Select a specific research topic"
        )
    
    # Apply discipline filter
    if selected_disciplines:
        filtered = [r for r in filtered if r.get('discipline') in selected_disciplines]
    
    # Apply specialty filter
    if selected_specialty != "All Specialties":
        filtered = [
            r for r in filtered 
            if selected_specialty in (r.get('all_topics') or []) or r.get('specialty') == selected_specialty
        ]
    
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
        filtered = [r for r in filtered if r.get('orcid_id') not in sent_invitations]
    
    # Apply retraction filter
    if hide_retracted:
        filtered = [r for r in filtered if not r.get('is_retracted')]
        if retracted_in_scope:
            st.caption(f"Hidden retracted authors: {retracted_in_scope:,}")
    
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
        sent_count = sum(1 for r in filtered if r.get('orcid_id') in sent_invitations)
        st.metric(f"Sent ({_invitation_type_label(invitation_type)})", sent_count)
    
    st.divider()
    
    # Results table with Send buttons
    st.subheader(f"Authors ({len(filtered)})")
    
    if not filtered:
        st.info("No authors match the current filters.")
        return

    invitation_counts: dict[str, int] = {}
    if db_storage.available:
        try:
            invitation_counts = db_storage.get_invitation_counts([
                row.get('orcid_id', '')
                for row in filtered
                if row.get('orcid_id')
            ])
        except Exception:
            invitation_counts = {}
    
    # Prepare dataframe for export
    df_data = []
    for r in filtered:
        orcid_id = r.get('orcid_id', '')
        invited_count = int(invitation_counts.get(orcid_id, 0))
        source_origin = r.get('source_origin', AUTHOR_SOURCE_OPENALEX)
        status = ''
        if r.get('is_retracted'):
            status = '🚫 RETRACTED'
        elif orcid_id in sent_invitations:
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
    from datetime import date as _date
    DAILY_CAP = 280
    if 'bulk_send_date' not in st.session_state or st.session_state.bulk_send_date != str(_date.today()):
        st.session_state.bulk_send_date = str(_date.today())
        st.session_state.bulk_sends_today = 0
    
    page_with_email = [a for a in page_results if a.get('email')]
    page_not_sent = [a for a in page_with_email if a.get('orcid_id', '') not in sent_invitations and not a.get('is_retracted')]

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
            bulk_template_strategy = st.selectbox(
                "Template Strategy",
                options=["Rotate publication templates", "Use selected template"],
                key=_scope_key(ui_scope, "bulk_publication_template_strategy")
            )
            bulk_scopus_indexed = False
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
    
    col_sel, col_skip, col_bulk = st.columns([1.2, 1.2, 1.6])
    with col_sel:
        select_all = st.checkbox(
            f"Select all on page ({len(page_with_email)} with email)",
            key=_scope_key(ui_scope, f"select_all_page_{current_results_page}")
        )
    with col_skip:
        skip_notified = st.checkbox(
            "Skip already notified",
            value=True,
            key=_scope_key(ui_scope, "bulk_skip_notified")
        )
    
    eligible = page_not_sent if skip_notified else page_with_email
    remaining_today = max(0, DAILY_CAP - st.session_state.bulk_sends_today)
    batch_size = min(len(eligible), remaining_today) if select_all else 0
    
    with col_bulk:
        if remaining_today == 0:
            st.warning("Daily limit reached (280/day)")
        elif select_all and batch_size < len(eligible):
            st.caption(f"Capped to {batch_size} (daily limit: {remaining_today} left)")
        
        bulk_send_clicked = st.button(
            f"Send Bulk ({batch_size} emails)" if select_all else "Select all to bulk send",
            type="primary" if select_all and batch_size > 0 and EMAIL_AVAILABLE else "secondary",
            disabled=not (select_all and batch_size > 0 and EMAIL_AVAILABLE),
            use_container_width=True,
            key=_scope_key(ui_scope, f"bulk_send_{current_results_page}")
        )
    
    st.caption(f"Brevo daily sends: {st.session_state.bulk_sends_today}/{DAILY_CAP} used today")

    if bulk_send_clicked and select_all and batch_size > 0:
        st.session_state[pending_bulk_dialog_key] = {
            'batch': [dict(author) for author in eligible[:batch_size]],
            'invitation_type': invitation_type,
            'tracking_journal_name': tracking_journal_name,
            'publisher_id': filters.get('publisher', 'brevo'),
            'selected_bulk_template': selected_bulk_template,
            'bulk_template_strategy': bulk_template_strategy,
            'bulk_scopus_indexed': bulk_scopus_indexed,
            'bulk_attach_pdf': bulk_attach_pdf,
            'bulk_include_cached_publications': bulk_include_cached_publications,
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
        is_notified = orcid_id in sent_invitations
        is_retracted = author.get('is_retracted', False)
        has_email = bool(author.get('email'))
        
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
                st.caption(f"Source: {_author_source_label(source_origin)}")
            if invited_count > 0:
                st.caption(f"Invited: {invited_count}")
        
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
            if is_retracted:
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
        send_blocked = is_already_notified and not confirm_resend
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
    row1[0].metric("Collected today", summary.get("emails_found_today", 0))
    row1[1].metric("Attempts today", summary.get("attempts_today", 0))
    row1[2].metric("Hit rate", f"{summary.get('hit_rate', 0) * 100:.1f}%")
    row1[3].metric("ORCID 429 today", summary.get("orcid_429_today", 0))

    row2 = st.columns(4)
    row2[0].metric("Queue pending", summary.get("queue_pending", 0))
    row2[1].metric("Total collected", summary.get("total_collected", 0))
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

    # Controls
    st.markdown("#### Controls")
    ctrl = st.columns(4)
    if ctrl[0].button("▶️ Start", key="collection_start", use_container_width=True):
        db_storage.set_run_status(RUN_STATUS_ACTIVE)
        st.rerun()
    if ctrl[1].button("⏸️ Pause", key="collection_pause", use_container_width=True):
        db_storage.set_run_status(RUN_STATUS_PAUSED)
        st.rerun()
    if ctrl[2].button("⏯️ Resume", key="collection_resume", use_container_width=True):
        db_storage.set_run_status(RUN_STATUS_ACTIVE)
        st.rerun()
    if ctrl[3].button("⏹️ Stop", key="collection_stop", use_container_width=True):
        db_storage.set_run_status(RUN_STATUS_IDLE)
        st.rerun()

    # Filters / configuration
    st.markdown("#### Targeting filters")
    with st.form("collection_config_form"):
        keyword_tags = st.text_input(
            "Keyword tags (comma-separated, resolved to OpenAlex topics)",
            value=run.get("keyword_tags", "") or "",
            help="e.g. machine learning, computer vision, genomics",
        )
        disciplines = st.multiselect(
            "Disciplines (post-filter on OpenAlex field)",
            options=ALL_DISCIPLINES,
            default=_parse_scientific_domains_json(run.get("disciplines_json")),
        )
        specialties_raw = st.text_input(
            "Specialty terms (comma-separated substring match)",
            value=", ".join(_parse_scientific_domains_json(run.get("specialties_json"))),
        )
        col_a, col_b = st.columns(2)
        with col_a:
            h_index_min = st.number_input(
                "h-index min", min_value=0, max_value=500,
                value=int(run.get("h_index_min") or DEFAULT_H_INDEX_MIN),
            )
        with col_b:
            h_index_max = st.number_input(
                "h-index max", min_value=0, max_value=1000,
                value=int(run.get("h_index_max") or DEFAULT_H_INDEX_MAX),
            )
        exclude_countries = st.multiselect(
            "Exclude countries",
            options=list(COUNTRIES.keys()),
            default=[
                name for name, code in COUNTRIES.items()
                if code in _parse_scientific_domains_json(run.get("exclude_countries_json"))
            ],
        )
        col_c, col_d = st.columns(2)
        with col_c:
            baseline_concurrency = st.slider(
                "Baseline concurrency", min_value=1, max_value=10,
                value=int(run.get("baseline_concurrency") or 2),
            )
        with col_d:
            baseline_delay = st.slider(
                "Baseline delay (s)", min_value=1.0, max_value=10.0, step=0.5,
                value=float(run.get("baseline_delay") or 3.0),
            )
        reset_cursor = st.checkbox(
            "Reset OpenAlex seed cursor (re-scan from the start with new filters)",
            value=False,
        )
        submitted = st.form_submit_button("💾 Save filters", use_container_width=True)

    if submitted:
        specialties = [s.strip() for s in specialties_raw.split(",") if s.strip()]
        exclude_codes = [COUNTRIES[name] for name in exclude_countries if name in COUNTRIES]
        db_storage.set_run_config(
            disciplines=disciplines,
            specialties=specialties,
            exclude_countries=exclude_codes,
            keyword_tags=keyword_tags.strip(),
            topic_ids=[] if reset_cursor else None,
            h_index_min=int(h_index_min),
            h_index_max=int(h_index_max),
            baseline_concurrency=int(baseline_concurrency),
            baseline_delay=float(baseline_delay),
            reset_cursor=reset_cursor,
        )
        st.success("Filters saved. The worker will pick them up on its next cycle.")
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

    author_tab, editorial_tab, collection_tab = st.tabs([
        _workflow_label(WORKFLOW_AUTHOR),
        _workflow_label(WORKFLOW_EDITORIAL),
        "📥 Collection",
    ])

    with author_tab:
        st.caption("Publication-submission invitations with scientific-domain targeting.")
        author_filters = dict(shared_filters)
        author_filters['invitation_type'] = _workflow_invitation_type(WORKFLOW_AUTHOR)
        render_search_section(author_filters, WORKFLOW_AUTHOR)

    with editorial_tab:
        st.caption("Editorial board-role invitations with editorial templates.")
        editorial_filters = dict(shared_filters)
        editorial_filters['invitation_type'] = _workflow_invitation_type(WORKFLOW_EDITORIAL)
        render_search_section(editorial_filters, WORKFLOW_EDITORIAL)

    with collection_tab:
        render_collection_panel()


if __name__ == "__main__":
    main()
