"""Editorial Board Invitation Tool v2 - FastAPI + HTMX"""

import json
import os
import sys
import time
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, Response, Form, Cookie, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# Add parent directory to path so we can reuse existing modules
PARENT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PARENT_DIR))

from config import (
    COUNTRIES,
    DEFAULT_H_INDEX_MIN,
    DEFAULT_H_INDEX_MAX,
    DEFAULT_MAX_RESULTS,
)
from disciplines import ALL_DISCIPLINES, get_discipline_from_topics, categorize_authors
from openalex_client import OpenAlexClient, OpenAlexRequestError
from orcid_async import fetch_emails_async
from openai_email_async import AsyncOpenAIEmailClient
from author_filters import author_matches_any_specialty, dedupe_authors
from bulk_email_jobs import prepare_bulk_recipients
from bulk_email_worker import BulkEmailWorker
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
    TEMPLATE_PUBLICATION_RECENT_WORK,
    choose_rotating_template,
    get_publication_template_ids,
)
from pdf_generator import generate_invitation_pdf, PUBLISHER_INFO
from db_client import get_storage, PostgresStorage
from db_client import (
    RUN_STATUS_ACTIVE,
    RUN_STATUS_IDLE,
    RUN_STATUS_PAUSED,
    RUN_STATUS_COOLDOWN,
    RUN_STATUS_RECOVERY,
    RUN_STATUS_STOPPED_TODAY,
    EMAIL_STATUS_FOUND,
    EMAIL_SUPPRESSION_SOURCE_UNSUBSCRIBE,
    BULK_JOB_STATUS_QUEUED,
    BULK_JOB_STATUS_RUNNING,
    BULK_RECIPIENT_STATUS_PENDING,
    BULK_RECIPIENT_STATUS_SENDING,
    BULK_RECIPIENT_STATUS_SENT,
    BULK_RECIPIENT_STATUS_FAILED,
    BULK_RECIPIENT_STATUS_SKIPPED,
)
from journal_presets import normalize_journal_preset_config
from session_store import (
    SessionData,
    SESSION_COOKIE_NAME,
    ensure_session_table,
    create_session,
    get_session_data,
)

WORKFLOW_AUTHOR = "author"
WORKFLOW_EDITORIAL = "editorial"

# ── Global singletons (shared across requests) ──────────────────────
_db_storage: Optional[PostgresStorage] = None
_email_sender: Optional[EmailSender] = None
_bulk_daemon_thread: Optional[threading.Thread] = None


def get_db() -> PostgresStorage:
    global _db_storage
    if _db_storage is None:
        _db_storage = get_storage()
    return _db_storage


def get_email_sender() -> EmailSender:
    global _email_sender
    if _email_sender is None:
        _email_sender = EmailSender()
    return _email_sender


# ── Application lifecycle ──────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    ensure_session_table()
    global _bulk_daemon_thread
    db = get_db()
    if db.available:
        sender = get_email_sender()
        # Start bulk email daemon in background thread
        _start_bulk_daemon(db, sender)
    yield
    # Shutdown (nothing special needed)


def _start_bulk_daemon(db: PostgresStorage, sender: EmailSender):
    """Start the bulk email processing daemon in a background thread."""
    def _daemon_loop():
        worker = BulkEmailWorker(storage=db, email_sender=sender)
        while True:
            try:
                result = worker.process_next()
                if result is None:
                    time.sleep(5)  # idle
                else:
                    time.sleep(2)  # between sends
            except Exception:
                time.sleep(10)  # error backoff

    _bulk_daemon_thread = threading.Thread(target=_daemon_loop, daemon=True)
    _bulk_daemon_thread.start()


app = FastAPI(title="Editorial Board Invitation Tool", lifespan=lifespan)

# Mount static files
app.mount("/static", StaticFiles(directory=str(PARENT_DIR / "v2" / "static")), name="static")

# Also mount shared static assets from parent (logos, pdf templates)
app.mount("/assets", StaticFiles(directory=str(PARENT_DIR)), name="assets")

# Templates
templates = Jinja2Templates(directory=str(PARENT_DIR / "v2" / "templates"))

# Add custom filters to Jinja2
templates.env.filters["json_dumps"] = json.dumps


# ── Session middleware ─────────────────────────────────────────────
def get_or_create_session(request: Request, response: Response) -> SessionData:
    """Get existing session or create a new one."""
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if session_id:
        data = get_session_data(session_id)
        if data is not None:
            return SessionData(session_id, data)

    # Create new session
    session_id = create_session()
    session_data = SessionData(session_id, {})
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_id,
        max_age=86400 * 7,  # 7 days
        httponly=True,
        samesite="lax",
    )
    return session_data


# ── Routes ─────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, response: Response):
    """Main dashboard page."""
    db = get_db()
    sd = get_or_create_session(request, response)

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "session_id": sd.session_id,
        "db_available": db.available,
        "countries": COUNTRIES,
        "disciplines": ALL_DISCIPLINES,
        "h_index_min": sd.search_params.get("h_index_min", DEFAULT_H_INDEX_MIN),
        "h_index_max": sd.search_params.get("h_index_max", DEFAULT_H_INDEX_MAX),
        "max_results": sd.search_params.get("max_results", DEFAULT_MAX_RESULTS),
        "jump_size": sd.search_params.get("jump_size", 250),
        "journal_config": sd.journal_config,
        "publisher": sd.publisher,
    })


@app.post("/session/update")
async def update_session_endpoint(
    request: Request,
    response: Response,
    key: str = Form(...),
    value: str = Form(...),
):
    """Update a session key-value pair."""
    sd = get_or_create_session(request, response)
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        parsed = value
    sd.set(key, parsed)
    sd.save()
    return JSONResponse({"ok": True})


