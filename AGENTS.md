# HALO — Project Overview (for Claude Code)

> Read this first. It's the context for working on HALO without re-deriving
> decisions we already made. HALO is one of three sibling apps over Microsoft
> Fabric Data Agents; this doc is scoped to HALO but notes the siblings where relevant.

---

## What HALO is

**HALO = Hierarchical Agent Loop Orchestrator.** A **LangGraph** orchestrator that
routes a natural-language question across **multiple Fabric Data Agents**, calls
them (in parallel when needed), synthesizes a combined answer, and validates it —
with a real **retry loop**. It's a FastAPI web app with **redirect (auth-code)
sign-in** and a **live reasoning-graph UI** that streams the graph's real trace
over SSE so you watch it think.

Every data-agent call runs under the **signed-in user's identity**, so Fabric
enforces that user's RLS/CLS. HALO never uses a service identity to read data.

### The three sibling apps (context)
- **AXIS** — single-agent console (Gradio). Direct MCP call to one data agent. No framework.
- **Prism** — enterprise portal (FastAPI + MSAL.js). Direct MCP *or* Foundry mode.
- **HALO** (this) — multi-agent orchestrator (LangGraph). Routes across many agents.

Decision rule these embody: **one agent → no framework (AXIS); route across
domains in the Microsoft stack → Foundry + Fabric IQ tools; route across domains
with custom control/loops → LangGraph (HALO).**

---

## Architecture

```
Browser ──/auth/login──▶ Microsoft (Entra) ──redirect──▶ /auth/callback
   │                                                          │
   │  (session cookie holds serialized MSAL token cache)      │
   ▼                                                          ▼
 index.html  ──GET /api/ask?q=… (SSE)──▶  server.py  ──runs──▶  LangGraph (halo_core.py)
   ▲                                                          │
   └────────── streamed trace events (per node) ◀─────────────┘
                                                              │ each fan_out call:
                                                              ▼
                                          Fabric Data Agent MCP endpoint (as the user)
```

### The graph (halo_core.py)

```
START → classify → gate → fan_out → synthesize → validate ─┬─ pass → END
                    │        ▲                              │
                 reject      └──────── retry (cycle) ───────┘
```

- **classify** — LLM picks which agent key(s) the question needs (JSON array). Falls back to keyword match, then first agent.
- **gate** — optional human-in-the-loop approval (`HALO_HITL=1`). Auto-approves when off.
- **fan_out** — calls each routed agent **in parallel** (`asyncio.gather`) as the user. Appends to `results` (additive reducer, so parallel writes are safe). On a retry, folds `critique` into the question.
- **synthesize** — 1 result → passthrough; N results → LLM merge.
- **validate** — LLM judge returns `{"verdict":"pass"}` or `{"verdict":"retry","critique":"…"}`. Bounded by `MAX_RETRIES`; loops back to `fan_out`.
- **checkpointer** — `MemorySaver` gives per-thread state (memory / resume / time-travel).

State object: `HaloState` (TypedDict). Key fields: `question`, `user_token`,
`route`, `results` (Annotated[list, add]), `answer`, `verdict`, `critique`,
`attempts`, `trace` (Annotated[list, add]).

---

## Auth model (IMPORTANT — don't redesign this)

**Fabric Data Agents require a USER token.** A service principal / managed
identity token is rejected (401). This constraint drives everything.

HALO is **hosted**, so there's no local browser → it uses the **auth-code redirect,
confidential-client** flow (NOT device code, NOT interactive popup — those are for
other hosts):

- `/auth/login` builds the Entra authorize URL (`initiate_auth_code_flow`) and redirects.
- User signs in at Microsoft; Microsoft redirects to `/auth/callback?code=&state=`.
- Server exchanges the code (`acquire_token_by_auth_code_flow`) using the **client secret** (confidential client) → user's Fabric token.
- Token cache is serialized into the **session cookie**; `_token_from_session` refreshes silently.

Required env (all in the **Fabric tenant**):
`HALO_CLIENT_ID`, `HALO_TENANT`, `HALO_CLIENT_SECRET`,
`HALO_REDIRECT_URI` (= `https://<host>/auth/callback`, **exact match** to the Entra
Web redirect URI — trailing-slash/exactness matters), `OPENROUTER_API_KEY`,
`HALO_SESSION_SECRET`.

