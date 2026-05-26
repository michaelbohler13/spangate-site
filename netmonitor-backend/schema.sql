-- ============================================================================
-- SpanGate Network Monitor — Supabase Database Schema
-- ============================================================================
-- Run this once in the Supabase SQL editor:
--   Dashboard → SQL Editor → New query → paste → Run
--
-- All tables are prefixed with nm_ to avoid collisions with other products.
-- Row Level Security (RLS) is enabled on every table; users can only read
-- their own data.  The backend uses the service-role key which bypasses RLS
-- for writes.
-- ============================================================================


-- ── API Keys ─────────────────────────────────────────────────────────────────
-- One row per agent deployment.  Created from the dashboard Settings page.
-- The raw key is shown to the user exactly once; we store only the SHA256 hash.

create table if not exists nm_api_keys (
  id           uuid        primary key default gen_random_uuid(),
  user_id      uuid        not null references auth.users(id) on delete cascade,

  -- Authentication
  key_hash     text        not null unique,  -- SHA256(raw_key), lowercase hex
  key_prefix   text        not null,         -- first 8 chars shown in UI e.g. "spng_a1b2"

  -- Metadata
  site_name    text        not null,         -- human label, e.g. "Main Campus"
  label        text        not null default 'Default',

  -- State
  is_active    boolean     not null default true,
  created_at   timestamptz not null default now(),
  last_used_at timestamptz               -- updated on every authenticated request
);

alter table nm_api_keys enable row level security;

create policy "nm_api_keys: owner select"
  on nm_api_keys for select using (auth.uid() = user_id);

create policy "nm_api_keys: owner insert"
  on nm_api_keys for insert with check (auth.uid() = user_id);

create policy "nm_api_keys: owner update"
  on nm_api_keys for update using (auth.uid() = user_id);

create policy "nm_api_keys: owner delete"
  on nm_api_keys for delete using (auth.uid() = user_id);


-- ── Heartbeats ────────────────────────────────────────────────────────────────
-- One row per heartbeat (every 5 minutes per agent).
-- The dashboard queries the most-recent row per site to show agent liveness
-- and aggregate device counts.

create table if not exists nm_heartbeats (
  id            bigint      generated always as identity primary key,
  user_id       uuid        not null references auth.users(id) on delete cascade,
  api_key_id    uuid        not null references nm_api_keys(id) on delete cascade,

  site_name     text        not null,
  device_count  int         not null check (device_count >= 0),
  devices_up    int         not null check (devices_up >= 0),
  devices_down  int         not null check (devices_down >= 0),
  agent_version text        not null default 'unknown',

  agent_ts      timestamptz not null,   -- timestamp the agent recorded
  received_at   timestamptz not null default now()  -- when we received it
);

alter table nm_heartbeats enable row level security;

create policy "nm_heartbeats: owner select"
  on nm_heartbeats for select using (auth.uid() = user_id);

-- Fast lookup: latest heartbeat per user + site
create index if not exists nm_heartbeats_latest
  on nm_heartbeats (user_id, site_name, received_at desc);

-- Purge heartbeats older than 30 days (schedule via pg_cron or Supabase cron):
-- select cron.schedule(
--   'purge-nm-heartbeats', '0 3 * * *',
--   $$delete from nm_heartbeats where received_at < now() - interval '30 days'$$
-- );


-- ── Ping Events ───────────────────────────────────────────────────────────────
-- One row per device state transition (up → down or down → up).
-- NOT recorded on every ping — only on changes — so this table stays small.

create table if not exists nm_ping_events (
  id          bigint      generated always as identity primary key,
  user_id     uuid        not null references auth.users(id) on delete cascade,
  api_key_id  uuid        not null references nm_api_keys(id) on delete cascade,

  site_name   text        not null,
  hostname    text        not null,
  ip          text        not null,
  status      text        not null check (status in ('up', 'down')),

  agent_ts    timestamptz not null,
  received_at timestamptz not null default now()
);