@app.get("/search", response_class=HTMLResponse)
async def search_page(request: Request, response: Response):
    """Search panel with filters."""
    sd = get_or_create_session(request, response)
    return templates.TemplateResponse("partials/search_panel.html", {
        "request": request,
        "countries": COUNTRIES,
        "disciplines": ALL_DISCIPLINES,
        "h_index_min": sd.search_params.get("h_index_min", DEFAULT_H_INDEX_MIN),
        "h_index_max": sd.search_params.get("h_index_max", DEFAULT_H_INDEX_MAX),
        "max_results": sd.search_params.get("max_results", DEFAULT_MAX_RESULTS),
        "jump_size": sd.search_params.get("jump_size", 250),
        "journal_config": sd.journal_config,
        "publisher": sd.publisher,
    })


@app.post("/search/openalex", response_class=HTMLResponse)
async def search_openalex(
    request: Request,
    response: Response,
    keywords: str = Form(""),
    h_index_min: int = Form(DEFAULT_H_INDEX_MIN),
    h_index_max: int = Form(DEFAULT_H_INDEX_MAX),
    countries_exclude: str = Form(""),
    disciplines: str = Form(""),
    max_results: int = Form(DEFAULT_MAX_RESULTS),
    jump_size: int = Form(250),
    batch_offset: int = Form(0),
):
    """Search OpenAlex for authors."""
    sd = get_or_create_session(request, response)
    db = get_db()

    # Parse multi-value form fields
    exclude_countries = [c for c in countries_exclude.split(",") if c] if countries_exclude else []
    selected_disciplines = [d for d in disciplines.split(",") if d] if disciplines else []

    # Resolve keywords to topics
    client = OpenAlexClient()
    try:
        topic_ids = client.search_topics(keywords) if keywords else []
    except OpenAlexRequestError:
        return templates.TemplateResponse("partials/error_message.html", {
            "request": request,
            "message": "Failed to resolve keywords to topics. Check your connection.",
        })

    # Build batch key for caching
    batch_key = f"oa:{','.join(sorted(topic_ids))}:{h_index_min}:{h_index_max}:{jump_size}"

    # Load cached batches or start fresh
    batch_cache = sd.search_batch_cache
    if batch_key not in batch_cache:
        batch_cache[batch_key] = {"batches": [], "checkpoints": {}, "total_estimate": None}

    cache = batch_cache[batch_key]

    # If requesting a batch we don't have yet, fetch it
    if batch_offset >= len(cache["batches"]):
        try:
            authors, next_cursor = client.load_authors_batch(
                topic_ids=topic_ids,
                h_index_min=h_index_min,
                h_index_max=h_index_max,
                cursor=cache["checkpoints"].get(str(batch_offset)),
                limit=jump_size,
            )
            cache["batches"].append(authors)
            if next_cursor:
                cache["checkpoints"][str(batch_offset + 1)] = next_cursor
        except OpenAlexRequestError as e:
            return templates.TemplateResponse("partials/error_message.html", {
                "request": request,
                "message": f"OpenAlex API error: {str(e)}",
            })

    # Enrich authors with discipline
    current_batch = cache["batches"][batch_offset] if batch_offset < len(cache["batches"]) else []
    categorize_authors(current_batch)

    # Apply discipline filter if set
    if selected_disciplines:
        current_batch = [a for a in current_batch if a.get("discipline") in selected_disciplines]

    # Apply country exclusion
    if exclude_countries:
        current_batch = [a for a in current_batch if a.get("country") not in exclude_countries]

    # Hydrate emails from DB
    if db.available:
        current_batch = _hydrate_emails_from_db(db, current_batch)

    # Deduplicate
    current_batch = dedupe_authors(current_batch)

    # Save batch cache
    sd.search_batch_cache = batch_cache
    sd.save()

    total_estimate = cache.get("total_estimate")
    if total_estimate is None and topic_ids:
        try:
            total_estimate = client.count_authors(
                topic_ids=topic_ids,
                h_index_min=h_index_min,
                h_index_max=h_index_max,
            )
            cache["total_estimate"] = total_estimate
            sd.search_batch_cache = batch_cache
            sd.save()
        except OpenAlexRequestError:
            pass

    return templates.TemplateResponse("partials/results_table.html", {
        "request": request,
        "authors": current_batch,
        "batch_offset": batch_offset,
        "total_batches": len(cache["batches"]),
        "total_estimate": total_estimate,
        "journal_config": sd.journal_config,
        "publisher": sd.publisher,
        "db_available": db.available,
        "db": db,
    })


@app.post("/search/database", response_class=HTMLResponse)
async def search_database(
    request: Request,
    response: Response,
    query: str = Form(""),
    source: str = Form("all"),
    with_email: bool = Form(False),
    hide_suppressed: bool = Form(True),
    hide_sent: bool = Form(True),
):
    """Search database for previously collected emails."""
    sd = get_or_create_session(request, response)
    db = get_db()

    if not db.available:
        return templates.TemplateResponse("partials/error_message.html", {
            "request": request,
            "message": "Database not available.",
        })

    authors = _search_database_email_rows(db, query, source, with_email, hide_suppressed, hide_sent)
    categorize_authors(authors)
    authors = dedupe_authors(authors)

    # Store in session for display
    sd.search_results = authors
    sd.save()

    return templates.TemplateResponse("partials/results_table.html", {
        "request": request,
        "authors": authors,
        "batch_offset": 0,
        "total_batches": 1,
        "total_estimate": len(authors),
        "journal_config": sd.journal_config,
        "publisher": sd.publisher,
        "db_available": True,
        "db": db,
    })


