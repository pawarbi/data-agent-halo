"""
HALO — Hierarchical Agent Loop Orchestrator
===========================================
A LangGraph orchestrator over multiple Microsoft Fabric Data Agents.

It shows the things managed orchestration hides: an explicit classifier, parallel
fan-out to several data agents, a synthesis step, a *validation loop* (a real
cycle that can retry), and an optional human-in-the-loop approval gate — all with
per-thread checkpointed memory. Every data-agent call runs under the signed-in
user's identity, so Fabric enforces per-user RLS/CLS.

This is a sibling to AXIS (single-agent console) and Prism (portal). HALO is the
multi-agent brain.

Graph shape:

    START
      │
      ▼
   classify ──────────────► (route)
      │                       │
      ▼                       ▼
   [gate?] ── approve ──► fan_out ──► (one or many) agent nodes ──► synthesize
      │                                                                 │
      ▼                                                                 ▼
    reject                                                          validate
                                                                    │      │
                                                              pass  │      │ retry (loop)
                                                                    ▼      └──► fan_out
                                                                   END

Configure your agents in AGENTS below (name, workspace_id, data_agent_id, and a
one-line description the classifier uses to route). Add as many as you have.

Run (standalone, prints a trace for sample questions):
    pip install -r requirements.txt
    az login   # your identity; needs access to the data agents + sources
    python halo.py
"""

from __future__ import annotations

import os
import re
import json
import time
import inspect
import asyncio
import functools
from contextlib import asynccontextmanager
from typing import TypedDict, Annotated, Optional
from operator import add

import requests
from mcp import ClientSession
try:
    from mcp.client.streamable_http import streamablehttp_client
except ImportError:  # newer mcp renamed it
    from mcp.client.streamable_http import streamable_http_client as streamablehttp_client

# mcp 2.x also dropped the `headers` kwarg (you pass a pre-configured httpx client
# instead) and yields 2 streams rather than 3. Detect once, branch in call_data_agent.
_CLIENT_TAKES_HEADERS = "headers" in inspect.signature(streamablehttp_client).parameters

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
from langgraph.config import get_stream_writer


# --------------------------------------------------------------------------- #
# Progress events.
#
# `updates` streaming only reports a node once it has finished, which leaves the
# UI silent for the 30-90s a data agent takes. These push finer-grained events
# out of the running graph: node start, per-agent start and finish, and model
# calls. No-op when nothing is streaming, so the CLI and tests are unaffected.
# --------------------------------------------------------------------------- #
def emit(**payload) -> None:
    try:
        writer = get_stream_writer()
    except Exception:
        return
    if writer:
        try:
            writer(payload)
        except Exception:
            pass


def instrument(name: str, fn):
    """Wrap a node so it announces when it starts and how long it took."""
    if asyncio.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def awrapped(state):
            emit(ev="node_start", node=name)
            t0 = time.perf_counter()
            out = await fn(state)
            emit(ev="node_end", node=name, ms=round((time.perf_counter() - t0) * 1000))
            return out
        return awrapped

    @functools.wraps(fn)
    def wrapped(state):
        emit(ev="node_start", node=name)
        t0 = time.perf_counter()
        out = fn(state)
        emit(ev="node_end", node=name, ms=round((time.perf_counter() - t0) * 1000))
        return out
    return wrapped

# --------------------------------------------------------------------------- #
# Configuration — add as many data agents as you have.
# Each needs: a short key, the workspace + data agent GUIDs, and a description
# the classifier uses to decide routing. Keep descriptions crisp and disjoint.
# --------------------------------------------------------------------------- #
FABRIC_HOST = os.environ.get("FABRIC_HOST", "api.fabric.microsoft.com")
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"

_AGENT_URL = re.compile(
    r"/workspaces/(?P<ws>[0-9a-fA-F-]{36})/dataagents/(?P<da>[0-9a-fA-F-]{36})"
)


