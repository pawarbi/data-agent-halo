"""
Local test harness for HALO — no Azure, no OpenRouter, no network.

Stands up two throwaway MCP servers that impersonate Fabric Data Agents, then
drives the real graph and the real FastAPI app against them. Everything except
the Fabric service itself is the production code path: the MCP handshake, tool
discovery, bearer propagation, parallel fan-out, the retry cycle, the SSE
encoding, and the auth guard.

    python test_local.py

Exits non-zero on the first failure.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
logging.basicConfig(level=logging.WARNING)   # the MCP client is chatty at INFO

import halo_core as core

TOKEN = "USER-TOKEN-alice-0001"


# --------------------------------------------------------------------------- #
# A stand-in Fabric Data Agent: one tool taking `question`, bearer required.
# --------------------------------------------------------------------------- #
def build_mock_agent(name: str):
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse
    try:
        from mcp.server.mcpserver import MCPServer          # mcp 2.x
    except ImportError:
        from mcp.server.fastmcp import FastMCP as MCPServer  # mcp 1.x

    seen: dict[str, str] = {}
    server = MCPServer(name=f"fabric-data-agent-{name}")

    @server.tool(name="query_data_agent", description="Ask a question of the data agent.")
    def query_data_agent(question: str) -> str:
        return f"[{name}] rows for {question!r} (as user={seen.get('user', '?')})"

    class RequireBearer(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            auth = request.headers.get("authorization", "")
            if not auth.startswith("Bearer "):
                return JSONResponse({"error": "missing bearer token"}, status_code=401)
            seen["user"] = auth.split(" ", 1)[1]
            return await call_next(request)

    try:
        app = server.streamable_http_app(streamable_http_path="/agent", stateless_http=True)
    except TypeError:  # mcp 1.x takes no arguments here
        server.settings.streamable_http_path = "/agent"
        server.settings.stateless_http = True
        app = server.streamable_http_app()
    app.add_middleware(RequireBearer)
    return app


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def serve(app, port: int) -> None:
    import uvicorn
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    threading.Thread(target=uvicorn.Server(cfg).run, daemon=True).start()


def wait_for(port: int, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"nothing listening on {port}")


# --------------------------------------------------------------------------- #
# Redirect the real URL builder at the mocks, and stub the model.
# --------------------------------------------------------------------------- #
# Derived from whatever is configured, so changing the agent list can't rot this.
KEYS = list(core.AGENTS)
FIRST, SECOND = KEYS[0], KEYS[1]
PORTS: dict[str, int] = {}
_real_client = core.streamablehttp_client


def _to_mock(url: str, **kw):
    ws = url.split("/workspaces/")[1].split("/dataagents/")[0]
    return _real_client(f"http://127.0.0.1:{PORTS[ws]}/agent", **kw)


_judged = {"n": 0}


def fake_llm(system: str, user: str, temperature: float = 0.0) -> str:
    """Deterministic stand-in for the model: 'compare' fans out, 'downtime' retries once."""
    if "route a question" in system:
        q = user.lower()
        if "compare" in q:
            return json.dumps(KEYS)
        if SECOND in q:
            return json.dumps([SECOND])
        return json.dumps([FIRST])
    if "QA judge" in system:
        _judged["n"] += 1
        if "downtime" in user.lower() and _judged["n"] % 2 == 1:
            return '{"verdict":"retry","critique":"break the figures out by plant and shift"}'
        return '{"verdict":"pass"}'
    return "Combined view:\n" + user.split("Results:")[-1].strip()


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
PASSED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if not cond:
        print(f"  FAIL  {label}" + (f"\n        {detail}" if detail else ""))
        sys.exit(1)
    PASSED.append(label)
    print(f"  ok    {label}")


def run_graph(question: str, thread: str) -> dict:
    graph = core.build_graph()
    return asyncio.run(graph.ainvoke(
        {"question": question, "user_token": TOKEN, "attempts": 0},
        {"configurable": {"thread_id": thread}},
    ))


def test_mcp_call() -> None:
    print("\n[1] the data-agent call, over real MCP")
    ws = core.AGENTS[FIRST]["workspace_id"]
    out = asyncio.run(core.call_data_agent(TOKEN, ws, "<agent-guid>", "a question"))
    check("agent answers", FIRST in out, out)
    check("signed-in user's token reaches the endpoint", TOKEN in out, out)
    other = asyncio.run(core.call_data_agent(
        "USER-TOKEN-bob-0002", core.AGENTS[SECOND]["workspace_id"], "<agent-guid>", "another"))
    check("a second user arrives as themselves", "bob" in other, other)


def test_single_route() -> None:
    print("\n[2] single-domain question")
    st = run_graph("a question only one domain can answer", "t-single")
    check("routes to one agent", st["route"] == [FIRST], str(st["route"]))
    check("one result", len(st["results"]) == 1)
    check("passthrough answer, no merge", st["answer"] == st["results"][0]["answer"])
    check("verdict pass", st["verdict"] == "pass")


def test_parallel() -> None:
    print("\n[3] cross-domain question, parallel fan-out")
    st = run_graph("compare the two domains", "t-parallel")
    agents = {r["agent"] for r in st["results"]}
    check("routes to every agent", agents == set(KEYS), str(agents))
    check("no agent errored", all("(error:" not in r["answer"] for r in st["results"]),
          str(st["results"]))
    check("answer cites each domain", all(k in st["answer"] for k in KEYS))


def test_retry() -> None:
    print("\n[4] validator retry cycle")
    st = run_graph("show me downtime", "t-retry")
    check("looped back to fan_out", st["attempts"] == 2, f"attempts={st['attempts']}")
    check("critique folded into the retry ask", "Refine:" in st["results"][0]["answer"],
          st["results"][0]["answer"])
    # The rejected first pass must not survive into the second synthesize.
    check("rejected pass dropped from results", len(st["results"]) == 1,
          f"{len(st['results'])} results: {st['results']}")
    check("final answer is the refined one", "Refine:" in st["answer"])
    check("retry is bounded", st["verdict"] == "pass")


def test_keyword_routing() -> None:
    """The no-model fallback. Doubles as a disjointness check on the descriptions.

    Each agent's own description terms must select that agent and no other. If
    this fails, two descriptions overlap enough that the classifier cannot tell
    them apart, which will misroute live questions too.
    """
    print("\n[5] keyword routing (the fallback when the model is unavailable)")
    for key, cfg in core.AGENTS.items():
        terms = [w for w in re.findall(r"[a-z]{4,}", cfg["description"].lower())
                 if w not in core._STOPWORDS][:6]
        route = core.keyword_route(" ".join(terms))
        check(f"{key}'s own terms route to {key} alone", route == [key],
              f"got {route} for {terms}")

    check("an unrelated question matches nothing",
          core.keyword_route("what is the weather in reykjavik") == [])

    # A question naming every domain at once must fan out to all of them.
    mixed = " ".join(
        w
        for cfg in core.AGENTS.values()
        for w in [t for t in re.findall(r"[a-z]{4,}", cfg["description"].lower())
                  if t not in core._STOPWORDS][:3]
    )
    check("a question spanning every domain fans out",
          sorted(core.keyword_route(mixed)) == sorted(KEYS), str(core.keyword_route(mixed)))

    # Cases for the agents currently configured. Skipped automatically if the
    # agent list changes, since the expected routes would no longer mean anything.
    expected = {
        "cloudmetrics": [
            "how many active subscriptions are there by plan?",
            "what is our churn rate this quarter?",
            "MRR by plan for enterprise accounts",
            "mean time to resolution on critical support tickets",
            "how many seats do our mid-market clients have?",
            "total invoices billed in 2024",
            "net revenue retention for the enterprise plan",
            "how much recurring revenue do we have?",
        ],
        "manufacturing": [
            "what was OEE by plant last month?",
            "downtime reasons last quarter",
            "scrap rate and yield by production line",
            "units sold and margin on turbomachinery",
            "inventory levels by plant",
            "which assets had the most downtime minutes?",
            "what was our revenue from turbines last month?",
            "which motors had the most downtime?",
        ],
        "ecommerce": [
            # "olist business" must not land on CloudMetrics, whose plan names
            # include the word "business".
            "how is the olist business doing?",
            "olist business performance",
            "how many orders were placed in sao paulo?",
            "which sellers had the most orders?",
            "average freight cost by brazilian state",
            "top product categories by order volume",
            "delivery times by zip code",
            "brazilian ecommerce orders last year",
        ],
    }
    # Cross-domain phrasings and the pair each should fan out to.
    pairs = [
        ("compare subscription growth with production output", {"cloudmetrics", "manufacturing"}),
        ("compare seats sold against units produced at each plant", {"cloudmetrics", "manufacturing"}),
        ("relationship between support tickets and scrap rate", {"cloudmetrics", "manufacturing"}),
        ("compare marketplace orders against units produced", {"ecommerce", "manufacturing"}),
    ]
    if set(expected) == set(KEYS):
        misrouted = [(q, core.keyword_route(q)) for key, qs in expected.items()
                     for q in qs if core.keyword_route(q) != [key]]
        misrouted += [(q, core.keyword_route(q)) for q, want in pairs
                      if set(core.keyword_route(q)) != want]
        check(f"{sum(len(v) for v in expected.values()) + len(pairs)} real questions route correctly",
              not misrouted, "; ".join(f"{q!r} -> {r}" for q, r in misrouted))
    else:
        print(f"  skip  agent-specific routing cases (configured: {KEYS})")

    # classify must say how it routed, so a silent fallback is visible in the trace.
    saved, core.llm = core.llm, lambda s, u, temperature=0.0: ""
    try:
        trace = core.classify({"question": "unroutable gibberish zzz"})["trace"][0]
        check("classify labels a last-resort guess", "guess" in trace, trace)
    finally:
        core.llm = saved


def test_auth_guard() -> None:
    print("\n[6] auth guard, on the untouched app")
    try:
        from starlette.testclient import TestClient
    except ImportError:
        print("  skip  starlette TestClient unavailable")
        return
    import server
    c = TestClient(server.app)
    check("/api/me reports signed out", c.get("/api/me").json()["signed_in"] is False)
    r = c.get("/api/ask", params={"q": "anything"})
    check("/api/ask refuses anonymous callers", r.status_code == 401, str(r.status_code))
    check("/ serves the UI", "auth/login" in c.get("/").text)
    r = c.get("/auth/callback", params={"state": "bogus", "code": "x"})
    check("stale auth state rejected", r.status_code == 400, str(r.status_code))


def test_sse() -> None:
    print("\n[7] SSE stream, through the real FastAPI app")
    import server
    server._token_from_session = lambda request: TOKEN
    port = free_port()
    serve(server.app, port)
    wait_for(port)

    def ask(q: str, thread: str) -> list[dict]:
        url = f"http://127.0.0.1:{port}/api/ask?q={urllib.parse.quote(q)}&thread={thread}"
        out = []
        with urllib.request.urlopen(url, timeout=90) as r:
            check("content-type is text/event-stream",
                  r.headers["content-type"].startswith("text/event-stream"))
            for raw in r:
                line = raw.decode("utf-8").rstrip("\n")
                if line.startswith("data: "):
                    out.append(json.loads(line[6:]))
        return out

    ev = ask("a question only one domain can answer", "sse-a")
    nodes = [e["node"] for e in ev]
    check("nodes stream in graph order",
          nodes == ["classify", "gate", "fan_out", "synthesize", "validate", "done"], str(nodes))
    check("route reaches the browser", ev[0]["route"] == [FIRST])
    check("final answer is non-empty", bool(ev[-1]["answer"]))

    ev = ask("show me downtime", "sse-b")
    nodes = [e["node"] for e in ev]
    check("retry emits a second fan_out", nodes.count("fan_out") == 2, str(nodes))
    check("critique reaches the browser",
          any(e.get("verdict") == "retry" and e.get("critique") for e in ev))
    second = [e for e in ev if e["node"] == "fan_out"][1]
    check("second pass shows one result, not two", len(second["results"]) == 1,
          str(second["results"]))


def main() -> None:
    print(f"python {sys.version.split()[0]}")
    try:
        import importlib.metadata as md
        print(f"mcp {md.version('mcp')} | langgraph {md.version('langgraph')}")
    except Exception:
        pass
    print(f"client takes headers (mcp 1.x style): {core._CLIENT_TAKES_HEADERS}")

    for key, cfg in core.AGENTS.items():
        port = free_port()
        PORTS[cfg["workspace_id"]] = port
        serve(build_mock_agent(key), port)
        wait_for(port)
    print(f"agents under test: {KEYS} on ports {sorted(PORTS.values())}")

    core.streamablehttp_client = _to_mock
    core.llm = fake_llm

    test_mcp_call()
    test_single_route()
    test_parallel()
    test_retry()
    test_keyword_routing()
    test_auth_guard()
    test_sse()

    print(f"\n{len(PASSED)} checks passed")


if __name__ == "__main__":
    main()