@app.post("/search/both", response_class=HTMLResponse)
async def search_both(
    request: Request,
    response: Response,
    keywords: str = Form(""),
    h_index_min: int = Form(DEFAULT_H_INDEX_MIN),
    h_index_max: int = Form(DEFAULT_H_INDEX_MAX),
    countries_exclude: str = Form(""),
    disciplines: str = Form(""),
    max_results: int = Form(DEFAULT_MAX_RESULTS),
    jump_size: int = Form(250),
    batch_offset: int = Form(0),
):
    """Search both OpenAlex and Database, merge results."""
    sd = get_or_create_session(request, response)
    db = get_db()

    # Get OpenAlex results
    oa_authors = []
    exclude_countries = [c for c in countries_exclude.split(",") if c] if countries_exclude else []
    selected_disciplines = [d for d in disciplines.split(",") if d] if disciplines else []

    client = OpenAlexClient()
    try:
        topic_ids = client.search_topics(keywords) if keywords else []
        if topic_ids:
            authors, _ = client.load_authors_batch(
                topic_ids=topic_ids,
                h_index_min=h_index_min,
                h_index_max=h_index_max,
                limit=jump_size,
            )
            categorize_authors(authors)
            oa_authors = authors
    except OpenAlexRequestError:
        pass

    # Get database results
    db_authors = []
    if db.available:
        db_authors = _search_database_email_rows(db, "", "all", True, True, True)
        categorize_authors(db_authors)

    # Merge by ORCID/email/OpenAlex ID
    merged = _merge_author_sources(oa_authors, db_authors)

    # Apply filters
    if selected_disciplines:
        merged = [a for a in merged if a.get("discipline") in selected_disciplines]
    if exclude_countries:
        merged = [a for a in merged if a.get("country") not in exclude_countries]

    merged = dedupe_authors(merged)

    sd.search_results = merged
    sd.save()

    return templates.TemplateResponse("partials/results_table.html", {
        "request": request,
        "authors": merged,
        "batch_offset": 0,
        "total_batches": 1,
        "total_estimate": len(merged),
        "journal_config": sd.journal_config,
        "publisher": sd.publisher,
        "db_available": db.available,
        "db": db,
    })


@app.get("/results/{batch_offset}", response_class=HTMLResponse)
async def get_results_batch(
    request: Request,
    response: Response,
    batch_offset: int,
    invitation_type: str = "author",
):
    """Get a specific batch of results."""
    sd = get_or_create_session(request, response)
    db = get_db()

    batch_cache = sd.search_batch_cache
    # Find the active batch key (last one used)
    batch_key = None
    for key in batch_cache:
        if key.startswith("oa:"):
            batch_key = key

    if not batch_key or batch_key not in batch_cache:
        return templates.TemplateResponse("partials/error_message.html", {
            "request": request,
            "message": "No cached results. Please run a search first.",
        })

    cache = batch_cache[batch_key]
    if batch_offset >= len(cache["batches"]):
        return templates.TemplateResponse("partials/error_message.html", {
            "request": request,
            "message": "Batch not available yet. Use 'Next' to load more.",
        })

    authors = cache["batches"][batch_offset]
    categorize_authors(authors)
    authors = dedupe_authors(authors)

    if db.available:
        authors = _hydrate_emails_from_db(db, authors)

    return templates.TemplateResponse("partials/results_table.html", {
        "request": request,
        "authors": authors,
        "batch_offset": batch_offset,
        "total_batches": len(cache["batches"]),
        "total_estimate": cache.get("total_estimate"),
        "journal_config": sd.journal_config,
        "publisher": sd.publisher,
        "db_available": db.available,
        "db": db,
        "invitation_type": invitation_type,
    })


@app.get("/author/{orcid_id}/invite", response_class=HTMLResponse)
async def author_invite_dialog(
    request: Request,
    response: Response,
    orcid_id: str,
    invitation_type: str = "author",
):
    """Show the invitation email composition dialog."""
    sd = get_or_create_session(request, response)
    db = get_db()

    # Find author in search results
    author = _find_author_in_session(sd, orcid_id)
    if not author:
        return templates.TemplateResponse("partials/error_message.html", {
            "request": request,
            "message": "Author not found in current results.",
        })

    # Check suppression status
    is_suppressed = False
    if db.available:
        is_suppressed = db.is_recipient_suppressed(
            author.get("email", ""),
            orcid_id=author.get("orcid_id", ""),
        )

    # Check already invited
    already_invited = False
    if db.available and author.get("orcid_id"):
        inv_type = INVITATION_TYPE_PUBLICATION if invitation_type == WORKFLOW_AUTHOR else INVITATION_TYPE_EDITORIAL
        journal_name = sd.journal_config.get("name", "") if inv_type == INVITATION_TYPE_PUBLICATION else None
        already_invited = db.is_sent(author["orcid_id"], inv_type, journal_name)

    # Get recent publications (for publication invitations)
    recent_pubs = []
    if invitation_type == WORKFLOW_AUTHOR and author.get("openalex_id"):
        recent_pubs = sd.recent_publications_cache.get(orcid_id, [])
        if not recent_pubs and db.available:
            try:
                client = OpenAlexClient()
                works = client.get_recent_works(author["openalex_id"], limit=3)
                recent_pubs = works
                cache = sd.recent_publications_cache
                cache[orcid_id] = works
                sd.recent_publications_cache = cache
                sd.save()
            except Exception:
                pass

    # Format template
    template_names = get_template_names(
        INVITATION_TYPE_PUBLICATION if invitation_type == WORKFLOW_AUTHOR else INVITATION_TYPE_EDITORIAL
    )
    first_template_id = list(template_names.keys())[0]

    pub_format = format_recent_publications(recent_pubs) if recent_pubs else ""
    formatted = format_template(
        template_id=first_template_id,
        author_name=author.get("name", author.get("author_name", "")),
        journal_name=sd.journal_config.get("name", ""),
        journal_issn=sd.journal_config.get("issn", ""),
        journal_link=sd.journal_config.get("link", ""),
        editor_in_chief_name=sd.journal_config.get("editor_in_chief", ""),
        publisher_name=PUBLISHER_INFO.get(sd.publisher, {}).get("name", ""),
        sender_email=PUBLISHER_INFO.get(sd.publisher, {}).get("email", ""),
        publisher_location=PUBLISHER_INFO.get(sd.publisher, {}).get("location", ""),
        journal_submission_link=sd.journal_config.get("submission_link", ""),
        journal_cite_score=sd.journal_config.get("cite_score", ""),
        journal_quartile=sd.journal_config.get("quartile", ""),
        journal_indexing_status=sd.journal_config.get("indexing_status", ""),
        author_specialty=author.get("specialty", author.get("research_area", "")),
        author_recent_publications=pub_format,
        journal_scope=sd.journal_config.get("scope", ""),
        invitation_goal=sd.journal_config.get("invitation_goal", ""),
    )

    invitation_type_value = (
        INVITATION_TYPE_PUBLICATION if invitation_type == WORKFLOW_AUTHOR else INVITATION_TYPE_EDITORIAL
    )

    return templates.TemplateResponse("partials/invite_dialog.html", {
        "request": request,
        "author": author,
        "invitation_type": invitation_type_value,
        "is_suppressed": is_suppressed,
        "already_invited": already_invited,
        "recent_publications": recent_pubs,
        "template_names": template_names,
        "subject": formatted["subject"],
        "body": formatted["body"],
        "journal_config": sd.journal_config,
        "publisher": sd.publisher,
        "publisher_info": PUBLISHER_INFO.get(sd.publisher, {}),
    })