def agent(url: str, description: str, excludes: str = "") -> dict:
    """Build an agent entry from the URL Fabric shows you, or from bare GUIDs.

    Paste the whole thing:
      https://<host>/v1/mcp/workspaces/<ws-guid>/dataagents/<agent-guid>/agent

    `description` is what the agent DOES hold, phrased positively. `excludes` is
    what it does not, and is deliberately kept out of `description`: the keyword
    fallback is bag-of-words, so writing "excludes customer data" in the
    description would index the word "customer" against this agent and cause the
    exact misroute the sentence was meant to prevent. Only the model sees
    `excludes`.
    """
    m = _AGENT_URL.search(url)
    if not m:
        raise ValueError(
            f"not a Fabric data agent URL (expected .../workspaces/<guid>/dataagents/<guid>/agent): {url}"
        )
    return {"workspace_id": m["ws"], "data_agent_id": m["da"],
            "description": description, "excludes": excludes}


# The classifier routes on `description` alone, so these are load-bearing. Two
# rules, both learned the hard way:
#
#   1. Make them disjoint. "revenue" means subscription revenue to one of these
#      agents and physical product revenue to the other, so say which.
#   2. Name the concrete nouns, not the abstractions. When no model is reachable,
#      `classify` falls back to stem overlap against this text, so the specific
#      terms a user would actually type are what make routing work.
#
# These were written from `python probe_agents.py` output, not guessed. Rerun it
# after changing an agent in Fabric.
AGENTS: dict[str, dict] = {
    "cloudmetrics": agent(
        os.environ.get(
            "CLOUDMETRICS_URL",
            "https://api.fabric.microsoft.com/v1/mcp/workspaces/"
            "00000000-0000-0000-0000-000000000000/dataagents/"
            "11111111-1111-1111-1111-111111111111/agent",
        ),
        "CloudMetrics B2B SaaS platform. Subscriptions and plans (starter, professional, "
        "business, enterprise), seats and users, companies, accounts and clients, MRR, "
        "ARR, NRR, churn and retention, invoices, billed and collected amounts, and "
        "support tickets including time to first response and mean time to resolution. "
        "This is the source for recurring software revenue. Data covers 2021-07 to "
        "2025-02; the latest complete month is 2024-12.",
        excludes="Anything on a factory floor, and any revenue from physical goods. "
                 "No plants, lines, machines, OEE, scrap, yield, downtime or inventory.",
    ),
    "manufacturing": agent(
        os.environ.get(
            "MANUFACTURING_URL",
            "https://api.fabric.microsoft.com/v1/mcp/workspaces/"
            "22222222-2222-2222-2222-222222222222/dataagents/"
            "33333333-3333-3333-3333-333333333333/agent",
        ),
        "Manufacturing operations. Plants, manufacturing lines, machinery and assets, "
        "production quantity, OEE, scrap rate, production yield, downtime minutes and "
        "downtime reasons or root causes, and inventory. Also physical product sales: "
        "units sold, sales revenue, cost and margin from the OpsRefData lakehouse. "
        "Turbomachinery here covers Pumps and Turbines. Defaults to the latest 30 days "
        "when no period is given.",
        excludes="Turbomachinery never includes Motors. No customer-level data, no "
                 "vendors, purchasing or purchase orders, no employees, and nothing "
                 "about software subscriptions, seats or support tickets.",
    ),
    "ecommerce": agent(
        os.environ.get(
            "ECOMMERCE_URL",
            "https://api.fabric.microsoft.com/v1/mcp/workspaces/"
            "44444444-4444-4444-4444-444444444444/dataagents/"
            "55555555-5555-5555-5555-555555555555/agent",
        ),
        "Brazilian online marketplace, the Olist dataset. Orders and order line items, "
        "marketplace sellers, online shoppers, catalogue products and categories, "
        "freight and delivery, and buyer geography by Brazilian state and ZIP code. "
        "This is the source for e-commerce and online retail orders. Sales here are at "
        "line-item grain, so an order spans several rows.",
        excludes="Nothing about software subscriptions, seats, MRR or support tickets, "
                 "and nothing from the factory: no plants, lines, OEE, scrap, yield, "
                 "downtime or production. Not SAP product sales either.",
    ),
    # Add more the same way — the UI lays out however many there are:
    # "finance": agent(os.environ.get("FINANCE_URL", "https://…/agent"),
    #                  "Budgets, GL accounts, cost centres, financial close."),
}

