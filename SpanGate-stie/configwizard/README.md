# NetConfig — Deployment Guide

A private, per-user network config wizard with saved profiles and version history.
Built on Supabase (auth + database) + Vercel (hosting). Free tier covers everything
until you have hundreds of daily active users.

---

## Architecture

```
browser
  ├── index.html       → Login / signup page
  ├── dashboard.html   → User's profile list + stats
  ├── profile.html     → Single device profile + version history
  └── wizard.html      → The network config wizard (your existing file)
        ↓
  Supabase
    ├── Auth           → Email/password + Google OAuth
    ├── profiles       → One row per device (per user)
    └── versions       → Config snapshots (per profile)
        ↓ Row Level Security
    Users can ONLY see their own data
```

---

## Step 1 — Create a Supabase project (5 min)

1. Go to https://supabase.com and sign up (free)
2. Click **New Project**, give it a name like `netconfig`
3. Choose a region close to your users
4. Wait ~2 min for it to provision

5. Go to **SQL Editor** → paste the entire contents of `supabase-schema.sql` → click **Run**

6. Go to **Project Settings → API**. Copy:
   - **Project URL** → your `SUPABASE_URL`
   - **anon public** key → your `SUPABASE_ANON_KEY`

---

## Step 2 — Enable Google OAuth (optional but recommended)

1. In Supabase: **Authentication → Providers → Google**
2. Create a Google OAuth app at https://console.cloud.google.com
   - Authorized redirect URI: `https://YOUR-PROJECT.supabase.co/auth/v1/callback`
3. Paste Client ID + Secret into Supabase

---

## Step 3 — Add your keys to the site files

In **each** of these files, replace the placeholder values:

```
index.html
dashboard.html
profile.html
wizard.html  (if you've added Supabase calls there)
```

Find and replace:
```javascript
const SUPABASE_URL  = 'YOUR_SUPABASE_URL';
const SUPABASE_ANON = 'YOUR_SUPABASE_ANON_KEY';
```

With your actual values:
```javascript
const SUPABASE_URL  = 'https://xyzxyzxyz.supabase.co';
const SUPABASE_ANON = 'eyJhbGciOiJIUzI1...';
```

The anon key is safe to put in frontend code — Supabase's Row Level Security
policies ensure users can only access their own data regardless.

---

## Step 4 — Add Supabase calls to wizard.html

In `wizard.html`, the profiles panel already saves to browser storage.
Update the `pf_persist()` and `pf_load()` functions to use Supabase instead:

```javascript
// At the top of the <script> block, add:
const sb = supabase.createClient(SUPABASE_URL, SUPABASE_ANON);

// Replace pf_load():
async function pf_load() {
  const { data: { session } } = await sb.auth.getSession();
  if (!session) return; // not logged in, skip
  const uid = session.user.id;

  const { data: profiles } = await sb
    .from('profiles')
    .select('*')
    .eq('user_id', uid)
    .order('updated_at', { ascending: false });

  const { data: versions } = await sb
    .from('versions')
    .select('*')
    .eq('user_id', uid)
    .order('version_num', { ascending: false });

  pf_profiles = profiles || [];
  pf_history  = (versions || []).map(v => ({
    ...v, profileId: v.profile_id, configText: v.config_text,
    version: v.version_num, label: v.label, savedAt: new Date(v.created_at).getTime(),
    linesAdded: v.lines_added, linesRemoved: v.lines_removed
  }));
  pf_render();
}

// Replace pf_persist() — call this after every save:
async function pf_persistProfile(profile, newVersion) {
  const { data: { session } } = await sb.auth.getSession();
  if (!session) return;
  const uid = session.user.id;

  // Upsert profile
  await sb.from('profiles').upsert({
    id: profile.id.startsWith('p') ? undefined : profile.id,
    user_id: uid,
    name: profile.name,
    vendor: profile.vendor,
    hostname: profile.hostname,
    mgmt_ip: profile.mgmtIP || '',
    vlan_count: profile.vlans || 0,
    notes: profile.notes || '',
    updated_at: new Date().toISOString()
  });

  // Insert version
  if (newVersion) {
    await sb.from('versions').insert({
      profile_id: profile.id,
      user_id: uid,
      version_num: newVersion.version,
      label: newVersion.label,
      config_text: newVersion.configText,
      wizard_snapshot: newVersion.wizardSnapshot,
      lines_added: newVersion.linesAdded,
      lines_removed: newVersion.linesRemoved
    });
  }
}
```

---

## Step 5 — Deploy to Vercel (5 min)

1. Install Vercel CLI: `npm i -g vercel`
2. From the `netconfig-app/` folder: `vercel`
3. Follow prompts — it's just static HTML, zero config needed
4. Your site is live at `https://your-project.vercel.app`

**Or drag-and-drop:**
Go to https://vercel.com/new → drag the entire `netconfig-app/` folder → done.

---

## File structure

```
netconfig-app/
  index.html           → Login page
  dashboard.html       → Profile list
  profile.html         → Profile detail + version history
  wizard.html          → The config wizard (copy your latest version here)
  supabase-schema.sql  → Run once in Supabase SQL Editor
  README.md            → This file
```

---

## Security notes

- **Row Level Security** is enabled on both tables. Users can only SELECT, INSERT,
  UPDATE, DELETE their own rows. This is enforced at the database level,
  not just the frontend — even if someone manipulates the JS.

- The `anon` key is safe to expose in frontend code. It only allows what RLS permits.

- Never expose the `service_role` key in frontend code.

- Passwords are handled entirely by Supabase Auth (bcrypt). You never touch them.

---

## Pricing to expect

| Users | Supabase | Vercel |
|-------|----------|--------|
| 0–500 | Free | Free |
| 500–10k | Free (50k MAU limit) | Free |
| 10k+ | $25/mo Pro | Free or $20/mo Pro |

The free tier is genuinely generous — you won't need to pay until you have real traction.

---

## Next steps (Phase 2)

- [ ] Add Stripe for subscriptions ($9/mo)
- [ ] Gate version history > 5 versions behind Pro plan
- [ ] Add email notifications ("Your config hasn't been updated in 30 days")
- [ ] Add compliance templates (CIS benchmarks)
- [ ] Add config push via SSH (Paramiko / ssh2)
