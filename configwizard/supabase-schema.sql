-- ============================================================
-- NetConfig — Supabase Database Schema
-- Run this in the Supabase SQL Editor
-- ============================================================

-- Profiles table: one row per device
create table public.profiles (
  id           uuid primary key default gen_random_uuid(),
  user_id      uuid not null references auth.users(id) on delete cascade,
  name         text not null,
  vendor       text not null check (vendor in ('cisco','aruba','juniper')),
  hostname     text,
  mgmt_ip      text,
  vlan_count   integer default 0,
  notes        text,
  created_at   timestamptz default now(),
  updated_at   timestamptz default now()
);

-- Versions table: one row per saved config snapshot
create table public.versions (
  id            uuid primary key default gen_random_uuid(),
  profile_id    uuid not null references public.profiles(id) on delete cascade,
  user_id       uuid not null references auth.users(id) on delete cascade,
  version_num   integer not null,
  label         text not null default 'Saved config',
  config_text   text,
  wizard_snapshot jsonb,   -- full wizard state for restore-into-wizard
  lines_added   integer default 0,
  lines_removed integer default 0,
  created_at    timestamptz default now()
);

-- Indexes
create index profiles_user_id_idx on public.profiles(user_id);
create index versions_profile_id_idx on public.versions(profile_id);
create index versions_user_id_idx on public.versions(user_id);

-- Auto-update updated_at on profiles
create or replace function public.handle_updated_at()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

create trigger profiles_updated_at
  before update on public.profiles
  for each row execute function public.handle_updated_at();

-- ── Row Level Security (RLS) ─────────────────────────────────
-- IMPORTANT: Users can ONLY see and edit their OWN data

alter table public.profiles enable row level security;
alter table public.versions  enable row level security;

-- Profiles: full access only to owner
create policy "profiles_select_own" on public.profiles
  for select using (auth.uid() = user_id);

create policy "profiles_insert_own" on public.profiles
  for insert with check (auth.uid() = user_id);

create policy "profiles_update_own" on public.profiles
  for update using (auth.uid() = user_id);

create policy "profiles_delete_own" on public.profiles
  for delete using (auth.uid() = user_id);

-- Versions: full access only to owner
create policy "versions_select_own" on public.versions
  for select using (auth.uid() = user_id);

create policy "versions_insert_own" on public.versions
  for insert with check (auth.uid() = user_id);

create policy "versions_update_own" on public.versions
  for update using (auth.uid() = user_id);

create policy "versions_delete_own" on public.versions
  for delete using (auth.uid() = user_id);

-- ============================================================
-- Done. RLS ensures zero cross-user data leakage at the
-- database level — even if there's a bug in the frontend.
-- ============================================================