# Model used for classify / synthesize / validate. Any OpenAI-compatible endpoint
# works, so this is not tied to OpenRouter:
#   OpenRouter (default)  HALO_LLM_BASE_URL=https://openrouter.ai/api/v1   + a key
#   Ollama, local         HALO_LLM_BASE_URL=http://localhost:11434/v1      no key
#   Azure OpenAI          HALO_LLM_BASE_URL=https://<res>.openai.azure.com/openai/v1
# OpenRouter's ":free" models are free of charge but still require a key; there is
# no anonymous tier.
LLM_BASE_URL = os.environ.get("HALO_LLM_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
LLM_KEY = os.environ.get("OPENROUTER_API_KEY", "") or os.environ.get("HALO_LLM_KEY", "")
OPENROUTER_KEY = LLM_KEY   # kept for anything still reading the old name
MODEL = os.environ.get("HALO_MODEL", "nvidia/nemotron-3-ultra-550b-a55b:free")

# Hosted endpoints need a key; something on localhost generally does not.
LLM_NEEDS_KEY = not re.match(r"https?://(localhost|127\.0\.0\.1|\[::1\])", LLM_BASE_URL)

MAX_RETRIES = int(os.environ.get("HALO_MAX_RETRIES", "1"))
HUMAN_IN_THE_LOOP = os.environ.get("HALO_HITL", "0") == "1"

# Seconds to wait on a single data-agent call. Real ones routinely take 30-90s.
AGENT_TIMEOUT = float(os.environ.get("HALO_AGENT_TIMEOUT", "300"))


# --------------------------------------------------------------------------- #
# Graph state — the object that flows through every node.
# `results` uses an additive reducer so parallel agent nodes can each append.
# --------------------------------------------------------------------------- #
def merge_results(old: list[dict] | None, new: list[dict] | None) -> list[dict]:
    """Additive so parallel fan-out writes are safe; `None` clears.

    validate sends None on a retry so the rejected first-pass answers don't get
    carried into the second synthesize (which would merge the very answer the
    judge just rejected back into the final one).
    """
    if new is None:
        return []
    return (old or []) + list(new)


class HaloState(TypedDict, total=False):
    question: str
    user_token: str                       # the signed-in user's Fabric token
    route: list[str]                       # which agent keys to call
    results: Annotated[list[dict], merge_results]   # [{agent, answer}] — parallel-safe
    answer: str                            # synthesized final answer
    verdict: str                           # "pass" | "retry"
    critique: str                          # validator feedback for a retry
    attempts: int
    approved: Optional[bool]               # human-in-the-loop decision
    trace: Annotated[list[str], add]       # human-readable step log


# --------------------------------------------------------------------------- #
# LLM helper (OpenRouter, OpenAI-compatible)
# --------------------------------------------------------------------------- #
# Why the last model call produced nothing. Surfaced in the trace so a degraded
# run is visible rather than silent.
llm_status: dict[str, str] = {"reason": ""}


def llm(system: str, user: str, temperature: float = 0.0) -> str:
    """Ask the model. Returns "" on any failure rather than raising.

    Every caller already has a non-model path: classify falls back to keyword
    routing, synthesize concatenates the blocks, validate passes. Letting an
    expired key or a bad model id raise here takes down the whole graph on a
    question the data agents could have answered perfectly well, so this
    swallows the error and records why.
    """
    if LLM_NEEDS_KEY and not LLM_KEY:
        llm_status["reason"] = f"no API key set for {LLM_BASE_URL}"
        return ""
    headers = {"Content-Type": "application/json"}
    if LLM_KEY:
        headers["Authorization"] = f"Bearer {LLM_KEY}"
    try:
        r = requests.post(
            f"{LLM_BASE_URL}/chat/completions",
            headers=headers,
            json={
                "model": MODEL,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}],
                "temperature": temperature,
            },
            timeout=60,
        )
        if r.status_code == 401:
            llm_status["reason"] = f"{LLM_BASE_URL} rejected the key (401) — the API key is invalid or expired"
            return ""
        if r.status_code == 402:
            llm_status["reason"] = f"out of credit (402) for model {MODEL}"
            return ""
        if r.status_code == 404:
            llm_status["reason"] = f"{LLM_BASE_URL} does not know model {MODEL!r} (404) — check HALO_MODEL"
            return ""
        if r.status_code == 429:
            llm_status["reason"] = f"rate-limited on model {MODEL} (429)"
            return ""
        r.raise_for_status()
        body = r.json()
        content = body["choices"][0]["message"]["content"].strip()
        usage = body.get("usage") or {}
        emit(ev="llm", model=MODEL,
             prompt_tokens=usage.get("prompt_tokens", 0),
             completion_tokens=usage.get("completion_tokens", 0))
        llm_status["reason"] = "" if content else "model returned an empty message"
        return content
    except Exception as e:
        llm_status["reason"] = f"{type(e).__name__}: {e}"
        return ""


