# HALO to the finish line

Where things stand and what is left. Tick these off in order; each has a check so
you know it worked before moving on.

## Done

- Three data agents wired and answering live: `cloudmetrics`, `manufacturing`, `ecommerce`.
- Routing at 21/21 on both the model tier and the keyword fallback.
- `test_local.py` at 34 checks, no cloud needed.
- Local app runs against real Fabric as your az CLI identity (`run_local.py`).
- Git repo initialised. Nothing pushed anywhere yet.

## Step 1 — reuse the AXIS app registration (you, ~3 min)

Do **not** create a new registration if you already own one with
`DataAgent.Execute.All` consented. An owner can add a redirect URI without an admin,
which is the cheap path.

Worth checking before you start: Power BI Administrator does **not** grant app
consent in Entra. If your tenant also limits self-consent to permissions classified
low impact, and Power BI Service has none classified, a brand new app registration
will need a Global Administrator to grant consent before anyone can sign in.

1. entra.microsoft.com → App registrations → your app →
   **Authentication** → under Web, **Add URI**: `http://localhost:8000/auth/callback`
   → **Save**. Leave any existing redirect URIs alone; other apps may need them.
2. **Certificates & secrets → New client secret** → copy the **Value**, not the Secret
   ID. (The AXIS secret lives in HF's secret store and cannot be read back, so make a
   new one. Multiple secrets on one app are fine.)

`HALO_CLIENT_ID`, `HALO_TENANT` and `HALO_REDIRECT_URI` are already filled in `.env`.

**Check:** `python -c "import server; print(server.CLIENT_ID, server.REDIRECT_URI)"`

## Step 2 — add the secret to .env (you, 1 min)

Uncomment the last line of `.env` and paste the Value:

```
HALO_CLIENT_SECRET=<the secret Value>
```

**Check:** `python -c "import server; print(bool(server.CLIENT_SECRET))"` prints `True`.

### Consent is per-user here, not tenant-wide

The existing grant has `consentType: Principal` — it covers **you**, not everyone. There
is no `AllPrincipals` grant. So a second user signing in will most likely hit "Need admin
approval" rather than a consent prompt.

That is fine for a single-presenter demo, but it blocks **step 7** (the RLS proof) and any
workshop with attendees signing in themselves. It applies to AXIS equally. To fix it
properly a Global Administrator grants tenant-wide consent once:

```
https://login.microsoftonline.com/<tenant-id>/adminconsent?client_id=<client-id>
```

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
is true by construction but **not yet demonstrated** — everything so far has run as a
single user. To actually show it, have a second user in the tenant sign in and ask the
same question, then compare.

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
