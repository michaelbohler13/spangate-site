-- ============================================================================
-- SpanGate Network Monitor — Database Schema
-- ============================================================================
-- Run this once to create all tables before deploying the backend.
--
-- For Supabase:
--   Dashboard → SQL Editor → New query → paste this → Run
--
-- The backend connects with the Supabase "service role" connection string
-- (DATABASE_URL environment variable).  SQLAlchemy's create_all() will also
-- create these tables automatically on first startup, so this file is
-- primarily useful for manual inspection or pre-provisioning.
-- ============================================================================


-- ── profiles ─────────────────────────────────────────────────────────────────
-- One row per Supabase auth user.
-- Stores the user's API key (plain text — shown to user once, never logged)
-- and a site_id that scopes all their devices/alerts to their account.
-- Created automatically when the user first visits the Settings page.

create table if not exists nm_profiles (
    id          uuid        primary key references auth.users(id) on delete cascade,
    api_key     text        unique,          -- Bearer token the agent sends
    site_id     text        not null default gen_random_uuid()::text,
    site_name   text
);

alter table nm_profiles enable row level security;

create policy "nm_profiles: owner all"
    on nm_profiles for all using (auth.uid() = id);


-- ── devices ──────────────────────────────────────────────────────────────────
-- One row per monitored device per site.
-- Devices are auto-created when the backend first receives a ping or config
-- event for an unknown hostname.

create table if not exists devices (
    id                  bigserial       primary key,
    site_id             varchar(255)    not null,
    hostname            varchar(255)    not null,
    ip                  varchar(45)     not null,
    vendor              varchar(100)    not null    default 'unknown',
    device_type         varchar(100)    not null    default 'unknown',

    -- Live status — written by the ping alert endpoint on every state change
    status              varchar(20)     not null    default 'unknown',
    last_seen           timestamptz,
    last_status_change  timestamptz,

    created_at          timestamptz     not null    default now(),

    constraint uq_devices_site_hostname unique (site_id, hostname)
);

create index if not exists ix_devices_site_hostname
    on devices (site_id, hostname);


-- ── agent_heartbeat ───────────────────────────────────────────────────────────
-- One row per site — upserted on every heartbeat POST from the agent.
-- Replaces the old in-memory heartbeat_state dict so data survives
-- serverless function cold starts.

create table if not exists agent_heartbeat (
    id              bigserial       primary key,
    site_id         varchar(255)    not null    unique,
    site_name       varchar(255)    not null    default '',
    last_seen       timestamptz     not null,
    agent_version   varchar(50),
    device_count    integer         not null    default 0,
    devices_up      integer         not null    default 0,
    devices_down    integer         not null    default 0
);


-- ── configs ───────────────────────────────────────────────────────────────────
-- Running-config snapshots pulled from devices via SSH.
-- One row per weekly backup per device.  Retained for 30 days.
-- config_text is never logged — only config_hash is exposed via the API.

create table if not exists configs (
    id              bigserial       primary key,
    device_id       bigint          not null    references devices(id) on delete cascade,
    config_text     text            not null,
    config_hash     varchar(64)     not null,
    pulled_at       timestamptz     not null    default now()
);

create index if not exists ix_configs_device_pulled
    on configs (device_id, pulled_at);

create index if not exists ix_configs_device_hash
    on configs (device_id, config_hash);


-- ── config_diffs ──────────────────────────────────────────────────────────────
-- Unified diff records produced when a config change is detected.
-- Retained for 30 days.

create table if not exists config_diffs (
    id          bigserial       primary key,
    device_id   bigint          not null    references devices(id) on delete cascade,
    old_hash    varchar(64)     not null,
    new_hash    varchar(64)     not null,
    diff_text   text            not null,
    detected_at timestamptz     not null    default now()
);

create index if not exists ix_config_diffs_device_detected
    on config_diffs (device_id, detected_at);


-- ── alerts ────────────────────────────────────────────────────────────────────
-- Alert events: ping_down, ping_up, config_change.
-- Retained for 30 days.  Purged nightly by the /api/v1/admin/cleanup endpoint
-- called via Vercel Cron.

create table if not exists alerts (
    id          bigserial       primary key,
    device_id   bigint          not null    references devices(id) on delete cascade,
    alert_type  varchar(50)     not null,   -- ping_down | ping_up | config_change
    message     varchar(500)    not null,
    created_at  timestamptz     not null    default now()
);

create index if not exists ix_alerts_device_created
    on alerts (device_id, created_at);

-- Fast cross-device feed queries
create index if not exists ix_alerts_created_at
    on alerts (created_at);


-- ── migrations ────────────────────────────────────────────────────────────────
-- Run these if upgrading an existing deployment (create_all won't ALTER tables).

-- 2026-05-28: manual backup request flag
alter table device_configs
    add column if not exists backup_requested_at timestamptz default null;
