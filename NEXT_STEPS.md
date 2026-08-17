# HALO to the finish line

Where things stand and what is left. Tick these off in order; each has a check so
you know it worked before moving on.

## Done

- Three data agents wired and answering live: `cloudmetrics`, `manufacturing`, `ecommerce`.
- Routing at 21/21 on both the model tier and the keyword fallback.
- `test_local.py` at 34 checks, no cloud needed.
- Local app runs against real Fabric as your az CLI identity (`run_local.py`).
- Git repo initialised. Nothing pushed anywhere yet.

## Step 1 — Entra app registration (you, ~10 min)

Azure portal → Microsoft Entra ID → App registrations → **New registration**, in the
**Fabric tenant**.

1. Name it (HALO). Accounts: **single tenant**. Register.
2. Copy the **Application (client) ID** and **Directory (tenant) ID** from the overview.
3. Authentication → Add a platform → **Web** → Redirect URI:
   `http://localhost:8000/auth/callback` → Configure.
   (http is allowed for localhost. The Render URI gets added later, in step 5.)
4. API permissions → Add a permission → APIs my organization uses → **Power BI Service**
   → Delegated → `DataAgent.Execute.All` → Add → then **Grant admin consent**.
   The consent step is the one people skip; without it every agent call 401s.
5. Certificates & secrets → New client secret → copy the **Value**, not the Secret ID.
   You cannot see it again after leaving the page.

**Check:** you have three things — client id, tenant id, secret value.

## Step 2 — put them in .env (you, 1 min)

Append to `.env` in this folder (already gitignored):

```
HALO_CLIENT_ID=<application (client) id>
HALO_TENANT=<directory (tenant) id>
HALO_CLIENT_SECRET=<the secret Value>
HALO_REDIRECT_URI=http://localhost:8000/auth/callback
```

The client id and tenant id are public identifiers and fine to share. The secret is not.

**Check:** `python -c "import server; print(bool(server.CLIENT_ID), server.REDIRECT_URI)"`
prints `True http://localhost:8000/auth/callback`.

## Step 3 — test the redirect flow locally (30 min of debugging saved)

Stop `run_local.py` first, it holds port 8000.

```
python server.py
```

Open http://localhost:8000, click **Sign in**, complete the Microsoft prompt, and ask
one question.

This is the only step `run_local.py` cannot cover, and it is where deploys usually fail.
Getting it right on localhost is far faster than getting it right on Render.

**Check:** you land back on the page signed in, `/api/me` shows your UPN, and a question
returns data.

Failures, all seen before:

| symptom | cause |
| --- | --- |
| `AADSTS50011` | redirect URI not an exact match (trailing slash counts), or wrong client id |
| `AADSTS7000215` | invalid secret: the Secret **ID** pasted instead of its **Value**, or expired |
| signed in, but agents 401 | `DataAgent.Execute.All` missing admin consent |

## Step 4 — push to GitHub (~5 min)

`gh auth switch -u pawarbi` first if needed.

```
gh repo create data-agent-halo --private --source=. --push
```

Private to start. `.gitignore` covers `.env`, `or.txt`, `*.key`, `secrets*`.

**Check:** `git log --oneline origin/main` matches local, and the repo on GitHub shows
no `.env`.

## Step 5 — deploy to Render

1. render.com → New → **Web Service** → connect the repo. `render.yaml` is detected.
2. Set the secret values in the Render dashboard (they are `sync: false`, so they are
   not in the file): `HALO_CLIENT_ID`, `HALO_TENANT`, `HALO_CLIENT_SECRET`,
   `OPENROUTER_API_KEY`.
3. Deploy. Note the URL, `https://<app>.onrender.com`.
4. Back in Entra: Authentication → add a **second** redirect URI,
   `https://<app>.onrender.com/auth/callback`. Keep the localhost one so you can still
   test locally.
5. In Render, set `HALO_REDIRECT_URI=https://<app>.onrender.com/auth/callback` and
   redeploy.

**Check:** open the URL, sign in, ask one question per agent.

## Step 6 — prove it end to end

- One question per agent. Suggested: "how many active subscriptions are there by plan?",
  "what was OEE by plant?", "how is the olist business doing?"
- One cross-domain question, to see the parallel fan-out and the period-mismatch note.
- One question the judge dislikes, to see the retry arc.

## Step 7 — the RLS claim

Every data-agent call runs as the signed-in user, so Fabric applies that user's RLS. That
is true by construction but **not yet demonstrated** — everything so far has run as
`you@example.com`. To actually show it, have a second user in the tenant sign
in and ask the same question, and compare.

Until someone does that, describe it as how the system is built rather than as something
observed.

## Known, and not fixable from HALO

- **Three different time universes.** CloudMetrics data ends 2024-12, Olist runs 2016-09
  to 2018-09, manufacturing reports 2026 dates. Every cross-domain answer carries a
  period-mismatch disclaimer, by design. There is no clean like-for-like number to show.
- **NL2SQL variance.** The same question can succeed and fail across runs; one ecommerce
  question did exactly that. Rehearse any specific demo question more than once.
- **Free-tier Render sleeps.** ~30-60s cold start. Warm it before a demo, or take the
  $7/mo Starter.
- **Cross-domain latency.** 30-55s single-domain, up to ~100s when the validator retries.

## Later, not blocking

- Human-in-the-loop gate surfaced in the web UI (console-only today, `HALO_HITL=1`).
- LangSmith tracing.
- Secretless auth on Azure via managed identity, the pattern Prism uses, instead of a
  client secret.
- `AGENTS.md` references `halo.py`, `README.md` and `halo_reasoning_visual.html`, which
  were never in the source zip.