@app.post("/author/{orcid_id}/send")
async def send_invitation(
    request: Request,
    response: Response,
    orcid_id: str,
    invitation_type: str = Form("publication"),
    template_id: str = Form(""),
    to_email: str = Form(""),
    subject: str = Form(""),
    body: str = Form(""),
    attach_pdf: bool = Form(False),
    scopus_indexed: bool = Form(False),
    mention_publications: bool = Form(False),
    confirm_resend: bool = Form(False),
):
    """Send an invitation email."""
    sd = get_or_create_session(request, response)
    db = get_db()
    sender = get_email_sender()

    author = _find_author_in_session(sd, orcid_id)
    if not author:
        return JSONResponse({"ok": False, "error": "Author not found"}, status_code=404)

    email = to_email or author.get("email", "")
    if not email or "@" not in email:
        return JSONResponse({"ok": False, "error": "Invalid email address"}, status_code=400)

    # Check suppression
    if db.available:
        if db.is_recipient_suppressed(email, orcid_id=orcid_id):
            return JSONResponse({"ok": False, "error": "This recipient is suppressed"}, status_code=400)

        # Check already invited
        journal_name = sd.journal_config.get("name", "") if invitation_type == INVITATION_TYPE_PUBLICATION else None
        if db.is_sent(orcid_id, invitation_type, journal_name):
            if not confirm_resend:
                return JSONResponse({"ok": False, "error": "Already invited. Check 'Confirm resend' to send again."}, status_code=409)

    # Format the email
    pub_format = ""
    if mention_publications and invitation_type == INVITATION_TYPE_PUBLICATION and author.get("openalex_id"):
        try:
            client = OpenAlexClient()
            works = client.get_recent_works(author["openalex_id"], limit=3)
            pub_format = format_recent_publications(works)
        except Exception:
            pass

    formatted = format_template(
        template_id=template_id,
        author_name=author.get("name", author.get("author_name", "")),
        journal_name=sd.journal_config.get("name", ""),
        journal_issn=sd.journal_config.get("issn", ""),
        journal_link=sd.journal_config.get("link", ""),
        editor_in_chief_name=sd.journal_config.get("editor_in_chief", ""),
        publisher_name=PUBLISHER_INFO.get(sd.publisher, {}).get("name", ""),
        sender_email=PUBLISHER_INFO.get(sd.publisher, {}).get("email", ""),
        publisher_location=PUBLISHER_INFO.get(sd.publisher, {}).get("location", ""),
        scopus_indexed=scopus_indexed,
        journal_submission_link=sd.journal_config.get("submission_link", ""),
        journal_cite_score=sd.journal_config.get("cite_score", ""),
        journal_quartile=sd.journal_config.get("quartile", ""),
        journal_indexing_status=sd.journal_config.get("indexing_status", ""),
        author_specialty=author.get("specialty", author.get("research_area", "")),
        author_recent_publications=pub_format,
        journal_scope=sd.journal_config.get("scope", ""),
        invitation_goal=sd.journal_config.get("invitation_goal", ""),
    )

    # Use user-provided subject/body if different from template
    final_subject = subject if subject != formatted["subject"] else formatted["subject"]
    final_body = body if body != formatted["body"] else formatted["body"]

    # Generate PDF if requested
    pdf_bytes = None
    if attach_pdf:
        pdf_bytes = generate_invitation_pdf(
            publisher_id=sd.publisher,
            recipient_name=author.get("name", author.get("author_name", "")),
            email_body=final_body,
            subject=final_subject,
            journal_name=sd.journal_config.get("name", ""),
            journal_link=sd.journal_config.get("link", ""),
        )

    # Send email
    result = sender.send_email(
        to_email=email,
        to_name=author.get("name", author.get("author_name", "")),
        subject=final_subject,
        body_html=sender._build_premium_html_email(
            subject=final_subject,
            body=final_body,
            publisher_id=sd.publisher,
            invitation_type=invitation_type,
            scopus_indexed=scopus_indexed,
        ),
        body_text=final_body,
        pdf_attachment=pdf_bytes,
    )

    if result.get("ok"):
        # Record invitation in database
        if db.available:
            db.mark_sent(
                orcid_id=orcid_id,
                author_name=author.get("name", author.get("author_name", "")),
                email=email,
                publisher=sd.publisher,
                invitation_type=invitation_type,
                journal_name=sd.journal_config.get("name", "") if invitation_type == INVITATION_TYPE_PUBLICATION else "",
            )

        return JSONResponse({"ok": True, "message": "Invitation sent successfully"})

    return JSONResponse({"ok": False, "error": result.get("error", "Failed to send email")}, status_code=500)