**Cross-tenant note:** users and Fabric are in the **same tenant** here, so no
guest/B2B setup. The *hosting* tenant (Render/Azure) is irrelevant — only the
**auth tenant** (where the app registration + users + Fabric live) matters. If you
ever need external/non-Entra users, that's a different design (guest federation or
App-Owns-Data with per-user effective identity) — do NOT scope data by prompt.

### Auth lessons already learned (from the AXIS build — reuse, don't rediscover)
- Microsoft's sign-in page **refuses to load in an iframe** → the *browser* must hit `/auth/login` at the top level. (On HF this needed `window.top.location`; on a plain FastAPI host the browser navigates normally.)
- Redirect URI must match Entra **exactly** (trailing slash included) or `AADSTS50011`.
- Using the wrong client id (e.g. the Azure CLI default `04b07795-…`) → `AADSTS50011` because that app lacks your redirect URI. Always set `HALO_CLIENT_ID`.
- Client **secret** is the only real credential → keep it a secret (env/secret store), never in the browser. Client id + tenant are public identifiers.
- `AADSTS7000215` = invalid secret (often pasted the secret **ID** not the **Value**, or it expired).

---

## Files

| File | Role |
| --- | --- |
| `server.py` | FastAPI: auth routes, `/api/ask` SSE, `/api/me`, serves `index.html`. |
| `halo_core.py` | The LangGraph graph + `AGENTS` config + node functions. Importable (no `__main__`). |
| `index.html` | Live UI: sign-in gate, question box, animated graph + streaming trace (consumes the SSE). |
| `halo.py` | Standalone CLI version of the graph (same logic, prints trace). Kept for local/CLI runs. |
| `halo_reasoning_visual.html` | Standalone **scripted** visualizer (demo-safe, no backend). Good for slides/offline. |
| `test_local.py` | Local test harness. Spins up fake data agents over real MCP, drives the graph + the SSE app. No cloud. |
| `dev_local.py` | Runs the real UI against those fake agents, so you can work on `index.html` without Entra/Fabric/OpenRouter. |
| `run_local.py` | Runs the real UI against the REAL agents, signing in with your az CLI identity instead of Entra. Loads `.env`. |
| `probe_agents.py` | Checks each configured agent answers, as you, and prints the tool schema it exposes. Run this first when anything looks wrong. |
| `requirements.txt` | Deps. |
| `render.yaml` | Render deploy config. |
| `DEPLOY_RENDER.md` | Step-by-step Render deploy. |
| `README.md` | Feature overview + Foundry-vs-LangGraph comparison. |

Two UIs exist on purpose: `index.html` (real, SSE-driven) and
`halo_reasoning_visual.html` (scripted, dependency-free for a talk). Keep both.

---

## Routing (what actually decides where a question goes)

`classify` has three tiers, and the trace always names which one fired:

1. **Model.** Sees each agent's `description` plus its `excludes`, returns a JSON array of keys.
2. **Keywords** (`keyword_route`). Stem overlap against `description` only. This runs whenever
   the model is unreachable, which is not rare, so treat it as a first-class path.
3. **First agent**, labelled `guess`.

Both tiers score 21/21 on the cases pinned in `test_local.py`. Things that broke routing and
are now guarded:

- **Generic measure words tie unrelated domains.** "churn *rate*" matched a manufacturing agent
  on "scrap *rate*". `rate`, `total`, `growth`, `month` and friends are stopwords.
- **A plan name that is also an English word.** CloudMetrics has a plan literally called
  `business`, so "the olist *business*" routed to CloudMetrics. `business` is a stopword.
- **Negations backfire in a bag of words.** Writing "excludes customer data" in a `description`
  indexes "customer" *against* that agent. That is why `excludes` is a separate field that only
  the model sees.
- **Comparison cues must fan out.** Without treating "compare X against Y" specially, the
  cross-domain question HALO exists for collapses to whichever domain got more words.

The same noun means different things per agent (`revenue`, `sales`, `customer`, `products`), so
descriptions have to name the domain, not just the metric.

## Adding / changing data agents

