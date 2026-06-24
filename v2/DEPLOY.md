# Deploying editorial-board-v2 to Railway

## Option 1: Create via Railway Dashboard (Recommended)

1. Go to https://railway.app/dashboard
2. Open your `editorial-board-tool` project
3. Click **"+ New"** → **"Empty Service"**
4. Name it `editorial-board-v2`
5. Go to the service settings → **Settings** tab
6. Set the **Start Command** to:
   ```
   cd v2 && uvicorn app:app --host 0.0.0.0 --port $PORT --workers 2
   ```
7. Go to the **Variables** tab and add:
   - `DATABASE_URL` — copy from the existing `editorial-board-app` service (or link to the same Postgres)
   - `EMAIL_CREDENTIALS` — copy from the existing service
   - `BREVO_API_KEY` — copy from the existing service
   - Any other env vars the existing app uses
8. Go to the **Deploy** tab → connect your GitHub repo
9. Set the **Root Directory** to: `/` (root of repo)
10. Deploy!

## Option 2: Deploy via Railway CLI

```bash
# In the project root directory
railway init --name editorial-board-v2  # if creating new project
# OR link to existing project and add service:
railway service add --name editorial-board-v2

# Set the start command:
railway up  # This will deploy with detected settings
```

Then in the Railway dashboard, set the start command to:
```
cd v2 && uvicorn app:app --host 0.0.0.0 --port $PORT --workers 2
```

## Option 3: Docker Deploy

```bash
# In Railway dashboard, set service to use Docker
# Set Dockerfile Context to: /
# Set Dockerfile Path: Dockerfile.v2
```

## Post-Deploy Verification

1. Open the v2 app URL in your browser
2. Verify the dashboard loads with the new UI
3. Test search functionality (OpenAlex)
4. Test database email search
5. Test sending a single invitation
6. Test bulk send
7. Test collection panel
8. Test unsubscribe link
9. Compare with old Streamlit app to ensure feature parity

## After Verification

Once you're satisfied with v2:
1. Update your custom domain to point to `editorial-board-v2`
2. Delete the old `editorial-board-app` Streamlit service
3. Rename `editorial-board-v2` to `editorial-board-app` (optional)

## Shared Resources

Both services share:
- Same PostgreSQL database
- Same Postgres tables (no schema changes)
- Same `email_suppressions` table (unsubscribe works across both)
- Same `author_invitations` table (invitation tracking works across both)
- Same `bulk_email_jobs` table (bulk jobs visible in both)
- Same `collection_runs` table (worker state shared)

The collector-worker service stays unchanged and continues running.