# --------------------------------------------------------------------------- #
# The one real Fabric call — a data agent over MCP, as the user.
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def _mcp_session(url: str, headers: dict):
    """Open an MCP session over streamable HTTP, on mcp 1.x or 2.x.

    1.x: client takes `headers=`, yields (read, write, get_session_id), and wants
         a timedelta for the session read timeout.
    2.x: client takes a pre-built httpx client, yields (read, write), and wants
         a float.

    Timeouts matter more than they look. A data agent runs NL->SQL/DAX and can
    sit there for minutes; httpx defaults to a 5s read timeout, which kills the
    call mid-think and surfaces as a bare ExceptionGroup.
    """
    if _CLIENT_TAKES_HEADERS:
        from datetime import timedelta
        async with streamablehttp_client(
            url, headers=headers, timeout=AGENT_TIMEOUT, sse_read_timeout=AGENT_TIMEOUT
        ) as streams:
            async with ClientSession(
                streams[0], streams[1],
                read_timeout_seconds=timedelta(seconds=AGENT_TIMEOUT),
            ) as session:
                await session.initialize()
                yield session
    else:
        import httpx2
        async with httpx2.AsyncClient(headers=headers, timeout=AGENT_TIMEOUT) as http_client:
            async with streamablehttp_client(url, http_client=http_client) as streams:
                async with ClientSession(
                    streams[0], streams[1], read_timeout_seconds=AGENT_TIMEOUT
                ) as session:
                    await session.initialize()
                    yield session


def describe_error(e: BaseException) -> str:
    """Flatten anyio's ExceptionGroup to the error that actually happened.

    The MCP client runs its transport in a task group, so a plain timeout or 401
    arrives as `ExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)`
    with the real cause nested inside. That string ends up in the answer and in
    the UI trace, where it tells you nothing.
    """
    seen, out = set(), []

    def walk(err: BaseException) -> None:
        if id(err) in seen:
            return
        seen.add(id(err))
        subs = getattr(err, "exceptions", None)
        if subs:
            for sub in subs:
                walk(sub)
            return
        label = type(err).__name__
        detail = str(err).strip()
        out.append(f"{label}: {detail}" if detail else label)
        if err.__cause__ is not None:
            walk(err.__cause__)

    walk(e)
    if not out:
        return f"{type(e).__name__}: {e}"
    msg = "; ".join(dict.fromkeys(out))
    if "ReadTimeout" in msg or "TimeoutError" in msg:
        msg += f" (no response within HALO_AGENT_TIMEOUT={AGENT_TIMEOUT:.0f}s)"
    return msg


def _tool_schema(tool) -> dict:
    # renamed inputSchema -> input_schema in mcp 2.x
    return getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None) or {}