@app.post("/author/{orcid_id}/pdf")
async def get_author_pdf(
    request: Request,
    response: Response,
    orcid_id: str,
    invitation_type: str = Form("publication"),
    template_id: str = Form(""),
    subject: str = Form(""),
    body: str = Form(""),
):
    """Generate and return PDF for an author."""
    sd = get_or_create_session(request, response)

    author = _find_author_in_session(sd, orcid_id)
    if not author:
        return JSONResponse({"ok": False, "error": "Author not found"}, status_code=404)

    pdf_bytes = generate_invitation_pdf(
        publisher_id=sd.publisher,
        recipient_name=author.get("name", author.get("author_name", "")),
        email_body=body,
        subject=subject,
        journal_name=sd.journal_config.get("name", ""),
        journal_link=sd.journal_config.get("link", ""),
    )

    return Response(content=pdf_bytes, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="invitation_{orcid_id}.pdf"'})


@app.post("/bulk/send", response_class=HTMLResponse)
async def bulk_send_preview(
    request: Request,
    response: Response,
    invitation_type: str = Form("publication"),
    orcid_ids: str = Form(""),  # comma-separated
):
    """Show bulk send preview and confirmation."""
    sd = get_or_create_session(request, response)
    db = get_db()

    ids = [i.strip() for i in orcid_ids.split(",") if i.strip()]
    authors = [_find_author_in_session(sd, oid) for oid in ids]
    authors = [a for a in authors if a]

    if not authors:
        return templates.TemplateResponse("partials/error_message.html", {
            "request": request,
            "message": "No valid authors selected.",
        })

    # Prepare recipients (filter suppressed, already sent, retracted)
    def is_already_sent(tracking_id: str) -> bool:
        if not db.available:
            return False
        journal_name = sd.journal_config.get("name", "") if invitation_type == INVITATION_TYPE_PUBLICATION else None
        return db.is_sent(tracking_id, invitation_type, journal_name)

    def is_suppressed(email: str, orcid_id: str) -> bool:
        if not db.available:
            return False
        return db.is_recipient_suppressed(email, orcid_id=orcid_id)

    retracted_names = set()
    if db.available:
        with db._get_cursor() as cur:
            cur.execute("SELECT DISTINCT LOWER(author_name) FROM retracted_authors")
            for row in cur.fetchall():
                retracted_names.add(row[0].lower())

    recipients = prepare_bulk_recipients(authors, is_already_sent, is_suppressed, retracted_names)

    # Get template options
    template_names = get_template_names(invitation_type)

    return templates.TemplateResponse("partials/bulk_preview.html", {
        "request": request,
        "recipients": recipients,
        "total_count": len(authors),
        "eligible_count": len(recipients),
        "filtered_count": len(authors) - len(recipients),
        "template_names": template_names,
        "invitation_type": invitation_type,
        "journal_config": sd.journal_config,
    })


@app.post("/bulk/queue")
async def queue_bulk_send(
    request: Request,
    response: Response,
    invitation_type: str = Form("publication"),
    template_id: str = Form(""),
    scopus_indexed: bool = Form(False),
    attach_pdf: bool = Form(False),
    orcid_ids: str = Form(""),
):
    """Queue a bulk email job."""
    sd = get_or_create_session(request, response)
    db = get_db()

    ids = [i.strip() for i in orcid_ids.split(",") if i.strip()]
    authors = [_find_author_in_session(sd, oid) for oid in ids]
    authors = [a for a in authors if a]

    if not authors:
        return JSONResponse({"ok": False, "error": "No valid authors selected"}, status_code=400)

    # Prepare recipients
    def is_already_sent(tracking_id: str) -> bool:
        if not db.available:
            return False
        journal_name = sd.journal_config.get("name", "") if invitation_type == INVITATION_TYPE_PUBLICATION else None
        return db.is_sent(tracking_id, invitation_type, journal_name)

    def is_suppressed(email: str, orcid_id: str) -> bool:
        if not db.available:
            return False
        return db.is_recipient_suppressed(email, orcid_id=orcid_id)

    retracted_names = set()
    if db.available:
        with db._get_cursor() as cur:
            cur.execute("SELECT DISTINCT LOWER(author_name) FROM retracted_authors")
            for row in cur.fetchall():
                retracted_names.add(row[0].lower())

    recipients = prepare_bulk_recipients(authors, is_already_sent, is_suppressed, retracted_names)

    if not recipients:
        return JSONResponse({"ok": False, "error": "No eligible recipients after filtering"}, status_code=400)

    # Create bulk job
    include_publications = invitation_type == INVITATION_TYPE_PUBLICATION
    job_id = db.create_bulk_email_job(
        recipients=recipients,
        invitation_type=invitation_type,
        publisher_id=sd.publisher,
        journal_name=sd.journal_config.get("name", ""),
        template_id=template_id or list(get_template_names(invitation_type).keys())[0],
        template_strategy="fixed" if invitation_type == INVITATION_TYPE_EDITORIAL else "rotate",
        scopus_indexed=scopus_indexed,
        attach_pdf=attach_pdf,
        include_publications=include_publications,
        journal_config=sd.journal_config,
    )

    return JSONResponse({"ok": True, "job_id": job_id, "queued_count": len(recipients)})


@app.get("/collection", response_class=HTMLResponse)
async def collection_panel(request: Request, response: Response):
    """Collection worker control panel."""
    sd = get_or_create_session(request, response)
    db = get_db()

    run_status = {"status": "unknown", "collected_today": 0, "attempts_today": 0,
                  "queue_pending": 0, "total_collected": 0}

    if db.available:
        with db._get_cursor() as cur:
            cur.execute("""
                SELECT status, config
                FROM collection_runs
                LIMIT 1
            """)
            row = cur.fetchone()
            if row:
                run_status["status"] = row[0] or "idle"
                config = row[1] or {}
                if isinstance(config, str):
                    try:
                        config = json.loads(config)
                    except json.JSONDecodeError:
                        config = {}
                run_status["config"] = config

            cur.execute("SELECT COUNT(*) FROM harvested_authors WHERE email_status = 'found'")
            r = cur.fetchone()
            run_status["total_collected"] = r[0] if r else 0

            cur.execute("SELECT COUNT(*) FROM harvested_authors WHERE email_status = 'pending'")
            r = cur.fetchone()
            run_status["queue_pending"] = r[0] if r else 0

    return templates.TemplateResponse("collection.html", {
        "request": request,
        "run_status": run_status,
        "countries": COUNTRIES,
        "disciplines": ALL_DISCIPLINES,
    })


@app.post("/collection/start")
async def collection_start(request: Request, response: Response):
    """Signal collection worker to start."""
    sd = get_or_create_session(request, response)
    db = get_db()

    if db.available:
        with db._get_cursor() as cur:
            cur.execute("UPDATE collection_runs SET status = %s", (RUN_STATUS_ACTIVE,))
            if cur.rowcount == 0:
                cur.execute("INSERT INTO collection_runs (status, config) VALUES (%s, %s)",
                           (RUN_STATUS_ACTIVE, json.dumps({})))

    return JSONResponse({"ok": True, "message": "Collection started"})


@app.post("/collection/pause")
async def collection_pause(request: Request, response: Response):
    """Pause collection worker."""
    sd = get_or_create_session(request, response)
    db = get_db()

    if db.available:
        with db._get_cursor() as cur:
            cur.execute("UPDATE collection_runs SET status = %s", (RUN_STATUS_PAUSED,))

    return JSONResponse({"ok": True, "message": "Collection paused"})


@app.post("/collection/stop")
async def collection_stop(request: Request, response: Response):
    """Stop collection worker for today."""
    sd = get_or_create_session(request, response)
    db = get_db()

    if db.available:
        with db._get_cursor() as cur:
            cur.execute("UPDATE collection_runs SET status = %s", (RUN_STATUS_STOPPED_TODAY,))

    return JSONResponse({"ok": True, "message": "Collection stopped for today"})


@app.post("/collection/config")
async def collection_save_config(
    request: Request,
    response: Response,
    keywords: str = Form(""),
    disciplines: str = Form(""),
    specialty_terms: str = Form(""),
    h_index_min: int = Form(DEFAULT_H_INDEX_MIN),
    h_index_max: int = Form(DEFAULT_H_INDEX_MAX),
    exclude_countries: str = Form(""),
    baseline_concurrency: int = Form(2),
    baseline_delay: float = Form(3.0),
):
    """Save collection worker configuration."""
    sd = get_or_create_session(request, response)
    db = get_db()

    config = {
        "keywords": keywords,
        "disciplines": [d for d in disciplines.split(",") if d],
        "specialty_terms": [s.strip() for s in specialty_terms.split(",") if s.strip()],
        "h_index_min": h_index_min,
        "h_index_max": h_index_max,
        "exclude_countries": [c for c in exclude_countries.split(",") if c],
        "baseline_concurrency": baseline_concurrency,
        "baseline_delay_sec": baseline_delay,
    }

    if db.available:
        with db._get_cursor() as cur:
            cur.execute("SELECT id FROM collection_runs LIMIT 1")
            row = cur.fetchone()
            if row:
                cur.execute("UPDATE collection_runs SET config = %s WHERE id = %s",
                           (json.dumps(config), row[0]))
            else:
                cur.execute("INSERT INTO collection_runs (status, config) VALUES (%s, %s)",
                           (RUN_STATUS_IDLE, json.dumps(config)))

    return JSONResponse({"ok": True, "message": "Collection config saved"})


@app.get("/history", response_class=HTMLResponse)
async def invitation_history(request: Request, response: Response):
    """Show invitation history."""
    sd = get_or_create_session(request, response)
    db = get_db()

    invitations = []
    if db.available:
        with db._get_cursor() as cur:
            cur.execute("""
                SELECT orcid_id, email, author_name, invitation_type, journal_name, sent_at
                FROM author_invitations
                ORDER BY sent_at DESC
                LIMIT 200
            """)
            for row in cur.fetchall():
                invitations.append({
                    "orcid_id": row[0],
                    "email": row[1],
                    "author_name": row[2],
                    "invitation_type": row[3],
                    "journal_name": row[4],
                    "sent_at": str(row[5]) if row[5] else "",
                })

    return templates.TemplateResponse("history.html", {
        "request": request,
        "invitations": invitations,
    })


@app.get("/jobs", response_class=HTMLResponse)
async def bulk_job_status(request: Request, response: Response):
    """Show bulk email job status."""
    sd = get_or_create_session(request, response)
    db = get_db()

    jobs = []
    if db.available:
        with db._get_cursor() as cur:
            cur.execute("""
                SELECT id, status, publisher_id, journal_name, invitation_type,
                       total_count, sent_count, failed_count, skipped_count,
                       created_at, updated_at, last_error
                FROM bulk_email_jobs
                ORDER BY created_at DESC
                LIMIT 10
            """)
            for row in cur.fetchall():
                jobs.append({
                    "id": row[0],
                    "status": row[1],
                    "publisher_id": row[2],
                    "journal_name": row[3],
                    "invitation_type": row[4],
                    "total_count": row[5],
                    "sent_count": row[6],
                    "failed_count": row[7],
                    "skipped_count": row[8],
                    "created_at": str(row[9]) if row[9] else "",
                    "updated_at": str(row[10]) if row[10] else "",
                    "last_error": row[11],
                })

    return templates.TemplateResponse("partials/job_status.html", {
        "request": request,
        "jobs": jobs,
    })


@app.get("/unsubscribe")
async def handle_unsubscribe(request: Request, response: Response, token: str = ""):
    """Handle unsubscribe link click."""
    db = get_db()

    if not token or not db.available:
        return templates.TemplateResponse("unsubscribe_result.html", {
            "request": request,
            "success": False,
            "message": "Invalid unsubscribe link.",
        })

    record = db.get_email_suppression_by_token(token)
    if not record:
        return templates.TemplateResponse("unsubscribe_result.html", {
            "request": request,
            "success": False,
            "message": "This unsubscribe link is invalid or has expired.",
        })

    result = db.suppress_recipient(
        record.get("email_lower", ""),
        orcid_id=record.get("orcid_id", ""),
        profile_key=record.get("profile_key", ""),
        reason="Unsubscribed via email link",
        source=EMAIL_SUPPRESSION_SOURCE_UNSUBSCRIBE,
    )

    if result:
        return templates.TemplateResponse("unsubscribe_result.html", {
            "request": request,
            "success": True,
            "message": "You have been unsubscribed successfully. No further emails will be sent.",
        })

    return templates.TemplateResponse("unsubscribe_result.html", {
        "request": request,
        "success": False,
        "message": "Unable to process unsubscribe request. Please contact us directly.",
    })


@app.post("/unsubscribe/manual")
async def manual_unsubscribe(
    request: Request,
    response: Response,
    email_text: str = Form(...),
):
    """Manual unsubscribe tool."""
    import re
    sd = get_or_create_session(request, response)
    db = get_db()

    # Extract email from text
    match = re.search(r'[\w\.-]+@[\w\.-]+\.\w+', email_text)
    if not match:
        return JSONResponse({"ok": False, "error": "No email address found in text"}, status_code=400)

    email = match.group(0)

    if db.available:
        result = db.suppress_recipient(
            email=email,
            reason="Manual unsubscribe via app",
            source="manual_unsubscribe",
        )
        if result:
            return JSONResponse({"ok": True, "message": f"{email} has been suppressed and purged"})

    return JSONResponse({"ok": False, "error": "Failed to suppress recipient"}, status_code=500)


@app.post("/presets/save")
async def save_preset(
    request: Request,
    response: Response,
    name: str = Form(...),
    publisher_id: str = Form("brevo"),
    journal_config_json: str = Form("{}"),
):
    """Save a journal preset."""
    sd = get_or_create_session(request, response)
    db = get_db()

    if not db.available:
        return JSONResponse({"ok": False, "error": "Database not available"}, status_code=500)

    config = normalize_journal_preset_config(json.loads(journal_config_json))
    preset_id = db.create_journal_preset(name=name, publisher_id=publisher_id, journal_config=config)

    return JSONResponse({"ok": True, "preset_id": preset_id})


@app.post("/presets/load/{preset_id}")
async def load_preset(request: Request, response: Response, preset_id: int):
    """Load a journal preset into session."""
    sd = get_or_create_session(request, response)
    db = get_db()

    if not db.available:
        return JSONResponse({"ok": False, "error": "Database not available"}, status_code=500)

    preset = db.get_journal_preset(preset_id)
    if preset:
        sd.journal_config = preset.get("journal_config", sd.journal_config)
        sd.publisher = preset.get("publisher_id", sd.publisher)
        sd.save()
        return JSONResponse({"ok": True, "config": sd.journal_config})

    return JSONResponse({"ok": False, "error": "Preset not found"}, status_code=404)


@app.get("/presets/list", response_class=HTMLResponse)
async def list_presets(request: Request, response: Response):
    """List saved journal presets."""
    sd = get_or_create_session(request, response)
    db = get_db()

    presets = []
    if db.available:
        presets = db.list_journal_presets()

    return templates.TemplateResponse("partials/preset_list.html", {
        "request": request,
        "presets": presets,
    })


@app.post("/publisher/select")
async def select_publisher(
    request: Request,
    response: Response,
    publisher_id: str = Form(...),
):
    """Select active publisher."""
    sd = get_or_create_session(request, response)
    sd.publisher = publisher_id
    sd.save()
    return JSONResponse({"ok": True})


@app.post("/journal/config")
async def update_journal_config(
    request: Request,
    response: Response,
    name: str = Form(""),
    issn: str = Form(""),
    link: str = Form(""),
    location: str = Form(""),
    editor_in_chief: str = Form(""),
    submission_link: str = Form(""),
    cite_score: str = Form(""),
    quartile: str = Form(""),
    indexing_status: str = Form(""),
    invitation_goal: str = Form("Regular submission"),
    scope: str = Form(""),
):
    """Update journal configuration in session."""
    sd = get_or_create_session(request, response)
    sd.journal_config = {
        "name": name,
        "issn": issn,
        "link": link,
        "location": location,
        "editor_in_chief": editor_in_chief,
        "submission_link": submission_link,
        "cite_score": cite_score,
        "quartile": quartile,
        "indexing_status": indexing_status,
        "invitation_goal": invitation_goal,
        "scope": scope,
    }
    sd.save()
    return JSONResponse({"ok": True})


@app.post("/export/csv")
async def export_csv(
    request: Request,
    response: Response,
    with_email: bool = Form(False),
):
    """Export search results as CSV."""
    import csv
    import io
    from fastapi.responses import StreamingResponse

    sd = get_or_create_session(request, response)
    db = get_db()
    authors = sd.search_results

    if not authors:
        return JSONResponse({"ok": False, "error": "No results to export"}, status_code=400)

    # Add invitation status
    sent_orcids = set()
    if db.available:
        with db._get_cursor() as cur:
            cur.execute("SELECT DISTINCT orcid_id FROM author_invitations")
            for row in cur.fetchall():
                sent_orcids.add(row[0])

    output = io.StringIO()
    fieldnames = ["name", "h_index", "specialty", "discipline", "country",
                  "institution", "openalex_id", "orcid_id", "invited_count", "status"]
    if with_email:
        fieldnames.insert(4, "email")
        fieldnames.insert(5, "all_emails")

    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()

    for author in authors:
        row = {
            "name": author.get("name", author.get("author_name", "")),
            "h_index": author.get("h_index", ""),
            "specialty": author.get("specialty", author.get("research_area", "")),
            "discipline": author.get("discipline", ""),
            "country": author.get("country", ""),
            "institution": author.get("institution", author.get("affiliation", "")),
            "openalex_id": author.get("openalex_id", ""),
            "orcid_id": author.get("orcid_id", ""),
            "invited_count": author.get("invited_count", 0),
            "status": "invited" if author.get("orcid_id") in sent_orcids else "not_invited",
        }
        if with_email:
            row["email"] = author.get("email", "")
            row["all_emails"] = ";".join(author.get("all_emails", []))
        writer.writerow(row)

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="authors_export.csv"'},
    )




