# SpanGate Network Monitor — Backend

FastAPI service that receives telemetry from SpanGate Network Monitor agents,
stores it in PostgreSQL, and serves data to the SpanGate dashboard.

---

## Stack

| Layer | Technology |
|---|---|
| Framework | FastAPI + uvicorn |
| Database | PostgreSQL via SQLAlchemy async (asyncpg) |
| Auth validation | Supabase (profiles table) |
| Cleanup | APScheduler — runs every 24 hours |
| Container | Docker |

---

## Local Development

```bash
# 1. Clone and enter the directory
cd netmonitor-backend

# 2. Create virtual environment
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env
# Fill in DATABASE_URL, SUPABASE_URL, SUPABASE_SERVICE_KEY

# 5. Start a local Postgres (Docker shortcut)
docker run -d --name pg -e POSTGRES_PASSWORD=postgres -p 5432:5432 postgres:16

# 6. Run the server
uvicorn main:app --reload --port 8000
```

Tables are created automatically on first boot via SQLAlchemy `create_all`.

Swagger UI: `http://localhost:8000/docs`

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `DATABASE_URL` | ✓ | PostgreSQL connection string (asyncpg format) |
| `SUPABASE_URL` | ✓ | Supabase project URL |
| `SUPABASE_SERVICE_KEY` | ✓ | Supabase service-role key (bypasses RLS) |
| `ENVIRONMENT` | | `development` or `production` (default: development) |
| `PORT` | | Port for uvicorn (default: 8000) |

---

## Required Supabase Table

API keys are validated against the `profiles` table in Supabase.
Run this once in the Supabase SQL editor if the table doesn't exist:

```sql
create table if not exists profiles (
  id        uuid    primary key references auth.users(id) on delete cascade,
  api_key   text    unique,
  site_id   text    not null default gen_random_uuid()::text,
  site_name text
);
alter table profiles enable row level security;
create policy "owner" on profiles for all using (auth.uid() = id);
```

To generate an API key for a user (run from a server-side context with service key):

```sql
update profiles
set api_key = 'spng_' || encode(gen_random_bytes(32), 'hex')
where id = '<user-uuid>';
```

Show the key to the user once — it cannot be recovered if lost.

---

## API Endpoints

All endpoints require `Authorization: Bearer <api_key>` except `/health`.

### Agent → Backend (posted by the agent)

| Method | Path | Description |
|---|---|---|
| POST | `/api/v1/agent/heartbeat` | Agent liveness check-in every 5 min |
| POST | `/api/v1/alerts/ping` | Device up/down state change |
| POST | `/api/v1/alerts/config-change` | Running-config hash changed |
| POST | `/api/v1/configs/backup` | Weekly config snapshot |

### Dashboard → Backend (consumed by the UI)

| Method | Path | Description |
|---|---|---|
| GET | `/api/v1/dashboard` | Aggregate summary |
| GET | `/api/v1/devices` | All devices with live status |
| GET | `/api/v1/devices/{hostname}` | Single device + alerts + config |
| GET | `/api/v1/alerts` | Last 50 alerts (filterable) |
| GET | `/api/v1/configs/{hostname}` | Last 10 config backup metadata |
| GET | `/api/v1/configs/{hostname}/{id}` | Full config text for one backup |
| GET | `/health` | Load-balancer health probe (no auth) |

---

## Database Schema

Tables are created automatically from `models.py`. Four tables:

| Table | Retention | Purpose |
|---|---|---|
| `devices` | Permanent | One row per monitored device per site |
| `configs` | 30 days | Running-config snapshots from SSH pulls |
| `config_diffs` | 30 days | Unified diff for each detected change |
| `alerts` | 30 days | Ping and config-change alert log |

The cleanup scheduler (APScheduler) deletes rows older than 30 days
from `configs`, `config_diffs`, and `alerts` every 24 hours.

---

## Production Deployment

### DigitalOcean App Platform (recommended)

1. Push the `netmonitor-backend/` directory to a GitHub repo
2. Create a new App → select your repo → choose the backend directory
3. Add a **DigitalOcean Managed PostgreSQL** database component ($15/mo)
4. Set environment variables in the App dashboard
5. Deploy — DigitalOcean runs the Dockerfile automatically

### Render.com

1. Create a new **Web Service** → connect your repo
2. Build command: *(Docker auto-detected)*
3. Add a **Render Postgres** database (free tier for dev)
4. Set `DATABASE_URL` to the Render internal connection string
5. Deploy

### Fly.io

```bash
fly launch       # creates fly.toml
fly secrets set DATABASE_URL=... SUPABASE_URL=... SUPABASE_SERVICE_KEY=...
fly deploy
```

---

## In-Memory State

Device ping status and agent heartbeat state are held in memory (no Redis).
This means state resets on every restart. The database is the source of truth
for alerts and config history; in-memory state is for real-time dashboard data only.

---

*SpanGate Technology — Your network. Under control.*
