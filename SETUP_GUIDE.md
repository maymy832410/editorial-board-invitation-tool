# Editorial Board Invitation Tool - Setup Guide

## Project Overview

A Streamlit app for finding academic authors via OpenAlex API and sending editorial board invitations.

**GitHub Repository:** https://github.com/maymy832410/editorial-board-invitation-tool

---

## Email Configuration

### AWS SES (Peninsula Publishing Press)

```
SMTP Host: email-smtp.us-east-1.amazonaws.com
Port: 587
Encryption: STARTTLS (TLS)
SMTP Username: AKIAT25BUYY3EDG6VHVN
SMTP Password: BHU7saZXG7tCRpmQfbTLUfueklTGqrowuoDRpy3HxnCu
From Email: editorial@peninsula-press.ae
From Name: Peninsula Publishing Press
```

**Note:** AWS SES requires production approval to send to unverified emails. Currently in sandbox mode (can only send to verified emails).

### Titan SMTP (Mesopotamian Academic Press)

```
SMTP Host: smtp.titan.email
Port: 465
Encryption: SSL
Email: editorial@mesopotamian.press
Password: oR^{;a/G~</yhp]
```

**Limit:** 50 emails/day

### email_credentials.json Format

```json
{
  "peninsula": {
    "name": "Peninsula Publishing Press",
    "email": "editorial@peninsula-press.ae",
    "password": "BHU7saZXG7tCRpmQfbTLUfueklTGqrowuoDRpy3HxnCu",
    "smtp_server": "email-smtp.us-east-1.amazonaws.com",
    "smtp_port": 587,
    "use_ssl": false,
    "smtp_username": "AKIAT25BUYY3EDG6VHVN"
  },
  "mesopotamian": {
    "name": "Mesopotamian Academic Press",
    "email": "editorial@mesopotamian.press",
    "password": "oR^{;a/G~</yhp]",
    "smtp_server": "smtp.titan.email",
    "smtp_port": 465,
    "use_ssl": true
  }
}
```

---

## Git Commands

### Initial Setup (if not done)

```bash
cd "/Users/smart/find authors emails"
git init
git remote add origin https://github.com/maymy832410/editorial-board-invitation-tool.git
```

### Daily Workflow

```bash
# Check status
git status

# Stage all changes
git add .

# Commit with message
git commit -m "Your commit message"

# Push to GitHub
git push

# Pull latest changes
git pull
```

### If push is rejected

```bash
git pull --rebase
git push
```

---

## Running the App Locally

```bash
cd "/Users/smart/find authors emails"
streamlit run app.py
```

App runs at: http://localhost:8501

### Kill existing app and restart

```bash
pkill -f "streamlit run"
streamlit run app.py
```

---

## Deploy to Streamlit Cloud

### 1. Push code to GitHub

```bash
git add .
git commit -m "Deploy to Streamlit Cloud"
git push
```

### 2. Go to Streamlit Cloud

- URL: https://share.streamlit.io
- Sign in with GitHub
- Click "New app"
- Select repository: `maymy832410/editorial-board-invitation-tool`
- Branch: `main`
- Main file: `app.py`

### 3. Configure Secrets

In Streamlit Cloud dashboard → App settings → Secrets, add:

```toml
[publishers.peninsula]
name = "Peninsula Publishing Press"
email = "editorial@peninsula-press.ae"
password = "BHU7saZXG7tCRpmQfbTLUfueklTGqrowuoDRpy3HxnCu"
smtp_server = "email-smtp.us-east-1.amazonaws.com"
smtp_port = 587
use_ssl = false
smtp_username = "AKIAT25BUYY3EDG6VHVN"

[publishers.mesopotamian]
name = "Mesopotamian Academic Press"
email = "editorial@mesopotamian.press"
password = "oR^{;a/G~</yhp]"
smtp_server = "smtp.titan.email"
smtp_port = 465
use_ssl = true
```

---

## OpenAlex API

- Polite email: `maymy832410@gmail.com`
- Base URL: `https://api.openalex.org`
- No API key required (polite pool with email)

---

## Key Files

| File | Purpose |
|------|---------|
| `app.py` | Main Streamlit application |
| `openalex_client.py` | OpenAlex API client |
| `email_sender.py` | SMTP email sending |
| `email_credentials.json` | Email credentials (gitignored) |
| `templates.py` | Email invitation templates |
| `pdf_generator.py` | PDF invitation letter generator |
| `disciplines.py` | Author discipline categorization |
| `progress_manager.py` | State persistence |

---

## AWS SES Setup

### Verify Email/Domain

1. AWS Console → SES → Verified Identities
2. Create identity → Email address
3. Enter: `editorial@peninsula-press.ae`
4. Click verification link in email

### Request Production Access

1. AWS Console → SES → Account dashboard
2. Click "Request production access"
3. Fill form (sending editorial invitations)
4. Wait 24-48 hours for approval

### Create SMTP Credentials

1. AWS Console → SES → SMTP settings
2. Click "Create SMTP credentials"
3. Save the username and password

---

## Troubleshooting

### "Missing final '@domain'" error
- Check email_credentials.json has valid email addresses
- Ensure recipient email is not empty

### AWS SES "Email not verified" error
- In sandbox mode, both sender AND recipient must be verified
- Request production access to send to anyone

### Git push rejected
```bash
git pull --rebase
git push
```

### App not updating after code changes
```bash
pkill -f "streamlit run"
streamlit run app.py
```

---

## Long-Run OpenAlex Enrichment (20k+ Profiles)

The importer now runs OpenAlex enrichment in repeated chunks and uses DB status fields as resume state.
If the process stops, re-running `import_data.py` continues from remaining `unknown` rows.

### Enrichment controls

- `OPENALEX_ENRICH_BATCH_SIZE` (default: `500`) - rows per batch
- `OPENALEX_ENRICH_MAX_TOTAL` (default: `0`) - hard cap for one run (`0` means unbounded)
- `OPENALEX_ENRICH_PROGRESS_EVERY` (default: `50`) - progress print interval
- `OPENALEX_ENRICH_BATCH_PAUSE_SEC` (default: `0`) - pause between DB batches
- `OPENALEX_ENRICH_INCLUDE_PENDING_MANUAL` (default: `false`) - include `pending_manual` rows in queue
- `OPENALEX_ENRICH_LIMIT` (legacy fallback) - used only when batch size is not set

### OpenAlex request controls

- `OPENALEX_MAX_RETRIES` (default: `3`)
- `OPENALEX_REQUEST_TIMEOUT_SEC` (default: `30`)
- `OPENALEX_BACKOFF_BASE_SEC` (default: `2`)
- `OPENALEX_REQUEST_PAUSE_SEC` (default: `0.1`)

### Example run

```bash
OPENALEX_ENRICH_BATCH_SIZE=300 \
OPENALEX_ENRICH_MAX_TOTAL=20000 \
OPENALEX_ENRICH_PROGRESS_EVERY=100 \
OPENALEX_ENRICH_BATCH_PAUSE_SEC=0.2 \
OPENALEX_MAX_RETRIES=4 \
python import_data.py
```

### Recommended operation

1. Run once with small limits to verify logging and status transitions.
2. Start a larger run for production volume.
3. If interrupted, restart with the same command; processed rows are skipped automatically.
4. Keep `OPENALEX_ENRICH_INCLUDE_PENDING_MANUAL=false` for normal runs to avoid retry loops.

---

## Contact/Accounts

- GitHub: maymy832410
- OpenAlex polite email: maymy832410@gmail.com
- AWS SES Region: us-east-1

---

*Last updated: January 2026*