async def call_data_agent(token: str, workspace_id: str, data_agent_id: str, question: str) -> str:
    url = f"https://{FABRIC_HOST}/v1/mcp/workspaces/{workspace_id}/dataagents/{data_agent_id}/agent"
    headers = {"Authorization": f"Bearer {token}"}
    async with _mcp_session(url, headers) as session:
        tools = await session.list_tools()
        if not tools.tools:
            return "(no tools exposed — is the agent published?)"
        tool = tools.tools[0]
        arg = "question"
        props = _tool_schema(tool).get("properties", {})
        if props and "question" not in props:
            arg = next(iter(props))
        result = await session.call_tool(tool.name, {arg: question})
        parts = [getattr(b, "text", "") for b in (getattr(result, "content", []) or [])]
        return "\n".join(p for p in parts if p).strip() or "(empty response)"


# --------------------------------------------------------------------------- #
# Keyword routing — the fallback when the model is unavailable.
#
# This runs whenever OPENROUTER_API_KEY is missing or the call fails, so it is
# not a rarely-taken path. It scores each agent by how many distinct word stems
# its description shares with the question, and returns every agent tied at the
# top, so a genuinely cross-domain question still fans out.
# --------------------------------------------------------------------------- #
_STOPWORDS = frozenset("""
all and any are but can did does for from get had has have here how into its many
much not our out over per plus said say than that the their them then there these
they this those was were what when where which who why will with you your
covers excludes never use used using
""".split()) | frozenset("""
amount average count current daily figure figures growth latest level levels metric
metrics month monthly number percent percentage period quarter quarterly rate rates
recent report show split told total totals trend value values week weekly year yearly
""".split()) | frozenset("""
business company data doing performance platform source
""".split())
# "business" is deliberately here even though it is also a CloudMetrics plan name:
# as a routing token it is pure noise, and without this "the olist business" matches
# CloudMetrics on the plan name alone.

# Words that say "this question spans things" rather than naming a domain. When one
# of these appears and more than one agent matched at all, fan out to all of them.
_CROSS_CUES = frozenset("""
against alongside between compare compared comparison correlate correlated correlation
relationship versus vs
""".split())