Edit `AGENTS` in `halo_core.py`:
```python
AGENTS = {
  "manufacturing": {"workspace_id": ..., "data_agent_id": ..., "description": "…"},
  # add more; keep descriptions crisp and disjoint — the classifier routes on them
}
```
Values can come from env (`MFG_WS`/`MFG_DA`, `ECOM_WS`/`ECOM_DA`, …). If you add
agents, also add their nodes to the graph layout in `index.html` (`N` object +
`NODEMAP`/route-key mapping) so they render. The visual currently has slots for
`mfg`/`ecom`/`fin`; generalize if there are more.

MCP URL shape (host-agnostic; `api.fabric.microsoft.com` or `msitapi…`):
`https://<host>/v1/mcp/workspaces/<wsid>/dataagents/<daid>/agent`
There's a URL parser in the AXIS app if you want to accept a pasted URL instead of GUIDs.

---

## Conventions / constraints

- **Model**: OpenRouter (OpenAI-compatible) for classify/synthesize/validate. Default a free model; any id via `HALO_MODEL`. These are light reasoning tasks — don't over-spec the model.
- **The data agent does the heavy lifting** (NL→SQL/DAX, RLS). HALO orchestrates: it **routes and stitches, it does NOT join across agents.** Cross-domain synthesis merges *returned values*, not underlying tables. Keep this honest in any UI copy.
- **Async**: nodes that call Fabric are async; blocking calls (MSAL, requests) should run in threads if added to async paths. (This bit us on Gradio 6 SSR in AXIS — same discipline here.)
- **mcp version drift**: `mcp` 2.x renamed `streamablehttp_client` to `streamable_http_client`, dropped its
  `headers=` kwarg (you pass a pre-built `httpx2` client instead), now yields 2 streams instead of 3, and renamed
  `Tool.inputSchema` to `input_schema`. `call_data_agent` detects which API it has and branches. Keep that, and if
  you touch it run `python test_local.py` on both a 1.x and a 2.x install: the old code imported fine on 2.x and
  only blew up on the first real question, which meant a green deploy that could not answer anything.
- **Free-tier hosting**: Render free sleeps after inactivity (~30–60s cold start). Warm before a demo, or use Starter ($7/mo) for always-on.
- **Don't** introduce localStorage/sessionStorage in the UI if it's ever moved into a constrained artifact host; here (real web app) normal browser storage is fine, but state is currently in-memory/session by design.

---

## What "done/working" looks like

- Sign in → `/api/me` returns `signed_in: true` + your UPN.
- Ask a single-domain question → one agent node lights, trace shows route + result, verdict pass, final answer.
- Ask a cross-domain question → two+ agents light **in parallel**, synthesize merges, answer cites both.
- Ask something the judge dislikes → **validate → retry** loops back to fan_out (salmon), second pass passes.
- Different users signing in → different rows (RLS) for the same question. (Needs a second real user to prove.)

`python test_local.py` covers all of that except the last line. It runs two throwaway
MCP servers that impersonate data agents, so the MCP handshake, tool discovery, bearer
propagation, parallel fan-out, the retry cycle, the SSE encoding and the auth guard are
all the real code paths. Only the Fabric service is faked. `python dev_local.py` serves
the same setup through the real UI at `http://127.0.0.1:8930`.

What still needs the cloud: real RLS behaviour, real Entra sign-in, and whether the
routing/judge prompts actually work against a real model. Those are exercised on deploy.

---

## Known issues in the agents themselves (not HALO)

- **ecommerce (Olist) has no data-range guard.** The data spans 2016-09-04 to 2018-09-03 and
  holds 98,666 orders, but asked "how is the olist business doing?" the agent returns *1 order*,
  because it applies a recent-period default relative to today and lands in an empty window.
  CloudMetrics avoids this with an explicit `## DATA RANGE` section telling it to use the most
  recent period *with data* rather than `GETDATE()`. The ecommerce agent needs the same, added
  in Fabric. This is not fixable from HALO and no amount of routing work hides it.

## Open items / next steps

- Add a DATA RANGE section to the ecommerce agent (see above). Until then its answers are empty
  or near-empty for any question without an explicit period.
- Optional: human-in-the-loop gate surfaced in the web UI (currently console-only for CLI).
- Optional: LangSmith tracing for full observability.
- Production hardening: move secret to a secret store; on Azure use managed identity / workload identity federation (secretless) — the pattern Prism uses.
