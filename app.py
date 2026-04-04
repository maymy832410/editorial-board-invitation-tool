"""Editorial Board Invitation Tool - Streamlit App

A unified tool for finding academic authors and sending editorial board invitations.
"""

import asyncio
import time
import streamlit as st
import pandas as pd

from config import (
    COUNTRIES,
    DEFAULT_H_INDEX_MIN,
    DEFAULT_H_INDEX_MAX,
    DEFAULT_MAX_RESULTS,
)
from openalex_client import OpenAlexClient
from orcid_async import fetch_emails_async
from openai_email_async import AsyncOpenAIEmailClient
from progress_manager import StateManager
from disciplines import ALL_DISCIPLINES
from email_sender import EmailSender
from templates import (
    get_template_names,
    format_template,
    TEMPLATE_BOARD_MEMBER,
    TEMPLATE_MANAGING_EDITOR,
    TEMPLATE_EDITOR_IN_CHIEF,
)
from pdf_generator import generate_invitation_pdf, PUBLISHER_INFO
from db_client import get_storage as get_db_storage
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


def get_sent_invitations() -> set:
    """Get sent invitations: full list from DB (all rows) merged with local state so UI matches DB."""
    db_sent = db_storage.get_all_sent() if db_storage.available else set()
    # Local state (backup / same session)
    local_sent = st.session_state.app_state.get('sent_invitations', set())
    if isinstance(local_sent, list):
        local_sent = set(local_sent)
    return db_sent | local_sent


def is_author_notified(orcid_id: str) -> bool:
    """Check if author was notified (DB with local fallback)."""
    if db_storage.available:
        return db_storage.is_sent(orcid_id)
    
    sent = st.session_state.app_state.get('sent_invitations', set())
    if isinstance(sent, list):
        sent = set(sent)
    return orcid_id in sent


def mark_author_notified(orcid_id: str, author_name: str = "", email: str = "", publisher: str = "") -> bool:
    """Mark author as notified in DB and local state. Returns True if DB save succeeded."""
    db_ok = True
    if db_storage.available:
        db_ok = db_storage.mark_sent(orcid_id, author_name, email, publisher)

    # Always update local state (backup / offline)
    sent = st.session_state.app_state.get('sent_invitations', set())
    if isinstance(sent, list):
        sent = set(sent)
    sent.add(orcid_id)
    st.session_state.app_state['sent_invitations'] = sent
    save_state()
    return db_ok


