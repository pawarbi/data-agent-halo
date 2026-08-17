"""
HALO server — FastAPI + LangGraph, hosted-ready (Render).

- Redirect auth (Entra auth-code, confidential client) — same pattern as AXIS,
  because a hosted app has no local browser. User signs in → token carries their
  identity → every data-agent call runs as them (RLS/CLS enforced).
- Runs the HALO graph and STREAMS its real trace over SSE, so the browser visual
  animates an actual run (classify → fan-out → synthesize → validate → retry).

Env (Render → Environment):
  HALO_CLIENT_ID        Entra app (client) id            (required)
  HALO_TENANT           Fabric tenant id (GUID)          (required)
  HALO_CLIENT_SECRET    Entra client secret              (required)
  HALO_REDIRECT_URI     https://<app>.onrender.com/auth/callback  (required, exact)
  OPENROUTER_API_KEY    for classify/synthesize/validate (required)
  HALO_MODEL            OpenRouter model id              (optional)
  HALO_SESSION_SECRET   random string for cookie signing (recommended)
  Agents: MFG_WS/MFG_DA, ECOM_WS/ECOM_DA, ...  (or edit AGENTS in halo_core.py)
"""

from __future__ import annotations

import os
import json
import time
import asyncio
import secrets as _secrets
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware
from msal import ConfidentialClientApplication, SerializableTokenCache


