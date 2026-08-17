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
import asyncio
import secrets as _secrets
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware
from msal import ConfidentialClientApplication, SerializableTokenCache

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
    request.session["cache"] = cache.serialize()
    request.session["who"] = who
    return RedirectResponse("/")


@app.get("/auth/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/")


def _token_from_session(request: Request) -> Optional[str]:
    blob = request.session.get("cache")
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
        request.session["cache"] = cache.serialize()
    return res.get("access_token") if res else None


# --------------------------------------------------------------------------- #
# Run the graph, stream real trace events over SSE
# --------------------------------------------------------------------------- #
@app.get("/api/ask")
async def api_ask(request: Request, q: str, thread: str = "web"):
    token = _token_from_session(request)
    if not token:
        return JSONResponse({"error": "not_signed_in"}, status_code=401)

    async def events():
        # emit an event per node as the graph streams state updates
        state_in = {"question": q, "user_token": token, "attempts": 0}
        config = {"configurable": {"thread_id": thread}}
        last_answer = ""
        try:
            async for update in _graph.astream(state_in, config, stream_mode="updates"):
                # update = {node_name: partial_state}
                for node, partial in update.items():
                    payload = {"node": node}
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
                    yield f"data: {json.dumps(payload)}\n\n"
            yield f"data: {json.dumps({'node': 'done', 'answer': last_answer})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'node': 'error', 'trace': str(e)})}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


@app.get("/api/me")
def me(request: Request):
    return {"who": request.session.get("who"), "signed_in": bool(request.session.get("cache")),
            "agents": {k: v["description"] for k, v in core.AGENTS.items()}}


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