@st.dialog("Send Invitation Email", width="large")
def email_dialog(author: dict, filters: dict):
    """Dialog for composing and sending invitation email to a specific author."""
    
    journal_config = st.session_state.app_state.get('journal_config', {})
    publisher_id = filters.get('publisher', 'brevo')
    
    is_already_notified = is_author_notified(author.get('orcid_id', ''))
    
    # WARNING BANNER for already notified authors
    if is_already_notified:
        st.error("⚠️ WARNING: This author has ALREADY been notified! Sending again will result in a DUPLICATE invitation.")
    
    # Author info header
    st.markdown(f"### To: **{author['name']}**")
    if author.get('institution'):
        st.caption(f"{author.get('institution')} | H-index: {author.get('h_index', 'N/A')}")
    
    st.divider()
    
    # Template selection
    template_names = get_template_names()
    col_type, col_scopus = st.columns([2, 1])
    with col_type:
        template_id = st.selectbox(
            "Invitation Type",
            options=list(template_names.keys()),
            format_func=lambda x: template_names[x],
            key="dialog_template"
        )
    with col_scopus:
        scopus_indexed = st.checkbox("Journal is Scopus indexed", value=False, key="dialog_scopus")
    
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
        scopus_indexed=scopus_indexed
    )
    
    # Editable email fields
    to_email = st.text_input(
        "To (Email)",
        value=author.get('email', ''),
        key="dialog_to"
    )
    subject = st.text_input(
        "Subject",
        value=formatted['subject'],
        key=f"dialog_subject_{template_id}_{scopus_indexed}"
    )
    
    body = st.text_area(
        "Email Body",
        value=formatted['body'],
        height=300,
        key=f"dialog_body_{template_id}_{scopus_indexed}"
    )
    
    # PDF option
    col1, col2 = st.columns(2)
    with col1:
        attach_pdf = st.checkbox("Attach PDF invitation letter", value=True, key="dialog_pdf")
    
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
                    file_name="Invitation_Letter_Preview.pdf",
                    mime="application/pdf"
                )
            except Exception as e:
                st.error(f"PDF error: {str(e)}")
    
    st.divider()
    
    # Confirmation checkbox for already notified authors
    confirm_resend = True  # Default to allowed
    if is_already_notified:
        confirm_resend = st.checkbox(
            "I confirm I want to send ANOTHER invitation to this already-notified author",
            value=False,
            key="dialog_confirm_resend"
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
                )
                
                if success:
                    db_ok = mark_author_notified(
                        author['orcid_id'],
                        author_name=author.get('name', ''),
                        email=to_email,
                        publisher=publisher_id
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
        st.subheader("Journal Details")
        
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
        
        # Auto-save journal config on change
        new_config = {
            'name': journal_name,
            'issn': journal_issn,
            'link': journal_link,
            'location': publisher_location,
            'editor_in_chief': editor_name
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
        
        max_results = st.number_input(
            "Max Results",
            min_value=10,
            max_value=5000,
            value=search_params.get('max_results', DEFAULT_MAX_RESULTS),
            step=100,
            key="max_results"
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
            'max_results': max_results,
            'concurrent': concurrent,
            'delay': delay,
            'publisher': selected_publisher,
            'use_tavily': use_tavily,
            'use_openai_web': use_openai_web
        }


def render_search_section(filters):
    """Render the search and results section."""
    
    st.header("Search Authors")
    
    # Search button
    col1, col2, col3 = st.columns([2, 1, 1])
    
    with col1:
        search_clicked = st.button("Search OpenAlex", type="primary", use_container_width=True)
    
    with col2:
        stop_clicked = st.button("Stop", use_container_width=True)
    
    with col3:
        pass  # Reserved for future use
    
    if stop_clicked:
        st.session_state.stop_fetching = True
    
    # Handle search
    if search_clicked:
        run_search(filters)
    
    # Display results (returns filtered list for email fetching)
    display_results(filters)


def run_search(filters):
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
    
    st.success(f"Found {total_count:,} authors. Fetching up to {filters['max_results']:,}...")
    
    # Step 3: Fetch authors with topic filter
    results = []
    progress_bar = st.progress(0)
    status_text = st.empty()
    
    try:
        for i, author in enumerate(client.search_authors(
            h_index_min=filters['h_min'],
            h_index_max=filters['h_max'],
            exclude_country_codes=exclude_country_codes,
            topic_ids=topic_ids,
            require_orcid=True,
            max_results=filters['max_results']
        )):
            # Add email field (to be filled later)
            author['email'] = None
            results.append(author)
            
            progress = min((i + 1) / min(total_count, filters['max_results']), 1.0)
            progress_bar.progress(progress)
            status_text.text(f"Fetched {i + 1} authors...")
        
        progress_bar.progress(1.0)
        status_text.text(f"Completed! Fetched {len(results)} authors.")
        
        # Save results and reset stale state
        st.session_state.app_state['search_results'] = results
        st.session_state.app_state['processed_orcids'] = set()
        st.session_state.filtered_authors = []  # clear stale filtered list
        st.session_state.app_state['search_params'] = {
            'keyword_tags': keyword_tags,
            'h_index_min': filters['h_min'],
            'h_index_max': filters['h_max'],
            'exclude_countries': filters['exclude_countries'],
            'max_results': filters['max_results']
        }
        save_state()
        
    except Exception as e:
        st.error(f"Error: {str(e)}")


def run_email_fetch_filtered(filters):
    """Fetch emails ONLY for currently filtered authors.
    
    Uses ORCID API first, then falls back to OpenAI inference for missing emails.
    """
    
    # Get filtered authors from session state
    filtered_authors = st.session_state.get('filtered_authors', [])
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


def display_results(filters):
    """Display search results with selection and filtering."""
    
    results = st.session_state.app_state.get('search_results', [])
    
    if not results:
        st.info("No results yet. Use the search button above.")
        return
    
    # Show email fetch result message from previous run
    if st.session_state.get('last_fetch_result'):
        st.success(st.session_state.pop('last_fetch_result'))
    
    filtered = results.copy()
    
    # Get sent invitations from DB (persistent)
    sent_invitations = get_sent_invitations()
    
    # Get retracted author names from DB (lowercased set for fast matching)
    retracted_names = db_storage.get_retracted_names() if db_storage.available else set()
    
    # Tag each author with retraction and sent status
    for r in filtered:
        r['is_retracted'] = r.get('name', '').lower() in retracted_names
    
    # Collect unique disciplines and specialties from results
    all_disciplines = set()
    all_specialties = set()
    for r in results:
        if r.get('discipline'):
            all_disciplines.add(r['discipline'])
        if r.get('all_topics'):
            all_specialties.update(r['all_topics'])
        elif r.get('specialty'):
            all_specialties.add(r['specialty'])
    
    # Filter options - Row 1: Discipline and Specialty filters
    col_f1, col_f2 = st.columns(2)
    
    with col_f1:
        # Discipline filter (multiselect)
        selected_disciplines = st.multiselect(
            "Filter by Discipline",
            options=sorted(all_disciplines),
            default=[],
            key="discipline_filter",
            help="Filter by broad discipline category"
        )
    
    with col_f2:
        # Specialty filter (single select with search)
        selected_specialty = st.selectbox(
            "Filter by Specialty",
            options=["All Specialties"] + sorted(all_specialties),
            key="specialty_filter",
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
            key="exclude_countries_postfilter",
            help="Exclude authors from these countries in the results below"
        )
        if excluded_display:
            excluded_codes = {opt.split("(")[-1].rstrip(")") for opt in excluded_display}
            filtered = [r for r in filtered if r.get('country') not in excluded_codes]
    
    # Filter options - Row 2: Checkboxes
    col_filter1, col_filter2, col_filter3 = st.columns(3)
    with col_filter1:
        show_only_with_email = st.checkbox("Show only authors with email", value=False, key="filter_email")
    with col_filter2:
        show_only_not_sent = st.checkbox("Hide already notified", value=False, key="filter_not_sent")
    with col_filter3:
        hide_retracted = st.checkbox("Hide retracted authors", value=True, key="filter_retracted")
    
    # Apply email filter
    if show_only_with_email:
        filtered = [r for r in filtered if r.get('email')]
    
    # Apply sent filter
    if show_only_not_sent:
        filtered = [r for r in filtered if r.get('orcid_id') not in sent_invitations]
    
    # Apply retraction filter
    if hide_retracted:
        filtered = [r for r in filtered if not r.get('is_retracted')]
    
    # Store filtered list in session state for email fetching
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
            disabled=without_email == 0
        )
    with col_btn2:
        stop_clicked = st.button("Stop Fetching", use_container_width=True)
    
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
        run_email_fetch_filtered(filters)
    
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
        st.metric("Previously Sent", sent_count)
    
    st.divider()
    
    # Results table with Send buttons
    st.subheader(f"Authors ({len(filtered)})")
    
    if not filtered:
        st.info("No authors match the current filters.")
        return
    
    # Prepare dataframe for export
    df_data = []
    for r in filtered:
        orcid_id = r.get('orcid_id', '')
        status = ''
        if r.get('is_retracted'):
            status = '🚫 RETRACTED'
        elif orcid_id in sent_invitations:
            status = '✅ SENT'
        df_data.append({
            'Name': r.get('name', ''),
            'H-Index': r.get('h_index', ''),
            'Specialty': r.get('specialty', '') or '',
            'Discipline': r.get('discipline', ''),
            'Email': r.get('email', '') or '',
            'All_Emails': r.get('all_emails', '') or r.get('email', '') or '',
            'Institution': r.get('institution', ''),
            'Country': r.get('country', ''),
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
    
    if 'results_page' not in st.session_state:
        st.session_state.results_page = 0
    
    # Pagination controls
    if total_pages > 1:
        col_prev, col_page, col_next = st.columns([1, 2, 1])
        with col_prev:
            if st.button("← Previous", disabled=st.session_state.results_page == 0):
                st.session_state.results_page -= 1
                st.rerun()
        with col_page:
            st.markdown(f"<center>Page {st.session_state.results_page + 1} of {total_pages}</center>", unsafe_allow_html=True)
        with col_next:
            if st.button("Next →", disabled=st.session_state.results_page >= total_pages - 1):
                st.session_state.results_page += 1
                st.rerun()
    
    # Get current page of results
    start_idx = st.session_state.results_page * page_size
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
    
    col_sel, col_skip, col_bulk = st.columns([1.2, 1.2, 1.6])
    with col_sel:
        select_all = st.checkbox(
            f"Select all on page ({len(page_with_email)} with email)",
            key=f"select_all_page_{st.session_state.results_page}"
        )
    with col_skip:
        skip_notified = st.checkbox("Skip already notified", value=True, key="bulk_skip_notified")
    
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
            key=f"bulk_send_{st.session_state.results_page}"
        )
    
    st.caption(f"Brevo daily sends: {st.session_state.bulk_sends_today}/{DAILY_CAP} used today")
    
    # --- Execute bulk send ---
    if bulk_send_clicked and select_all and batch_size > 0:
        journal_config = st.session_state.app_state.get('journal_config', {})
        publisher_id = filters.get('publisher', 'brevo')
        template_id = st.session_state.get('template_select', TEMPLATE_BOARD_MEMBER)
        scopus_indexed = st.session_state.get('main_scopus', False)
        attach_pdf = st.session_state.get('attach_pdf', True)
        
        pub_info = PUBLISHER_INFO.get(publisher_id, {})
        publisher_name = pub_info.get('name') or (email_sender.get_publisher_name(publisher_id) if EMAIL_AVAILABLE else "")
        publisher_location = pub_info.get('location') or journal_config.get('location', '')
        sender_email = email_sender.get_publisher_email(publisher_id) if EMAIL_AVAILABLE else ""
        
        batch = eligible[:batch_size]
        sent_ok = 0
        failed = 0
        errors = []
        
        progress_bar = st.progress(0, text="Starting bulk send...")
        status_area = st.empty()
        
        for i, author in enumerate(batch):
            author_name = author.get('name', 'Unknown')
            to_email = author.get('email', '')
            
            status_area.info(f"Sending {i+1}/{len(batch)}: {author_name} ({to_email})")
            
            formatted = format_template(
                template_id=template_id,
                author_name=author_name,
                journal_name=journal_config.get('name', ''),
                journal_issn=journal_config.get('issn', ''),
                journal_link=journal_config.get('link', ''),
                editor_in_chief_name=journal_config.get('editor_in_chief', ''),
                publisher_name=publisher_name,
                sender_email=sender_email,
                publisher_location=publisher_location,
                scopus_indexed=scopus_indexed
            )
            
            pdf_bytes = None
            if attach_pdf:
                try:
                    pdf_bytes = generate_invitation_pdf(
                        publisher_id=publisher_id,
                        recipient_name=author_name,
                        email_body=formatted['body'],
                        subject=formatted['subject'],
                        journal_name=journal_config.get('name', ''),
                        journal_link=journal_config.get('link', '')
                    )
                except Exception:
                    pdf_bytes = None
            
            success, msg = email_sender.send_email(
                publisher_id=publisher_id,
                to_email=to_email,
                subject=formatted['subject'],
                body=formatted['body'],
                to_name=author_name,
                pdf_attachment=pdf_bytes,
            )
            
            if success:
                sent_ok += 1
                st.session_state.bulk_sends_today += 1
                mark_author_notified(
                    author.get('orcid_id', ''),
                    author_name=author_name,
                    email=to_email,
                    publisher=publisher_id
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
    
    # --- Display rows ---
    for idx, author in enumerate(page_results):
        orcid_id = author.get('orcid_id', '')
        is_notified = orcid_id in sent_invitations
        is_retracted = author.get('is_retracted', False)
        has_email = bool(author.get('email'))
        
        if is_retracted:
            row_class = "retracted-row"
        elif is_notified:
            row_class = "notified-row"
        elif has_email:
            row_class = "pending-row"
        else:
            row_class = "no-email-row"
        
        cols = st.columns([2.2, 0.6, 1.8, 1.3, 0.7, 2, 1])
        
        with cols[0]:
            name_display = author.get('name', '')
            if is_retracted:
                st.markdown(f"🚫 ~~{name_display}~~ :red[RETRACTED]")
            elif is_notified:
                st.markdown(f"✅ **{name_display}** :green[SENT]")
            else:
                st.write(name_display)
        
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
                st.button("🚫", disabled=True, key=f"retracted_{orcid_id}_{start_idx + idx}", use_container_width=True)
            elif has_email:
                if is_notified:
                    btn_label = "Re-send"
                    btn_type = "secondary"
                else:
                    btn_label = "Send"
                    btn_type = "primary"
                if st.button(btn_label, key=f"send_{orcid_id}_{start_idx + idx}", type=btn_type, use_container_width=True):
                    email_dialog(author, filters)
            else:
                st.button("—", disabled=True, key=f"no_email_{start_idx + idx}", use_container_width=True)
    
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
            use_container_width=True
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
                use_container_width=True
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


def main():
    """Main app entry point."""
    
    st.title("Editorial Board Invitation Tool")
    st.caption("Find academic authors and send editorial board invitations")
    
    # Render sidebar and get filters
    filters = render_sidebar()
    
    # Main content
    render_search_section(filters)


if __name__ == "__main__":
    main()