def _load_dotenv() -> None:
    """Read a .env next to this file, if there is one. The real environment wins.

    That precedence is the opposite of run_local.py's, deliberately: on a host
    the platform's own environment must never be overridden by a file that
    happened to get deployed.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    shadowed = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip("'\"")
            if not key:
                continue
            if key not in os.environ:
                os.environ[key] = value
            elif os.environ[key] != value:
                shadowed.append(key)
    if shadowed:
        # Silent shadowing is nasty: a stale value in the user environment beats the
        # file you just edited, and the only symptom is a puzzling 401 much later.
        print(f"[config] ignoring .env for {', '.join(shadowed)} — the environment already "
              f"sets a different value, and the environment wins here. Unset it to use .env.")


_load_dotenv()             # before the config below and before halo_core reads env

import halo_core as core   # the graph + agents (refactored from halo.py)

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
CLIENT_ID = os.environ.get("HALO_CLIENT_ID", "").strip()
TENANT = os.environ.get("HALO_TENANT", "organizations").strip()
CLIENT_SECRET = os.environ.get("HALO_CLIENT_SECRET", "").strip()
REDIRECT_URI = os.environ.get("HALO_REDIRECT_URI", "").strip()
SESSION_SECRET = os.environ.get("HALO_SESSION_SECRET", _secrets.token_hex(16))
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"
AUTHORITY = f"https://login.microsoftonline.com/{TENANT}"

app = FastAPI(title="HALO")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax", https_only=False)

_graph = core.build_graph()
_flows: dict[str, dict] = {}   # state -> auth-code flow

# sid -> serialized MSAL token cache.
#
# The cache CANNOT live in the session cookie. A Fabric access token is a large
# JWT, and cache also holds a refresh token and an id token; serialized it runs
# to several KB, past the ~4KB a browser will store. The browser then drops the
# cookie silently, so sign-in appears to succeed (Entra redirects back happily)
# and the user lands on the page still signed out, with nothing in any log to
# say why. The cookie now carries only this opaque id.
#
# In-memory, so a restart signs everyone out and it assumes a single worker —
# the same constraint the graph's MemorySaver checkpointer already has. Move
# both to Redis together if this ever needs to scale out.
_caches: dict[str, str] = {}

# How often to send an SSE comment frame while the graph is busy. Must stay well
# under the shortest idle timeout in front of the app; 15s clears the common 30-60s.
HEARTBEAT_SECONDS = float(os.environ.get("HALO_SSE_HEARTBEAT", "15"))


def _msal(cache: Optional[SerializableTokenCache] = None) -> ConfidentialClientApplication:
    return ConfidentialClientApplication(
        CLIENT_ID, authority=AUTHORITY, client_credential=CLIENT_SECRET,
        token_cache=cache or SerializableTokenCache(),
    )


# --------------------------------------------------------------------------- #
# Auth routes (redirect / auth-code, confidential client)
# --------------------------------------------------------------------------- #
@app.get("/auth/login")
def login(request: Request):
    cache = SerializableTokenCache()
    flow = _msal(cache).initiate_auth_code_flow([FABRIC_SCOPE], redirect_uri=REDIRECT_URI)
    _flows[flow["state"]] = flow
    if len(_flows) > 100:
        for k in list(_flows)[:-100]:
            _flows.pop(k, None)
    return RedirectResponse(flow["auth_uri"])


@app.get("/auth/callback")
def callback(request: Request):
    params = dict(request.query_params)
    state = params.get("state")
    flow = _flows.pop(state, None) if state else None
    if not flow:
        return HTMLResponse("<p>Sign-in expired. <a href='/'>Try again</a>.</p>", status_code=400)
    cache = SerializableTokenCache()
    result = _msal(cache).acquire_token_by_auth_code_flow(flow, params)
    if "access_token" not in result:
        msg = result.get("error_description", "sign-in failed")
        return HTMLResponse(f"<p>{msg}</p><p><a href='/'>Back</a></p>", status_code=400)
    who = ""
    try:
        who = result.get("id_token_claims", {}).get("preferred_username", "")
    except Exception:
        pass
    blob = cache.serialize()
    print(f"[auth] signed in {who}; token cache is {len(blob)} bytes "
          f"({'too big for a cookie' if len(blob) > 3000 else 'cookie-sized'}), "
          f"keeping it server-side")
    sid = _secrets.token_urlsafe(16)
    _caches[sid] = blob
    if len(_caches) > 500:                       # crude bound, oldest first
        for k in list(_caches)[:-500]:
            _caches.pop(k, None)
    request.session["sid"] = sid
    request.session["who"] = who
    return RedirectResponse("/")


@app.get("/auth/logout")
def logout(request: Request):
    _caches.pop(request.session.get("sid", ""), None)
    request.session.clear()
    return RedirectResponse("/")


def _token_from_session(request: Request) -> Optional[str]:
    sid = request.session.get("sid")
    blob = _caches.get(sid) if sid else None
    if not blob:
        return None
    cache = SerializableTokenCache()
    cache.deserialize(blob)
    app_ = _msal(cache)
    accounts = app_.get_accounts()
    if not accounts:
        return None
    res = app_.acquire_token_silent([FABRIC_SCOPE], account=accounts[0])
    if cache.has_state_changed:
        _caches[sid] = cache.serialize()
    return res.get("access_token") if res else None


# --------------------------------------------------------------------------- #
# Run the graph, stream real trace events over SSE
# --------------------------------------------------------------------------- #
_models_cache: dict = {"at": 0.0, "items": []}


@app.get("/api/models")
def api_models():
    """Models the configured endpoint offers, free ones first.

    Only OpenRouter publishes a catalogue we can read without credentials; for any
    other endpoint the UI just lets you type an id.
    """
    if "openrouter.ai" not in core.LLM_BASE_URL:
        return {"current": core.MODEL, "models": [], "note": "type any model id"}
    if time.time() - _models_cache["at"] < 900 and _models_cache["items"]:
        return {"current": core.MODEL, "models": _models_cache["items"]}
    try:
        import requests
        r = requests.get(f"{core.LLM_BASE_URL}/models", timeout=20)
        r.raise_for_status()
        items = []
        for m in r.json().get("data", []):
            mid = m.get("id", "")
            pricing = m.get("pricing") or {}
            free = mid.endswith(":free") or str(pricing.get("prompt", "0")) in ("0", "0.0")
            items.append({"id": mid, "name": m.get("name") or mid, "free": free})
        # Free first, then alphabetical, so the zero-cost options are reachable.
        items.sort(key=lambda m: (not m["free"], m["id"]))
        _models_cache.update(at=time.time(), items=items)
        return {"current": core.MODEL, "models": items}
    except Exception as e:
        return {"current": core.MODEL, "models": [], "error": core.describe_error(e)}


@app.get("/api/ask")
async def api_ask(request: Request, q: str, thread: str = "web", model: str = "",
                  validate: int = 1):
    token = _token_from_session(request)
    if not token:
        return JSONResponse({"error": "not_signed_in"}, status_code=401)

    async def events():
        # Two streams interleaved: "updates" fires once a node completes, "custom"
        # carries the fine-grained progress the nodes emit while they are still
        # running. Without the second, the UI sits silent for the 30-90s a data
        # agent takes to answer.
        state_in = {"question": q, "user_token": token, "attempts": 0,
                    "skip_validate": not validate}
        if model:
            state_in["model"] = model
        config = {"configurable": {"thread_id": thread}}
        last_answer = ""
        t_start = time.perf_counter()

        def elapsed_ms() -> int:
            return round((time.perf_counter() - t_start) * 1000)

        def render(mode, chunk) -> list[dict]:
            """One stream item to zero or more SSE payloads."""
            nonlocal last_answer
            if mode == "custom":
                out = dict(chunk)
                out["elapsed_ms"] = elapsed_ms()
                return [out]
            payloads = []
            for node, partial in chunk.items():        # {node_name: partial_state}
                payload = {"node": node, "elapsed_ms": elapsed_ms()}
                if partial.get("route") is not None:
                    payload["route"] = partial["route"]
                if partial.get("results") is not None:
                    payload["results"] = [
                        {"agent": r.get("agent"), "preview": (r.get("answer") or "")[:180]}
                        for r in partial["results"]
                    ]
                if partial.get("verdict"):
                    payload["verdict"] = partial["verdict"]
                if partial.get("critique"):
                    payload["critique"] = partial["critique"]
                if partial.get("answer"):
                    payload["answer"] = partial["answer"]
                    last_answer = partial["answer"]
                if partial.get("trace"):
                    payload["trace"] = partial["trace"][-1]
                payloads.append(payload)
            return payloads

        # The graph is drained by a task feeding a queue, so this generator can wake
        # on a timer even when the graph has nothing to say. A data agent thinks for
        # 30-90s in one go, which is long enough for a proxy or load balancer to
        # decide an idle connection is dead and close it mid-question. Comment
        # frames keep the connection provably alive; the browser ignores them.
        queue: asyncio.Queue = asyncio.Queue()

        async def produce():
            try:
                async for item in _graph.astream(state_in, config,
                                                 stream_mode=["updates", "custom"]):
                    await queue.put(("item", item))
            except Exception as exc:
                await queue.put(("error", exc))
            finally:
                await queue.put(("end", None))

        pump = asyncio.create_task(produce())
        try:
            while True:
                try:
                    kind, item = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if kind == "end":
                    break
                if kind == "error":
                    failed = {"node": "error", "elapsed_ms": elapsed_ms(),
                              "trace": core.describe_error(item)}
                    yield f"data: {json.dumps(failed)}\n\n"
                    return
                for payload in render(*item):
                    yield f"data: {json.dumps(payload)}\n\n"
            yield (f"data: {json.dumps({'node': 'done', 'answer': last_answer, 'elapsed_ms': elapsed_ms()})}"
                   f"\n\n")
        finally:
            pump.cancel()

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        # no-cache and no-transform stop well-meaning caches touching the stream;
        # X-Accel-Buffering is nginx's opt-out, which several PaaS front ends honour.
        headers={"Cache-Control": "no-cache, no-transform",
                 "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"},
    )


@app.get("/api/me")
def me(request: Request):
    return {"who": request.session.get("who"),
            "signed_in": request.session.get("sid", "") in _caches,
            "agents": {k: v["description"] for k, v in core.AGENTS.items()},
            "examples": core.EXAMPLES}


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
def index():
    # served from the same directory
    here = os.path.dirname(__file__)
    with open(os.path.join(here, "index.html"), encoding="utf-8") as f:
        return f.read()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), reload=False)
