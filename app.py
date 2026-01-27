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
        
        disciplines = st.multiselect(
            "Filter by Discipline",
            options=ALL_DISCIPLINES,
            default=search_params.get('disciplines', []),
            help="Filter results after search",
            key="disciplines"
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
        st.subheader("Email Fetch Speed")
        
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
            'h_min': h_min,
            'h_max': h_max,
            'countries': countries,
            'disciplines': disciplines,
            'max_results': max_results,
            'concurrent': concurrent,
            'delay': delay,
            'publisher': selected_publisher
        }


def render_search_section(filters):
    """Render the search and results section."""
    
    st.header("Search Authors")
    
    # Search button
    col1, col2, col3 = st.columns([2, 1, 1])
    
    with col1:
        search_clicked = st.button("Search OpenAlex", type="primary", use_container_width=True)
    
    with col2:
        fetch_emails_clicked = st.button(
            "Fetch Emails",
            use_container_width=True,
            disabled=not st.session_state.app_state.get('search_results')
        )
    
    with col3:
        stop_clicked = st.button("Stop", use_container_width=True)
    
    if stop_clicked:
        st.session_state.stop_fetching = True
    
    # Handle search
    if search_clicked:
        run_search(filters)
    
    # Handle email fetching
    if fetch_emails_clicked:
        run_email_fetch(filters)
    
    # Display results
    display_results(filters['disciplines'])


