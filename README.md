# HALO

**Hierarchical Agent Loop Orchestrator.** A LangGraph orchestrator that routes a
question across several Microsoft Fabric data agents, calls them in parallel,
merges what comes back, and judges its own answer, retrying when the answer does
not hold up. It streams the whole thing live, so you watch the graph think.

Every data agent call runs under the signed-in user's token, so Fabric applies
that person's row-level security. HALO never reads data with a service identity.

```
START -> classify -> gate -> fan_out -> synthesize -> validate -+- pass -> END
              |         ^                              |
     no domain matches  +--------- retry (cycle) ------+
              |
         out_of_scope -> END
```

## What it does that a single agent cannot

- **Routes across domains.** A classifier picks the agent or agents a question
  needs, from descriptions you write. It can also pick none, and say so, without
  spending a query.
- **Asks in parallel.** Several agents at once, each told it owns only part of
  the question so it reports what it has instead of inventing a proxy for what it
  does not.
- **Checks its own work.** A judge can send the whole thing round again with a
  critique attached. Bounded by `HALO_MAX_RETRIES`.
- **Degrades instead of failing.** No model key means keyword routing,
  concatenated synthesis and auto-pass. The trace says which happened.
- **Shows the seams.** Cross-domain answers place each domain's figures side by
  side with its own time period. HALO stitches, it does not join.

## Quick start

```
pip install -r requirements.txt
cp agents.example.json agents.json     # then edit it, see below
az login
python probe_agents.py                 # check your agents answer
python run_local.py                    # open http://127.0.0.1:8000
```

`run_local.py` signs you in with your az CLI identity, so you can use the real
app against real agents before setting up any app registration. For hosting, see
DEPLOY_RENDER.md.

No cloud at all:

```
python test_local.py    # 61 checks against throwaway MCP servers
python dev_local.py     # the real UI against fake agents, for working on index.html
```

## Pointing it at your agents

Copy `agents.example.json` to `agents.json` and describe your own. Nothing about
a particular deployment lives in the code.

```json
{
  "examples": ["What was OEE by plant?", "Compare orders with production"],
  "agents": {
    "manufacturing": {
      "url": "https://api.fabric.microsoft.com/v1/mcp/workspaces/<ws-guid>/dataagents/<agent-guid>/agent",
      "description": "Plants, production lines, OEE, scrap, downtime and inventory.",
      "excludes": "No customers, vendors or employees."
    }
  }
}
```

Paste the whole MCP URL from the data agent's Endpoints page; the GUIDs are
parsed out of it. Add as many agents as you like, the graph in the UI lays itself
out to fit. `agents.json` is gitignored; when hosting, put the same JSON in the
`HALO_AGENTS` environment variable so your GUIDs never enter the repo.

### Writing descriptions that route correctly

The classifier sees `description` and nothing else, so this is the single field
that decides whether questions land in the right place. Every rule below comes
from a real misroute:

1. **Name concrete nouns**, the words a user would actually type: plants,
   invoices, sellers, ZIP codes. Not "metrics", "insights" or "data".
2. **Make them disjoint.** If two agents both say "revenue", say whose. Recurring
   subscription revenue and physical product revenue are different things and the
   classifier cannot guess which you meant.
3. **Put definitions in `description`, exclusions in `excludes`.** Writing
   "turbomachinery never includes motors" as an exclusion read to the classifier
   as "holds no motor data", and questions about motors were refused outright. A
   definition is not a scope exclusion.
4. **Never phrase a negative in `description`.** The no-model keyword fallback is
   a bag of words, so "no customer data" indexes *customer* against that agent
   and causes the exact misroute the sentence was meant to prevent. That is why
   `excludes` is a separate field only the model sees.
5. **Avoid words that are generic in one domain and specific in another.** A plan
   named "business" made "the olist business" match a SaaS agent. Generic measure
   words (rate, total, growth, month) are already stopwords for this reason.
6. **State the data range** if it is unusual, so cross-domain answers can be
   honest about differing periods.

Write these from `python probe_agents.py` output rather than from memory. It
prints what each agent actually exposes, and rerunning it after a change in
Fabric is the cheapest way to keep the descriptions true.

## Configuration

| Variable | Meaning |
| --- | --- |
| `HALO_AGENTS` | the agent catalogue as JSON, for hosts where a file is awkward |
| `HALO_AGENTS_FILE` | path to the catalogue, if not `agents.json` |
| `HALO_CLIENT_ID`, `HALO_TENANT`, `HALO_CLIENT_SECRET`, `HALO_REDIRECT_URI` | Entra app registration, for hosted sign-in |
| `HALO_SESSION_SECRET` | cookie signing key |
| `OPENROUTER_API_KEY` | model key. Free models still need one; there is no anonymous tier |
| `HALO_LLM_BASE_URL` | any OpenAI-compatible endpoint. Defaults to OpenRouter; a localhost URL needs no key |
| `HALO_MODEL` | default model id. The UI can override it per question |
| `HALO_MAX_RETRIES` | how many times the judge may send an answer back. Default 1 |
| `HALO_AGENT_TIMEOUT` | seconds to wait on one data agent call. Default 300 |
| `HALO_SSE_HEARTBEAT` | seconds between keepalive frames. Default 15 |
| `HALO_HITL` | `1` to require console approval before any data is touched |
| `FABRIC_HOST` | `api.fabric.microsoft.com`, or another Fabric host |

Local runs read a `.env` beside the code. `run_local.py` lets `.env` win over the
environment, because the file you just edited should be the one that takes
effect. `server.py` does the opposite, because on a host the platform's own
configuration must never be overridden by a deployed file. Both say so at
startup when the two disagree.

## Files

| File | Role |
| --- | --- |
| `halo_core.py` | the graph, the classifier, the agent catalogue loader |
| `server.py` | FastAPI: Entra sign-in, the SSE endpoint, serves the UI |
| `index.html` | live UI: graph, streaming trace, timings, model picker, About |
| `agents.example.json` | template catalogue, with the guidance above |
| `run_local.py` | real agents, your az CLI identity, no app registration needed |
| `dev_local.py` | fake agents and a fake user, for UI work |
| `probe_agents.py` | check the agents answer and print what they expose |
| `test_local.py` | 61 checks, no cloud |
| `NEXT_STEPS.md` | deployment checklist |
| `DEPLOY_RENDER.md` | hosting on Render |
| `AGENTS.md` | design notes and the reasoning behind them |

## Honest limits

- **HALO stitches, it does not join.** Cross-domain answers put each domain's
  figures side by side. There is no row-level join and no cross-domain
  arithmetic, by design, because the agents sit over separate models with
  separate security.
- **Different agents carry different data ranges** and different default periods.
  A combined answer states each period rather than implying a like-for-like
  comparison.
- **Session state is in memory.** The token cache and the graph checkpointer both
  live in the process, so a restart signs everyone out and it assumes one worker.
  Move both to Redis together to scale out.
- **The data agents do the real work.** NL to SQL, the semantic model, the
  security. HALO decides who to ask and checks what comes back.