@app.post("/reset/all")
async def reset_all(request: Request, response: Response):
    """Reset all session data."""
    sd = get_or_create_session(request, response)
    sd._data.clear()
    sd.save()
    return JSONResponse({"ok": True})


# ── Helper functions ───────────────────────────────────────────────

def _find_author_in_session(sd: SessionData, orcid_id: str) -> Optional[dict]:
    """Find an author by ORCID ID in session search results or batch cache."""
    # Search in current search_results
    for author in sd.search_results:
        if author.get("orcid_id") == orcid_id:
            return author

    # Search in batch cache
    for batch_key, batch_data in sd.search_batch_cache.items():
        for batch in batch_data.get("batches", []):
            for author in batch:
                if author.get("orcid_id") == orcid_id:
                    return author

    # Try to fetch from DB
    db = get_db()
    if db.available:
        with db._get_cursor() as cur:
            cur.execute("""
                SELECT orcid_id, author_name, email, openalex_id, scientific_domain,
                       scientific_domains_json, match_status, match_confidence
                FROM author_profiles
                WHERE orcid_id = %s
                LIMIT 1
            """, (orcid_id,))
            row = cur.fetchone()
            if row:
                return {
                    "orcid_id": row[0],
                    "name": row[1],
                    "email": row[2],
                    "openalex_id": row[3],
                    "scientific_domain": row[4],
                }

    return None


