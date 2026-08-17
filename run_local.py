"""
Run HALO locally against the REAL data agents, as your own Azure identity.

For when you want to use the actual app before the Entra app registration exists.
Sign-in here is your az CLI login (or an interactive browser prompt if that is not
available), instead of the hosted auth-code redirect. Everything else is real: the
real agents, real Fabric, real RLS under your identity.

    az login
    python run_local.py                 then open http://127.0.0.1:8000

Add a browser sign-in prompt instead of reusing az:

    python run_local.py --interactive

Set OPENROUTER_API_KEY (in the environment or a .env file next to this script) to
turn on the model-driven classifier, synthesis and validation. Without it those
degrade to keyword routing, concatenation, and auto-pass.

Related runners:
    dev_local.py   fake agents, fake user, no cloud at all. For UI work.
    probe_agents.py  checks the agents answer, without starting a server.

This deliberately does not touch server.py's hosted auth. The production path
stays the confidential-client redirect flow; this file patches the token lookup
for local use only.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
logging.basicConfig(level=logging.WARNING)

PORT = int(os.environ.get("PORT", "8000"))


def load_dotenv() -> list[str]:
    """Read a .env sitting next to this file. Values here OVERRIDE the environment.

    That is the opposite of the usual precedence, on purpose. A stale key left in
    the user environment otherwise silently wins over the one you just put in
    .env, and the only symptom is a 401 from a file that looks correct. For a
    local dev runner, the file you just edited should be the one that takes
    effect. The names overridden are printed at startup so it is never a mystery.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    loaded = []
    if not os.path.exists(path):
        return loaded
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip("'\"")
            if not key:
                continue
            shadowed = key in os.environ and os.environ[key] != value
            os.environ[key] = value
            loaded.append(f"{key} (replacing the one in your environment)" if shadowed else key)
    return loaded


_DOTENV = load_dotenv()   # before importing halo_core, which reads env at import time
import halo_core as core
import server


class LocalIdentity:
    """Your Azure identity, cached and refreshed a little before it expires."""

    def __init__(self, interactive: bool = False):
        self._interactive = interactive
        self._lock = threading.Lock()
        self._token = ""
        self._expires = 0.0
        self.who = ""

    def _credential(self):
        from azure.identity import (AzureCliCredential, ChainedTokenCredential,
                                    InteractiveBrowserCredential)
        if self._interactive:
            return InteractiveBrowserCredential()
        # az login if it is there, a browser prompt if it is not.
        return ChainedTokenCredential(AzureCliCredential(), InteractiveBrowserCredential())

    def refresh(self) -> None:
        token = self._credential().get_token(core.FABRIC_SCOPE)
        with self._lock:
            self._token = token.token
            self._expires = float(token.expires_on)
            self.who = self._upn(token.token)

    @staticmethod
    def _upn(token: str) -> str:
        """Read the unverified JWT payload, only to display who is signed in."""
        import base64, json
        try:
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload))
            return claims.get("upn") or claims.get("unique_name") or claims.get("oid", "signed in")
        except Exception:
            return "signed in"

    def token(self) -> str:
        with self._lock:
            fresh = self._token and time.time() < self._expires - 300
            current = self._token
        if fresh:
            return current
        self.refresh()          # blocks briefly, roughly once an hour
        with self._lock:
            return self._token


def main() -> None:
    interactive = "--interactive" in sys.argv
    identity = LocalIdentity(interactive=interactive)

    if _DOTENV:
        print("from .env: " + ", ".join(_DOTENV))

    print("signing in" + (" (browser)" if interactive else " (az CLI, or browser if needed)"))
    identity.refresh()          # up front, so the first question is not slowed by it
    print(f"signed in as {identity.who}")

    print(f"\nagents ({core.FABRIC_HOST}):")
    for key, cfg in core.AGENTS.items():
        print(f"  {key:15} {cfg['workspace_id']} / {cfg['data_agent_id']}")

    if core.LLM_KEY or not core.LLM_NEEDS_KEY:
        print(f"\nmodel: {core.MODEL} via {core.LLM_BASE_URL}")
        probe = core.llm("Reply with the single word: ok", "ping")
        if probe:
            print("model reachable")
        else:
            print(f"MODEL UNAVAILABLE: {core.llm_status['reason']}")
            print("HALO still runs: keyword routing, concatenated synthesis, auto-pass.")
    else:
        print(f"\nno API key for {core.LLM_BASE_URL}, so classify falls back to keyword")
        print("routing, synthesize concatenates, and validate auto-passes.")

    server._token_from_session = lambda request: identity.token()

    # FastAPI matches in registration order, so drop the hosted /api/me first.
    server.app.router.routes = [
        r for r in server.app.router.routes if getattr(r, "path", None) != "/api/me"
    ]

    @server.app.get("/api/me")
    def me():
        return {"who": identity.who, "signed_in": True,
                "agents": {k: v["description"] for k, v in core.AGENTS.items()},
                "examples": core.EXAMPLES}

    print(f"\nHALO on http://127.0.0.1:{PORT}\n")
    import uvicorn
    uvicorn.run(server.app, host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
