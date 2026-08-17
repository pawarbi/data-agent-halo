"""
Run HALO locally with the real UI, against fake data agents.

For working on index.html, the SSE wiring, or the graph without needing Entra,
Fabric or an OpenRouter key. It serves the real server.py and index.html and
substitutes only the three things that need a cloud:

  - sign-in            -> a fixed fake user token
  - the data agents    -> two local MCP servers that echo the question back
  - the model          -> canned routing/merge/judge responses

    python dev_local.py          then open http://127.0.0.1:8930

Ask "compare units produced vs units sold" to see a parallel fan-out, or
"show me downtime" to see the validator send it round the retry loop.
"""

from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
logging.basicConfig(level=logging.WARNING)

import halo_core as core
import server
from test_local import build_mock_agent, fake_llm, free_port, serve, wait_for

PORT = int(os.environ.get("PORT", "8930"))
FAKE_TOKEN = "USER-TOKEN-alice-0001"

PORTS: dict[str, int] = {}
_real_client = core.streamablehttp_client


def _to_mock(url: str, **kw):
    ws = url.split("/workspaces/")[1].split("/dataagents/")[0]
    return _real_client(f"http://127.0.0.1:{PORTS[ws]}/agent", **kw)


def main() -> None:
    for key, cfg in core.AGENTS.items():
        port = free_port()
        PORTS[cfg["workspace_id"]] = port
        serve(build_mock_agent(key), port)
        wait_for(port)
        print(f"mock {key} agent on 127.0.0.1:{port}")

    core.streamablehttp_client = _to_mock
    core.llm = fake_llm
    server._token_from_session = lambda request: FAKE_TOKEN

    # FastAPI matches in registration order, so drop the real /api/me first.
    server.app.router.routes = [
        r for r in server.app.router.routes if getattr(r, "path", None) != "/api/me"
    ]

    @server.app.get("/api/me")
    def me():
        return {"who": "alice@contoso.com (local dev)", "signed_in": True,
                "agents": {k: v["description"] for k, v in core.AGENTS.items()},
                "examples": core.EXAMPLES}

    print(f"\nHALO (local dev) on http://127.0.0.1:{PORT}\n")
    import uvicorn
    uvicorn.run(server.app, host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
