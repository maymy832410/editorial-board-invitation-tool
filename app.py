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
from pdf_generator import generate_invitation_pdf


# Page config
st.set_page_config(
    page_title="Editorial Board Invitation Tool",
    page_icon="📬",
    layout="wide"
)

# Initialize state manager
state_mgr = StateManager()

# Initialize email sender (may fail if credentials missing)
try:
    email_sender = EmailSender()
    EMAIL_AVAILABLE = True
except FileNotFoundError:
    email_sender = None
    EMAIL_AVAILABLE = False

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


@st.dialog("Send Invitation Email", width="large")
def email_dialog(author: dict, filters: dict):
    """Dialog for composing and sending invitation email to a specific author."""
    
    journal_config = st.session_state.app_state.get('journal_config', {})
    publisher_id = filters.get('publisher', 'peninsula')
    
    # Author info header
    st.markdown(f"### To: **{author['name']}**")
    if author.get('institution'):
        st.caption(f"{author.get('institution')} | H-index: {author.get('h_index', 'N/A')}")
    
    st.divider()
    
    # Template selection
    template_names = get_template_names()
    template_id = st.selectbox(
        "Invitation Type",
        options=list(template_names.keys()),
        format_func=lambda x: template_names[x],
        key="dialog_template"
    )
    
    # Format template
    publisher_name = email_sender.get_publisher_name(publisher_id) if EMAIL_AVAILABLE else ""
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
        publisher_location=journal_config.get('location', '')
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
        key="dialog_subject"
    )
    
    body = st.text_area(
        "Email Body",
        value=formatted['body'],
        height=300,
        key="dialog_body"
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
    
    # Action buttons
    col1, col2 = st.columns(2)
    
    with col1:
        if st.button("Cancel", use_container_width=True):
            st.rerun()
    
    with col2:
        send_disabled = not EMAIL_AVAILABLE or not to_email
        if st.button("Send Email", type="primary", use_container_width=True, disabled=send_disabled):
            with st.spinner("Sending..."):
                # Generate PDF if needed
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
                
                # Send email
                success, msg = email_sender.send_email(
                    publisher_id=publisher_id,
                    to_email=to_email,
                    subject=subject,
                    body=body,
                    to_name=author['name'],
                    pdf_attachment=pdf_bytes
                )
                
                if success:
                    # Mark as notified if sent to original email
                    if to_email == author.get('email'):
                        sent_invitations = st.session_state.app_state.get('sent_invitations', set())
                        if isinstance(sent_invitations, list):
                            sent_invitations = set(sent_invitations)
                        sent_invitations.add(author['orcid_id'])
                        st.session_state.app_state['sent_invitations'] = sent_invitations
                        save_state()
                    
                    st.success(f"Email sent to {to_email}!")
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
        
        # Publisher Selection
        st.subheader("Publisher")
        
        if EMAIL_AVAILABLE:
            publishers = email_sender.get_publishers()
            publisher_options = {p['id']: f"{p['name']}" for p in publishers}
            
            current_publisher = st.session_state.app_state.get('publisher', 'peninsula')
            
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
            
            # Show publisher email
            st.caption(f"Sender: {email_sender.get_publisher_email(selected_publisher)}")
            
            # Test connection button
            if st.button("Test Email Connection", use_container_width=True):
                with st.spinner("Testing..."):
                    success, msg = email_sender.test_connection(selected_publisher)
                if success:
                    st.success(msg)
                else:
                    st.error(msg)
        else:
            st.warning("Email credentials not found. Create email_credentials.json")
            selected_publisher = 'peninsula'
        
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
        
        countries = st.multiselect(
            "Countries",
            options=list(COUNTRIES.keys()),
            default=search_params.get('countries', []),
            key="countries"
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
        
        # Web Search Email Finding
        st.subheader("Web Search for Emails")
        
        use_openai = st.checkbox(
            "Enable web search for emails",
            value=True,
            help="Use OpenAI to search the web and find real emails",
            key="use_openai"
        )
        
        if use_openai:
            st.caption("🔍 Searches faculty pages, Google Scholar, ResearchGate")
            st.caption("💰 Uses GPT-4o-mini with web search")
        
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
            'countries': countries,
            'max_results': max_results,
            'concurrent': concurrent,
            'delay': delay,
            'publisher': selected_publisher,
            'use_openai': use_openai
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
    
    country_codes = [COUNTRIES[c] for c in filters['countries']] if filters['countries'] else None
    
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
    if filters['countries']:
        search_info += f" | Countries: {', '.join(filters['countries'])}"
    if keywords:
        search_info += f" | Keywords: {', '.join(keywords[:3])}{'...' if len(keywords) > 3 else ''}"
    st.info(f"Searching: {search_info}")
    
    # Step 2: Get total count with topic filter
    with st.spinner("Counting matching authors..."):
        total_count = client.get_total_count(
            h_index_min=filters['h_min'],
            h_index_max=filters['h_max'],
            country_codes=country_codes,
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
            country_codes=country_codes,
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
        
        # Save results
        st.session_state.app_state['search_results'] = results
        st.session_state.app_state['processed_orcids'] = set()
        st.session_state.app_state['search_params'] = {
            'keyword_tags': keyword_tags,
            'h_index_min': filters['h_min'],
            'h_index_max': filters['h_max'],
            'countries': filters['countries'],
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
    use_openai = filters.get('use_openai', True)
    
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
            
            # Tavily + GPT-4o-mini fallback for authors without ORCID email
            if use_openai and authors_without_email:
                status_text.text(f"Searching web for emails ({len(authors_without_email)} authors)...")
                
                async def fetch_tavily_emails():
                    async with AsyncOpenAIEmailClient(
                        max_concurrent=min(5, filters['concurrent']),
                        delay_between_requests=0.5
                    ) as client:
                        return await client.fetch_emails_batch(authors_without_email)
                
                tavily_results = loop.run_until_complete(fetch_tavily_emails())
                
                # Update with web search results
                for result in tavily_results:
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
    status_text.text(f"Complete! Found {total_found} emails ({orcid_emails_found} ORCID + {openai_emails_found} Web)")
    st.session_state.stop_fetching = False


def display_results(filters):
    """Display search results with selection and filtering."""
    
    results = st.session_state.app_state.get('search_results', [])
    
    if not results:
        st.info("No results yet. Use the search button above.")
        return
    
    filtered = results.copy()
    
    sent_invitations = st.session_state.app_state.get('sent_invitations', set())
    if isinstance(sent_invitations, list):
        sent_invitations = set(sent_invitations)
    
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
    
    # Filter options - Row 2: Checkboxes
    col_filter1, col_filter2 = st.columns(2)
    with col_filter1:
        show_only_with_email = st.checkbox("Show only authors with email", value=False, key="filter_email")
    with col_filter2:
        show_only_not_sent = st.checkbox("Hide already notified", value=False, key="filter_not_sent")
    
    # Apply email filter
    if show_only_with_email:
        filtered = [r for r in filtered if r.get('email')]
    
    # Apply sent filter
    if show_only_not_sent:
        filtered = [r for r in filtered if r.get('orcid_id') not in sent_invitations]
    
    # Store filtered list in session state for email fetching
    st.session_state.filtered_authors = filtered
    
    # Count authors without email in filtered list
    without_email = sum(1 for r in filtered if not r.get('email'))
    
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
    
    if stop_clicked:
        st.session_state.stop_fetching = True
    
    if fetch_emails_clicked:
        run_email_fetch_filtered(filters)
    
    # Summary metrics
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Filtered Authors", len(filtered))
    with col2:
        with_email = sum(1 for r in filtered if r.get('email'))
        st.metric("With Email", with_email)
    with col3:
        sent_count = sum(1 for r in filtered if r.get('orcid_id') in sent_invitations)
        st.metric("Notified", sent_count)
    with col4:
        st.metric("Need Email", without_email)
    
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
        df_data.append({
            'Name': r.get('name', ''),
            'H-Index': r.get('h_index', ''),
            'Specialty': r.get('specialty', '') or '',
            'Discipline': r.get('discipline', ''),
            'Email': r.get('email', '') or '',
            'All_Emails': r.get('all_emails', '') or r.get('email', '') or '',
            'Institution': r.get('institution', ''),
            'Country': r.get('country', ''),
            'Notified': '✓' if orcid_id in sent_invitations else '',
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
    header_cols = st.columns([2.5, 0.7, 2, 1.5, 2, 1])
    with header_cols[0]:
        st.markdown("**Name**")
    with header_cols[1]:
        st.markdown("**H-Index**")
    with header_cols[2]:
        st.markdown("**Specialty**")
    with header_cols[3]:
        st.markdown("**Discipline**")
    with header_cols[4]:
        st.markdown("**Email**")
    with header_cols[5]:
        st.markdown("**Action**")
    
    st.divider()
    
    # Display rows with Send buttons (paginated for performance)
    page_size = 50
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
    
    # Display rows
    for idx, author in enumerate(page_results):
        orcid_id = author.get('orcid_id', '')
        is_notified = orcid_id in sent_invitations
        has_email = bool(author.get('email'))
        
        # Row styling based on status
        if is_notified:
            row_class = "notified-row"
        elif has_email:
            row_class = "pending-row"
        else:
            row_class = "no-email-row"
        
        cols = st.columns([2.5, 0.7, 2, 1.5, 2, 1])
        
        with cols[0]:
            name_display = author.get('name', '')
            if is_notified:
                st.markdown(f"✅ {name_display}")
            else:
                st.write(name_display)
        
        with cols[1]:
            st.write(author.get('h_index', ''))
        
        with cols[2]:
            specialty = author.get('specialty', '') or ''
            # Truncate long specialty names
            if len(specialty) > 30:
                specialty = specialty[:27] + "..."
            st.write(specialty)
        
        with cols[3]:
            st.write(author.get('discipline', ''))
        
        with cols[4]:
            email = author.get('email', '')
            all_emails = author.get('all_emails', '')
            if email:
                # Show all emails if multiple found, otherwise just primary
                display_email = all_emails if all_emails else email
                # Truncate long emails
                if len(display_email) > 30:
                    email_display = display_email[:27] + "..."
                else:
                    email_display = display_email
                # Show source indicator
                source = author.get('email_source', 'orcid')
                if source == 'web_search':
                    st.write(f"🔍 {email_display}")
                else:
                    st.write(email_display)
            else:
                st.caption("No email")
        
        with cols[5]:
            if has_email:
                btn_label = "✓ Sent" if is_notified else "Send"
                btn_type = "secondary" if is_notified else "primary"
                if st.button(btn_label, key=f"send_{orcid_id}_{start_idx + idx}", type=btn_type, use_container_width=True):
                    # Open dialog for this author
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
        # Export only with emails
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
    publisher_id = filters.get('publisher', 'peninsula')
    
    # Check if ready
    if not selected:
        st.info("Select an author from the table above to send an invitation.")
        return
    
    if not journal_config.get('name'):
        st.warning("Please configure journal details in the sidebar.")
        return
    
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
    
    with col2:
        st.markdown(f"**Selected Author:** {selected['name']}")
        if selected.get('email'):
            st.markdown(f"**Author Email:** {selected['email']}")
        else:
            st.warning("No email available")
    
    # Format template
    publisher_name = email_sender.get_publisher_name(publisher_id) if EMAIL_AVAILABLE else ""
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
        publisher_location=journal_config.get('location', '')
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
        key=f"email_subject_{author_key}_{template_id}"
    )
    
    # Body - editable
    body = st.text_area(
        "Email Body",
        value=formatted['body'],
        height=350,
        key=f"email_body_{author_key}_{template_id}"
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
        if EMAIL_AVAILABLE and to_email:
            send_label = "Send Email with PDF" if attach_pdf else "Send Email"
            if st.button(send_label, type="primary", use_container_width=True):
                with st.spinner("Sending..."):
                    # Generate PDF if needed
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
                    
                    # Send email
                    success, msg = email_sender.send_email(
                        publisher_id=publisher_id,
                        to_email=to_email,
                        subject=subject,
                        body=body,
                        to_name=selected['name'],
                        pdf_attachment=pdf_bytes
                    )
                
                if success:
                    st.success(f"Email sent to {to_email}!")
                    
                    # Mark as sent (only if sent to original email)
                    if to_email == selected.get('email'):
                        sent = st.session_state.app_state.get('sent_invitations', set())
                        if isinstance(sent, list):
                            sent = set(sent)
                        sent.add(selected['orcid_id'])
                        st.session_state.app_state['sent_invitations'] = sent
                        save_state()
                    else:
                        st.info("Note: Email was sent to a test address. Author not marked as notified.")
                    
                    st.rerun()
                else:
                    st.error(f"Failed: {msg}")
        elif not to_email:
            st.warning("No email address. Enter one above.")
        else:
            st.warning("Email sending not available.")
    
    # Show if already sent
    sent_invitations = st.session_state.app_state.get('sent_invitations', set())
    if isinstance(sent_invitations, list):
        sent_invitations = set(sent_invitations)
    
    if selected.get('orcid_id') in sent_invitations:
        st.success("This author has already been notified.")


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
