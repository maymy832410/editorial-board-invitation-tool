-- Run this in Supabase SQL Editor to create the sent_invitations table.
-- Required for the app to persist "sent" status across sessions.

create table if not exists sent_invitations (
  orcid_id   text primary key,
  author_name text default '',
  email      text default '',
  publisher  text default '',
  sent_at    timestamptz default now()
);

-- Allow anonymous (app) access if using anon key (enable RLS and add policy as needed)
alter table sent_invitations enable row level security;

-- Policy: allow all for anon (app uses anon key). Restrict in production if needed.
create policy "Allow anon read and write"
  on sent_invitations for all
  to anon
  using (true)
  with check (true);
