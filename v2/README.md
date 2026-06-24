# Editorial Board Invitation Tool v2

FastAPI + HTMX replacement for the Streamlit-based editorial board invitation tool.

## Architecture

- **Backend:** FastAPI (Python 3.11+)
- **Frontend:** HTMX (dynamic updates) + Alpine.js (interactivity) + Jinja2 templates
- **Session:** Cookie-based session ID with server-side state in PostgreSQL `user_sessions` table
- **Database:** Shared PostgreSQL (same schema as v1, no migrations needed)

## Feature Parity with v1

| Feature | v1 (Streamlit) | v2 (FastAPI+HTMX) |
|---|---|---|
| OpenAlex author search | ✅ | ✅ |
| Database email search | ✅ | ✅ |
| Combined search (OpenAlex + DB) | ✅ | ✅ |
| H-index, country, discipline filters | ✅ | ✅ |
| Text search filter on results | ✅ | ✅ |
| Results table with pagination | ✅ | ✅ |
| Author selection checkboxes | ✅ | ✅ |
| Individual invitation send dialog | ✅ | ✅ |
| Email template selection | ✅ | ✅ |
| Publication templates (3 types) | ✅ | ✅ |
| Editorial templates (3 + Scopus variants) | ✅ | ✅ |
| Recent OpenAlex publications | ✅ | ✅ |
| PDF invitation letter generation | ✅ | ✅ |
| PDF preview + download | ✅ | ✅ |
| Bulk email send | ✅ | ✅ |
| Bulk send preview + confirmation | ✅ | ✅ |
| Bulk job status monitoring | ✅ | ✅ |
| Background bulk email daemon | ✅ | ✅ |
| Collection worker control | ✅ | ✅ |
| Collection metrics dashboard | ✅ | ✅ |
| Collection configuration | ✅ | ✅ |
| Journal configuration | ✅ | ✅ |
| Journal presets (save/load) | ✅ | ✅ |
| Publisher selection | ✅ | ✅ |
| Manual unsubscribe tool | ✅ | ✅ |
| Unsubscribe link handler | ✅ | ✅ |
| CSV export | ✅ | ✅ |
| CSV export with email | ✅ | ✅ |
| Invitation history | ✅ | ✅ |
| Suppression checks (inline badges) | ✅ | ✅ |
| Deduplication | ✅ | ✅ |
| Per-user session isolation | ❌ (shared) | ✅ |
| Retraction watch import | ✅ | ⏳ (via DB direct) |
| Sent invitations import | ✅ | ⏳ (via DB direct) |

## Project Structure

```
v2/
├── app.py                    # FastAPI application (all routes + logic)
├── session_store.py          # Per-user session management
├── requirements.txt          # Python dependencies
├── DEPLOY.md                 # Railway deployment instructions
├── README.md                 # This file
├── templates/
│   ├── base.html             # Base layout (nav, sidebar, tabs)
│   ├── dashboard.html        # Main page
│   ├── collection.html       # Collection worker panel
│   ├── history.html          # Invitation history
│   ├── unsubscribe_result.html # Unsubscribe confirmation page
│   └── partials/
│       ├── sidebar.html      # Sidebar with filters + config
│       ├── search_panel.html # Search form + filters
│       ├── results_table.html # Author results table
│       ├── invite_dialog.html # Individual send dialog
│       ├── bulk_preview.html  # Bulk send confirmation
│       ├── job_status.html    # Bulk job status cards
│       ├── preset_list.html   # Journal presets
│       └── error_message.html # Error display
└── static/
    └── css/
        └── style.css         # Professional styling
```

## Reused Modules (unchanged from parent directory)

All of these are imported from the parent directory and work without modification:

- `config.py` — Constants, countries, worker tunables
- `disciplines.py` — 27 OpenAlex fields, discipline categorization
- `openalex_client.py` — OpenAlex API client
- `orcid_async.py` — Async ORCID email fetching
- `openai_email_async.py` — OpenAI/Tavily email extraction
- `author_filters.py` — Specialty matching, deduplication
- `bulk_email_jobs.py` — Bulk recipient preparation
- `bulk_email_worker.py` — Background bulk email processing
- `email_sender.py` — SMTP + Brevo API email sending
- `templates.py` — 9 email templates with placeholders
- `pdf_generator.py` — Premium PDF invitation letters
- `db_client.py` — All PostgreSQL operations
- `journal_presets.py` — Preset config normalization
- `collector_worker.py` — Background email collection (separate service)
- `import_data.py` — CSV import scripts
- `emergency_suppress_invited.py` — Emergency suppression script

## Running Locally

```bash
cd v2
pip install -r requirements.txt
uvicorn app:app --reload --port 8000
```

Requires `DATABASE_URL` and `EMAIL_CREDENTIALS` environment variables.

## Deploying to Railway

See `DEPLOY.md` for detailed instructions.

## Session Isolation

Each browser tab gets a unique session cookie. Sessions store:
- Search results and batch cache
- Filter settings
- Journal configuration
- Publisher selection
- Selected authors
- Recent publications cache
- Processed ORCIDs

Sessions expire after 24 hours of inactivity.

## Key Differences from v1

1. **Per-user isolation** — Each user has their own search results, filters, and queue
2. **No full-page reloads** — HTMX swaps content in place for instant feedback
3. **Unified workflow** — Author and editorial invitations share the same search/results view
4. **Professional UI** — Clean, modern design with proper typography and spacing
5. **Faster performance** — No Streamlit rerender overhead, async email sending