alter table nm_ping_events enable row level security;

create policy "nm_ping_events: owner select"
  on nm_ping_events for select using (auth.uid() = user_id);

-- Fast lookup: recent events per user + site + device
create index if not exists nm_ping_events_device
  on nm_ping_events (user_id, site_name, hostname, received_at desc);

-- Fast lookup: all recent events for a user (alert feed)
create index if not exists nm_ping_events_user_recent
  on nm_ping_events (user_id, received_at desc);

-- Purge ping events older than 30 days:
-- select cron.schedule(
--   'purge-nm-ping-events', '0 3 * * *',
--   $$delete from nm_ping_events where received_at < now() - interval '30 days'$$
-- );


-- ── Config Changes ────────────────────────────────────────────────────────────
-- One row per detected config change.  Stores the full new config text so the
-- dashboard can render a diff against any prior backup.

create table if not exists nm_config_changes (
  id          bigint      generated always as identity primary key,
  user_id     uuid        not null references auth.users(id) on delete cascade,
  api_key_id  uuid        not null references nm_api_keys(id) on delete cascade,

  site_name   text        not null,
  hostname    text        not null,
  new_config  text        not null,   -- full running-config after the change
  old_hash    char(64)    not null,   -- SHA256 of the previous config
  new_hash    char(64)    not null,   -- SHA256 of this config

  agent_ts    timestamptz not null,
  received_at timestamptz not null default now()
);

alter table nm_config_changes enable row level security;

create policy "nm_config_changes: owner select"
  on nm_config_changes for select using (auth.uid() = user_id);

create index if not exists nm_config_changes_device
  on nm_config_changes (user_id, site_name, hostname, received_at desc);

create index if not exists nm_config_changes_user_recent
  on nm_config_changes (user_id, received_at desc);

-- Purge config changes older than 30 days:
-- select cron.schedule(
--   'purge-nm-config-changes', '0 3 * * *',
--   $$delete from nm_config_changes where received_at < now() - interval '30 days'$$
-- );


-- ── Config Backups ────────────────────────────────────────────────────────────
-- One row per weekly pull per device.  The dashboard uses these rows to show
-- config history and lets users compare any two snapshots.

create table if not exists nm_config_backups (
  id          bigint      generated always as identity primary key,
  user_id     uuid        not null references auth.users(id) on delete cascade,
  api_key_id  uuid        not null references nm_api_keys(id) on delete cascade,

  site_name   text        not null,
  hostname    text        not null,
  config_text text        not null,   -- full running-config at backup time
  config_hash char(64)    not null,   -- SHA256 of config_text (for dedup)

  agent_ts    timestamptz not null,
  received_at timestamptz not null default now()
);

alter table nm_config_backups enable row level security;

create policy "nm_config_backups: owner select"
  on nm_config_backups for select using (auth.uid() = user_id);

create index if not exists nm_config_backups_device
  on nm_config_backups (user_id, site_name, hostname, received_at desc);

-- Purge backups older than 30 days:
-- select cron.schedule(
--   'purge-nm-config-backups', '0 3 * * *',
--   $$delete from nm_config_backups where received_at < now() - interval '30 days'$$
-- );


-- ============================================================================
-- Convenience views (optional — useful for the dashboard API)
-- ============================================================================

-- Latest heartbeat per user+site
create or replace view nm_latest_heartbeats as
  select distinct on (user_id, site_name)
    *
  from nm_heartbeats
  order by user_id, site_name, received_at desc;

-- Most recent ping status per user+site+device
create or replace view nm_latest_ping_status as
  select distinct on (user_id, site_name, hostname)
    *
  from nm_ping_events
  order by user_id, site_name, hostname, received_at desc;

-- Most recent config backup per user+site+device
create or replace view nm_latest_config_backups as
  select distinct on (user_id, site_name, hostname)
    *
  from nm_config_backups
  order by user_id, site_name, hostname, received_at desc;