def run_search(filters):
    """Execute the author search."""
    
    country_codes = [COUNTRIES[c] for c in filters['countries']] if filters['countries'] else None
    
    client = OpenAlexClient()
    
    # Show search info
    search_info = f"H-index: {filters['h_min']}-{filters['h_max']}"
    if filters['countries']:
        search_info += f" | Countries: {', '.join(filters['countries'])}"
    st.info(f"Searching: {search_info}")
    
    # Get total count
    with st.spinner("Counting matching authors..."):
        total_count = client.get_total_count(
            h_index_min=filters['h_min'],
            h_index_max=filters['h_max'],
            country_codes=country_codes,
            require_orcid=True
        )
    
    if total_count == 0:
        st.warning("No authors found. Try adjusting filters.")
        return
    
    st.success(f"Found {total_count:,} authors. Fetching up to {filters['max_results']:,}...")
    
    # Fetch authors
    results = []
    progress_bar = st.progress(0)
    status_text = st.empty()
    
    try:
        for i, author in enumerate(client.search_authors(
            h_index_min=filters['h_min'],
            h_index_max=filters['h_max'],
            country_codes=country_codes,
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
            'h_index_min': filters['h_min'],
            'h_index_max': filters['h_max'],
            'countries': filters['countries'],
            'disciplines': filters['disciplines'],
            'max_results': filters['max_results']
        }
        save_state()
        
    except Exception as e:
        st.error(f"Error: {str(e)}")


def run_email_fetch(filters):
    """Fetch emails for authors using async."""
    
    results = st.session_state.app_state.get('search_results', [])
    if not results:
        st.warning("No authors to process. Search first.")
        return
    
    processed = st.session_state.app_state.get('processed_orcids', set())
    if isinstance(processed, list):
        processed = set(processed)
    
    # Get authors without emails
    to_process = [
        {'orcid_id': a['orcid_id'], 'name': a['name']}
        for a in results
        if a.get('orcid_id') and a['orcid_id'] not in processed
    ]
    
    if not to_process:
        st.info("All authors already processed.")
        return
    
    st.session_state.stop_fetching = False
    
    # Progress display
    progress_bar = st.progress(0)
    col1, col2, col3 = st.columns(3)
    with col1:
        processed_metric = st.empty()
    with col2:
        found_metric = st.empty()
    with col3:
        speed_metric = st.empty()
    
    status_text = st.empty()
    
    # Process in batches
    batch_size = filters['concurrent'] * 5
    total = len(to_process)
    emails_found = 0
    start_time = time.time()
    
    for batch_start in range(0, total, batch_size):
        if st.session_state.stop_fetching:
            st.warning("Stopped by user")
            break
        
        batch = to_process[batch_start:batch_start + batch_size]
        status_text.text(f"Processing batch {batch_start // batch_size + 1}...")
        
        # Run async fetch
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
        finally:
            loop.close()
        
        # Update results
        for result in batch_results:
            orcid_id = result.get('orcid_id')
            email = result.get('email')
            
            if orcid_id:
                # Update in search results
                for author in st.session_state.app_state['search_results']:
                    if author.get('orcid_id') == orcid_id:
                        author['email'] = email
                        break
                
                processed.add(orcid_id)
                
                if email:
                    emails_found += 1
        
        st.session_state.app_state['processed_orcids'] = processed
        
        # Update metrics
        done = len(processed)
        progress_bar.progress(done / len(results))
        processed_metric.metric("Processed", f"{done}/{len(results)}")
        found_metric.metric("Emails Found", emails_found)
        
        elapsed = time.time() - start_time
        speed = done / elapsed if elapsed > 0 else 0
        speed_metric.metric("Speed", f"{speed:.1f}/sec")
        
        # Auto-save
        save_state()
    
    status_text.text("Email fetching complete!")
    st.session_state.stop_fetching = False


def display_results(discipline_filter):
    """Display search results with selection."""
    
    results = st.session_state.app_state.get('search_results', [])
    
    if not results:
        st.info("No results yet. Use the search button above.")
        return
    
    # Apply discipline filter
    if discipline_filter:
        filtered = [r for r in results if r.get('discipline') in discipline_filter]
    else:
        filtered = results
    
    sent_invitations = st.session_state.app_state.get('sent_invitations', set())
    if isinstance(sent_invitations, list):
        sent_invitations = set(sent_invitations)
    
    # Collect all unique specialties from results for autocomplete
    all_specialties = set()
    for r in results:
        if r.get('all_topics'):
            all_specialties.update(r['all_topics'])
        elif r.get('specialty'):
            all_specialties.add(r['specialty'])
    
    # Filter options - Row 1: Specialty autocomplete
    selected_specialty = st.selectbox(
        "Filter by Specialty (autocomplete)",
        options=["All Specialties"] + sorted(all_specialties),
        key="specialty_filter",
        help="Search and select a specific research topic"
    )
    
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
    
    # Summary metrics
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Total Authors", len(filtered))
    with col2:
        with_email = sum(1 for r in filtered if r.get('email'))
        st.metric("With Email", with_email)
    with col3:
        sent_count = sum(1 for r in filtered if r.get('orcid_id') in sent_invitations)
        st.metric("Notified", sent_count)
    with col4:
        pending = with_email - sent_count
        st.metric("Pending", max(0, pending))
    
    st.divider()
    
    # Results table with selection
    st.subheader(f"Authors ({len(filtered)})")
    
    # Prepare dataframe
    df_data = []
    for r in filtered:
        orcid_id = r.get('orcid_id', '')
        df_data.append({
            'Select': False,
            'Name': r.get('name', ''),
            'H-Index': r.get('h_index', ''),
            'Specialty': r.get('specialty', '') or '',
            'Discipline': r.get('discipline', ''),
            'Email': r.get('email', '') or '',
            'Institution': r.get('institution', ''),
            'Country': r.get('country', ''),
            'Notified': '✓' if orcid_id in sent_invitations else '',
            'orcid_id': orcid_id,
            'all_topics': r.get('all_topics', [])
        })
    
    if not df_data:
        st.info("No authors match the current filters.")
        return
    
    df = pd.DataFrame(df_data)
    
    # Use data editor for selection
    edited_df = st.data_editor(
        df,
        column_config={
            "Select": st.column_config.CheckboxColumn("Select", default=False, width="small"),
            "Name": st.column_config.TextColumn("Name", width="medium"),
            "H-Index": st.column_config.NumberColumn("H-Index", width="small"),
            "Specialty": st.column_config.TextColumn("Specialty", width="medium"),
            "Discipline": st.column_config.TextColumn("Discipline", width="small"),
            "Email": st.column_config.TextColumn("Email", width="medium"),
            "Institution": st.column_config.TextColumn("Institution", width="large"),
            "Country": st.column_config.TextColumn("Country", width="small"),
            "Notified": st.column_config.TextColumn("Notified", width="small"),
            "orcid_id": None,  # Hidden
            "all_topics": None  # Hidden
        },
        hide_index=True,
        use_container_width=True,
        disabled=['Name', 'H-Index', 'Specialty', 'Discipline', 'Email', 'Institution', 'Country', 'Notified', 'orcid_id', 'all_topics']
    )
    
    # Get selected authors
    selected_rows = edited_df[edited_df['Select'] == True]
    
    if not selected_rows.empty:
        selected_author = {
            'name': selected_rows.iloc[0]['Name'],
            'email': selected_rows.iloc[0]['Email'],
            'orcid_id': selected_rows.iloc[0]['orcid_id'],
            'specialty': selected_rows.iloc[0]['Specialty'],
            'discipline': selected_rows.iloc[0]['Discipline'],
            'institution': selected_rows.iloc[0]['Institution']
        }
        st.session_state.selected_author = selected_author
    
    # Export button
    col1, col2 = st.columns(2)
    with col1:
        csv = df.drop(columns=['Select', 'orcid_id', 'all_topics']).to_csv(index=False)
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
            csv_email = df_with_email.drop(columns=['Select', 'orcid_id', 'all_topics']).to_csv(index=False)
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
    
    st.divider()
    
    # Invitation section
    render_invitation_section(filters)


if __name__ == "__main__":
    main()