def _hydrate_emails_from_db(db: PostgresStorage, authors: list) -> list:
    """Fill missing emails and domains from database profiles."""
    if not authors:
        return authors

    orcids = [a.get("orcid_id") for a in authors if a.get("orcid_id")]
    if not orcids:
        return authors

    with db._get_cursor() as cur:
        placeholders = ",".join(["%s"] * len(orcids))
        cur.execute(f"""
            SELECT orcid_id, email, scientific_domain, scientific_domains_json
            FROM author_profiles
            WHERE orcid_id IN ({placeholders})
        """, orcids)

        profile_map = {}
        for row in cur.fetchall():
            profile_map[row[0]] = {
                "email": row[1],
                "scientific_domain": row[2],
                "scientific_domains_json": row[3],
            }

    for author in authors:
        orcid = author.get("orcid_id")
        if orcid and orcid in profile_map:
            profile = profile_map[orcid]
            if not author.get("email") and profile.get("email"):
                author["email"] = profile["email"]
            if not author.get("scientific_domain") and profile.get("scientific_domain"):
                author["scientific_domain"] = profile["scientific_domain"]

    return authors


def _search_database_email_rows(
    db: PostgresStorage,
    query: str,
    source: str,
    with_email: bool,
    hide_suppressed: bool,
    hide_sent: bool,
) -> list:
    """Search database email records (author_profiles, harvested_authors, sent_invitations)."""
    authors = []

    with db._get_cursor() as cur:
        if source in ("all", "profiles"):
            sql = """
                SELECT orcid_id, author_name, email, openalex_id, scientific_domain,
                       scientific_domains_json, affiliation, country, h_index
                FROM author_profiles
                WHERE email IS NOT NULL AND email <> ''
            """
            params = []
            if query:
                sql += """ AND (
                    LOWER(author_name) LIKE LOWER(%s) OR
                    LOWER(email) LIKE LOWER(%s) OR
                    LOWER(affiliation) LIKE LOWER(%s) OR
                    LOWER(openalex_id) LIKE LOWER(%s) OR
                    LOWER(orcid_id) LIKE LOWER(%s)
                )"""
                like_query = f"%{query}%"
                params = [like_query] * 5

            cur.execute(sql, params)
            for row in cur.fetchall():
                authors.append({
                    "orcid_id": row[0],
                    "name": row[1],
                    "email": row[2],
                    "openalex_id": row[3],
                    "scientific_domain": row[4],
                    "affiliation": row[6],
                    "country": row[7],
                    "h_index": row[8],
                    "source": "database",
                })

        if source in ("all", "harvested"):
            sql = """
                SELECT orcid_id, author_name, email, openalex_id, affiliation, country
                FROM harvested_authors
                WHERE email_status = 'found' AND email IS NOT NULL AND email <> ''
            """
            params = []
            if query:
                sql += """ AND (
                    LOWER(author_name) LIKE LOWER(%s) OR
                    LOWER(email) LIKE LOWER(%s)
                )"""
                like_query = f"%{query}%"
                params = [like_query] * 5

            cur.execute(sql, params)
            for row in cur.fetchall():
                authors.append({
                    "orcid_id": row[0],
                    "name": row[1],
                    "email": row[2],
                    "openalex_id": row[3],
                    "affiliation": row[4],
                    "country": row[5],
                    "source": "collected",
                })

    # Filter suppressed
    if hide_suppressed:
        authors = [a for a in authors if not db.is_email_suppressed(a.get("email", ""))]

    # Filter already sent
    if hide_sent:
        sent_orcids = set()
        with db._get_cursor() as cur:
            cur.execute("SELECT DISTINCT orcid_id FROM author_invitations")
            for row in cur.fetchall():
                sent_orcids.add(row[0])
        authors = [a for a in authors if a.get("orcid_id") not in sent_orcids]

    return authors


def _merge_author_sources(oa_authors: list, db_authors: list) -> list:
    """Merge OpenAlex and database author results, preferring DB emails."""
    merged = list(oa_authors)

    # Index merged by ORCID and email
    by_orcid = {}
    by_email = {}
    for i, author in enumerate(merged):
        if author.get("orcid_id"):
            by_orcid[author["orcid_id"]] = i
        if author.get("email"):
            by_email[author["email"].lower()] = i

    for db_author in db_authors:
        orcid = db_author.get("orcid_id")
        email = (db_author.get("email") or "").lower()

        if orcid and orcid in by_orcid:
            # Merge: prefer DB email
            idx = by_orcid[orcid]
            if not merged[idx].get("email") and db_author.get("email"):
                merged[idx]["email"] = db_author["email"]
            merged[idx]["source"] = "both"
        elif email and email in by_email:
            idx = by_email[email]
            merged[idx]["source"] = "both"
        else:
            merged.append(db_author)

    return merged