def _stem(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("es") and len(word) > 4:
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


def _stems(text: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[a-z]{3,}", text.lower()) if w not in _STOPWORDS}


def keyword_route(question: str) -> list[str]:
    """Agents whose description overlaps the question most. Empty if none do.

    Generic measure words (rate, total, growth, month...) are stopwords, because
    they otherwise tie unrelated domains together: "churn rate" would match a
    manufacturing agent purely on "scrap rate".

    A comparison cue ("compare X against Y") means the question is asking across
    domains, so every agent with any match is returned rather than only the top
    scorer. Without that, the phrasing HALO exists to serve collapses to whichever
    domain happens to be named with more words.
    """
    asked = _stems(question)
    scores = {k: len(asked & _stems(v["description"])) for k, v in AGENTS.items()}
    matched = [k for k, s in scores.items() if s > 0]
    if not matched:
        return []
    if len(matched) > 1 and (_CROSS_CUES & set(re.findall(r"[a-z]{2,}", question.lower()))):
        return matched
    best = max(scores.values())
    return [k for k, s in scores.items() if s == best]


# --------------------------------------------------------------------------- #
# NODES
# --------------------------------------------------------------------------- #
def classify(state: HaloState) -> dict:
    """Decide which agent(s) should answer. Returns one or more agent keys."""
    catalog = "\n".join(
        f"- {k}: {v['description']}"
        + (f"\n    Does NOT cover: {v['excludes']}" if v.get("excludes") else "")
        for k, v in AGENTS.items()
    )
    sys = (
        "You route a question to data agents. Choose every agent whose domain the "
        "question needs, and only those. If it genuinely spans domains, return "
        "several; if one agent covers it, return just that one. The same word can "
        "mean different things to different agents (revenue, sales, customer), so "
        "match on the domain the question is really about, not on a shared keyword. "
        "Reply with ONLY a JSON array of agent keys from this list, nothing else."
        "\n\n" + catalog
    )
    raw = llm(sys, state["question"])
    route: list[str] = []
    how = "model"
    try:
        route = [k for k in json.loads(re.search(r"\[.*\]", raw, re.S).group()) if k in AGENTS]
    except Exception:
        route = []
    if not route:
        # No key, a model error, or unparseable output. Fall back to keywords.
        route = keyword_route(state["question"])
        how = "keywords"
    if not route:
        route = list(AGENTS)[:1]
        how = "guess"
    why = llm_status["reason"] if how != "model" else ""
    label = f"classify → route={route} (by {how}{': ' + why if why else ''})"
    return {"route": route, "attempts": state.get("attempts", 0), "trace": [label]}


def gate(state: HaloState) -> dict:
    """Optional human-in-the-loop approval before touching data."""
    if not HUMAN_IN_THE_LOOP:
        return {"approved": True, "trace": ["gate → auto-approved (HITL off)"]}
    # In a real UI this is an interrupt; here we prompt on the console.
    ans = input(f"\n[HITL] Approve querying {state['route']} for: "
                f"{state['question']!r}? [y/N] ").strip().lower()
    ok = ans == "y"
    return {"approved": ok, "trace": [f"gate → {'approved' if ok else 'REJECTED'}"]}


async def fan_out(state: HaloState) -> dict:
    """Call each routed data agent IN PARALLEL, as the user. Appends to results."""
    token = state["user_token"]
    q = state["question"]
    # If this is a retry, fold the validator's critique into the ask.
    if state.get("critique"):
        q = f"{q}\n\n(Refine: {state['critique']})"

    # On a fan-out, tell each agent it is answering one part of a larger question.
    # Without this, an agent handed a cross-domain question tries to answer all of
    # it, and a good agent will reach for the nearest available column as a proxy
    # for the data it does not have. That reads as a confident cross-domain answer
    # sourced entirely from one domain, which is worse than a partial answer.
    fanning_out = len(state["route"]) > 1
    scope = (
        "\n\nYou are one of several data agents answering this question in parallel. "
        "Answer only the part your own data actually covers, and state which part that "
        "is. Another agent covers the rest, so do not substitute a proxy metric, an "
        "estimate, or a similarly-named column for data you do not hold. If none of the "
        "question falls in your domain, say so plainly.\n"
        "Do still answer your part in full: apply your normal defaults for anything "
        "unspecified, such as the time period, and state what you used, rather than "
        "asking a clarifying question."
    )

    async def one(key: str) -> dict:
        cfg = AGENTS[key]
        ask = f"{q}{scope}" if fanning_out else q
        emit(ev="agent_start", agent=key)
        t0 = time.perf_counter()
        failed = False
        try:
            ans = await call_data_agent(token, cfg["workspace_id"], cfg["data_agent_id"], ask)
        except Exception as e:
            ans = f"(error: {describe_error(e)})"
            failed = True
        ms = round((time.perf_counter() - t0) * 1000)
        emit(ev="agent_end", agent=key, ms=ms, failed=failed, preview=ans[:180])
        return {"agent": key, "answer": ans, "ms": ms}

    t0 = time.perf_counter()
    results = await asyncio.gather(*(one(k) for k in state["route"]))
    wall = round((time.perf_counter() - t0) * 1000)
    serial = sum(r["ms"] for r in results)
    # What the parallelism actually bought. With one agent these are equal.
    emit(ev="fanout_done", wall_ms=wall, serial_ms=serial, agents=len(results))
    return {"results": list(results),
            "attempts": state.get("attempts", 0) + 1,
            "trace": [f"fan_out → called {state['route']} in parallel"]}


def synthesize(state: HaloState) -> dict:
    """Merge the per-agent answers into one grounded response."""
    blocks = "\n\n".join(f"[{r['agent']}]\n{r['answer']}" for r in state["results"])
    if len(state["results"]) == 1:
        answer = state["results"][0]["answer"]
    else:
        sys = ("Combine the per-domain results into one coherent answer to the user's "
               "question. Attribute figures to their domain. Never invent numbers; use "
               "only what the results contain.\n"
               "Each agent reports the time period it used, and those periods are not "
               "always the same: the domains have different data ranges and different "
               "defaults. State the period alongside each figure, and if the periods "
               "differ, say so plainly rather than presenting the numbers as a "
               "like-for-like comparison.\n"
               "These figures come from separate systems. Report them side by side; do "
               "not compute ratios or totals across domains, and do not imply a "
               "row-level join that did not happen.")
        answer = llm(sys, f"Question: {state['question']}\n\nResults:\n{blocks}") or blocks
    return {"answer": answer, "trace": [f"synthesize → merged {len(state['results'])} result(s)"]}


def validate(state: HaloState) -> dict:
    """Judge the answer. Either pass, or request a bounded retry (a real cycle)."""
    if state.get("attempts", 0) > MAX_RETRIES:
        return {"verdict": "pass", "trace": ["validate → max retries reached, accepting"]}
    sys = (
        "You are a strict QA judge. Decide if the answer fully and correctly "
        "addresses the question using grounded data.\n"
        "Know how this system works before you judge it. Each domain is a separate "
        "data agent over a separate source. They are queried independently and their "
        "results are placed side by side. There is no join across them, by design.\n"
        "So do NOT ask for a merged, correlated or like-for-like comparison across "
        "domains, and do not treat differing time periods between domains as a defect "
        "when the answer has disclosed them. Presenting each domain's figure with its "
        "own period, and saying they are not directly comparable, is the correct "
        "outcome for a cross-domain question, not a deficiency.\n"
        "Ask for a retry only when something is genuinely wrong: a domain in scope was "
        "not answered at all, a figure is missing or internally inconsistent, an error "
        "is reported instead of data, or the answer states something the results do not "
        "support.\n"
        'Reply ONLY as JSON: {"verdict":"pass"} or '
        '{"verdict":"retry","critique":"<what to fix>"}.'
    )
    raw = llm(sys, f"Question: {state['question']}\n\nAnswer: {state['answer']}")
    verdict, critique = "pass", ""
    try:
        obj = json.loads(re.search(r"\{.*\}", raw, re.S).group())
        verdict = obj.get("verdict", "pass")
        critique = obj.get("critique", "")
    except Exception:
        verdict = "pass"  # if the judge is unavailable, don't loop forever
    out = {"verdict": verdict, "critique": critique,
           "trace": [f"validate → {verdict}" + (f" ({critique})" if critique else "")]}
    if verdict == "retry":
        out["results"] = None   # drop the rejected pass; fan_out starts clean
    return out


# --------------------------------------------------------------------------- #
# EDGES (routing functions)
# --------------------------------------------------------------------------- #
def after_gate(state: HaloState) -> str:
    return "fan_out" if state.get("approved") else "rejected"


def after_validate(state: HaloState) -> str:
    return "retry" if state.get("verdict") == "retry" else END


def rejected(state: HaloState) -> dict:
    return {"answer": "Request was not approved.", "trace": ["rejected → stopped"]}


# --------------------------------------------------------------------------- #
# BUILD THE GRAPH
# --------------------------------------------------------------------------- #
def build_graph():
    g = StateGraph(HaloState)
    # Wrapped at wiring time rather than by decorating the functions, so the node
    # functions stay directly callable in tests.
    g.add_node("classify", instrument("classify", classify))
    g.add_node("gate", instrument("gate", gate))
    g.add_node("fan_out", instrument("fan_out", fan_out))
    g.add_node("synthesize", instrument("synthesize", synthesize))
    g.add_node("validate", instrument("validate", validate))
    g.add_node("rejected", instrument("rejected", rejected))

    g.add_edge(START, "classify")
    g.add_edge("classify", "gate")
    g.add_conditional_edges("gate", after_gate, {"fan_out": "fan_out", "rejected": "rejected"})
    g.add_edge("fan_out", "synthesize")
    g.add_edge("synthesize", "validate")
    g.add_conditional_edges("validate", after_validate, {"retry": "fan_out", END: END})
    g.add_edge("rejected", END)

    # Checkpointer = durable per-thread memory (conversation, resume, time-travel).
    return g.compile(checkpointer=MemorySaver())


# --------------------------------------------------------------------------- #
