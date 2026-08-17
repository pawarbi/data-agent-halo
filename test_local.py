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

# main() swaps core.llm for a stub. Keep the real one so the degraded-model tests
# can exercise the actual function, which is the only thing that sets llm_status.
_REAL_LLM = core.llm


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


def fake_llm(system: str, user: str, temperature: float = 0.0, model=None) -> str:
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


def test_out_of_scope() -> None:
    """A question no domain covers must not reach Fabric at all.

    Before this, an unroutable question fell back to "first agent", so "hi" spent
    a real Fabric call and returned a greeting formatted as a data answer.
    """
    print("\n[5] questions no agent can answer")
    calls: list[str] = []
    real_call = core.call_data_agent

    async def counting_call(token, ws, da, question):
        calls.append(ws)
        return await real_call(token, ws, da, question)

    core.call_data_agent = counting_call
    saved = core.llm
    # A working model that correctly answers "none of these".
    core.llm = lambda s, u, temperature=0.0, model=None: "[]" if "route a question" in s else '{"verdict":"pass"}'
    try:
        st = run_graph("hi", "t-oos")
        check("no agent was called", calls == [], str(calls))
        check("route is empty", st.get("route") == [], str(st.get("route")))
        check("still returns an answer", bool(st.get("answer")))
        check("the answer says which domains exist",
              all(k in st["answer"] for k in KEYS), st.get("answer", "")[:120])
        check("never reached fan_out",
              not any("fan_out" in t for t in st["trace"]), str(st["trace"]))
    finally:
        core.llm = saved
        core.call_data_agent = real_call


def test_validate_explains_itself() -> None:
    """A pass should say why, not just pass. Parsing must stay tolerant."""
    print("\n[6] the judge explains a pass")
    saved = core.llm
    try:
        core.llm = lambda s, u, temperature=0.0, model=None: (
            '{"verdict":"pass","reason":"both domains answered and periods are stated"}')
        out = core.validate({"question": "q", "answer": "a", "attempts": 1})
        check("a pass carries a reason", out["reason"].startswith("both domains"), str(out))
        check("the reason reaches the trace", "both domains" in out["trace"][0], out["trace"][0])

        # An older judge that only returns a verdict must still work.
        core.llm = lambda s, u, temperature=0.0, model=None: '{"verdict":"pass"}'
        out = core.validate({"question": "q", "answer": "a", "attempts": 1})
        check("a bare verdict still parses", out["verdict"] == "pass" and out["reason"] == "",
              str(out))

        # A retry shows the critique, not the reason.
        core.llm = lambda s, u, temperature=0.0, model=None: (
            '{"verdict":"retry","critique":"manufacturing was never asked"}')
        out = core.validate({"question": "q", "answer": "a", "attempts": 1})
        check("a retry still shows the critique",
              "never asked" in out["trace"][0], out["trace"][0])

        # Unparseable output must not loop forever, and must say why it passed.
        core.llm = lambda s, u, temperature=0.0, model=None: "sorry, I cannot do that"
        out = core.validate({"question": "q", "answer": "a", "attempts": 1})
        check("unparseable judgement passes and explains itself",
              out["verdict"] == "pass" and bool(out["reason"]), str(out))
    finally:
        core.llm = saved


def test_validation_optional() -> None:
    """The judge can be turned off. It must then not run at all, not just pass."""
    print("\n[6] validation toggle")
    judged = {"n": 0}
    saved = core.llm

    def counting(system, user, temperature=0.0, model=None):
        # Count judgements but keep fake_llm's behaviour, so the retry path is
        # still reachable; a stub that always passed would hide it.
        if "QA judge" in system:
            judged["n"] += 1
        return fake_llm(system, user, temperature, model)

    core.llm = counting
    try:
        graph = core.build_graph()
        off = asyncio.run(graph.ainvoke(
            {"question": "show me downtime", "user_token": TOKEN, "attempts": 0,
             "skip_validate": True},
            {"configurable": {"thread_id": "t-noval"}}))
        check("the judge never runs when off", judged["n"] == 0, f"{judged['n']} judgements")
        check("an answer still comes back", bool(off.get("answer")))
        check("no retry, so one fan-out only", off.get("attempts") == 1, str(off.get("attempts")))
        check("validate left no trace entry",
              not any("validate" in t for t in off["trace"]), str(off["trace"]))

        # fake_llm asks for a retry on the first "downtime" judgement, so reset its
        # counter to make the next one the first.
        judged["n"] = 0
        _judged["n"] = 0
        on = asyncio.run(graph.ainvoke(
            {"question": "show me downtime", "user_token": TOKEN, "attempts": 0},
            {"configurable": {"thread_id": "t-val"}}))
        check("the judge runs by default", judged["n"] >= 1, f"{judged['n']} judgements")
        check("and can still drive a retry", on.get("attempts") == 2, str(on.get("attempts")))
    finally:
        core.llm = saved


def test_config_and_model_override() -> None:
    """Agents come from config, and a per-run model choice reaches the model call."""
    print("\n[6] configuration")
    agents, examples = core.load_agents(json.dumps({
        "examples": ["one", "two"],
        "agents": {"demo": {
            "url": "https://api.fabric.microsoft.com/v1/mcp/workspaces/"
                   "00000000-0000-0000-0000-000000000000/dataagents/"
                   "11111111-1111-1111-1111-111111111111/agent",
            "description": "A demo domain.", "excludes": "Nothing else."}}}))
    check("agents load from JSON", list(agents) == ["demo"], str(list(agents)))
    check("the URL is parsed into GUIDs",
          agents["demo"]["workspace_id"] == "00000000-0000-0000-0000-000000000000")
    check("examples load from the same file", examples == ["one", "two"], str(examples))

    for bad, why in (({"agents": {}}, "no agents"),
                     ({"agents": {"x": {"url": "nonsense", "description": "d"}}}, "bad url"),
                     ({"agents": {"x": {"url": "https://h/v1/mcp/workspaces/"
                                        "00000000-0000-0000-0000-000000000000/dataagents/"
                                        "11111111-1111-1111-1111-111111111111/agent"}}},
                      "no description")):
        try:
            core.load_agents(json.dumps(bad))
            check(f"rejects config with {why}", False, "no error raised")
        except Exception:
            check(f"rejects config with {why}", True)

    # The chosen model must reach the HTTP call, not just sit in state.
    seen = {}
    saved = core.llm

    def spy(system, user, temperature=0.0, model=None):
        seen.setdefault("model", model)
        return json.dumps([FIRST]) if "route a question" in system else '{"verdict":"pass"}'

    core.llm = spy
    try:
        graph = core.build_graph()
        asyncio.run(graph.ainvoke(
            {"question": "anything", "user_token": TOKEN, "attempts": 0,
             "model": "some/other-model"},
            {"configurable": {"thread_id": "t-model"}}))
        check("a per-run model choice reaches the model call",
              seen.get("model") == "some/other-model", str(seen))
    finally:
        core.llm = saved


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
    #
    # Simulate the model being unavailable by removing the key and letting the real
    # llm() run, rather than stubbing it out. classify distinguishes "the model said
    # none of these" from "the model failed" via llm_status, which only the real
    # function sets — a stub returning "" would silently take the wrong branch.
    saved_key, saved_llm = core.LLM_KEY, core.llm
    core.LLM_KEY, core.llm = "", _REAL_LLM
    try:
        out = core.classify({"question": "unroutable gibberish zzz"})
        check("an unroutable question routes nowhere rather than guessing",
              out["route"] == [], str(out["route"]))
        check("the trace says no domain covers it", "no domain covers" in out["trace"][0],
              out["trace"][0])
        routed = core.classify({"question": f"tell me about {FIRST}"})
        check("the keyword fallback still routes what it recognises",
              routed["route"] == [FIRST], str(routed["route"]))
        check("the trace names the degraded tier", "keywords" in routed["trace"][0],
              routed["trace"][0])
    finally:
        core.LLM_KEY, core.llm = saved_key, saved_llm


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

    # The stream now interleaves two kinds of event: node completions (which carry
    # "node") and live progress emitted from inside a running node (which carry "ev").
    def split(ev):
        # Progress events also carry "node", so key off "ev" to tell them apart.
        return ([e["node"] for e in ev if "ev" not in e],
                [e for e in ev if "ev" in e])

    ev = ask("a question only one domain can answer", "sse-a")
    nodes, prog = split(ev)
    check("nodes stream in graph order",
          nodes == ["classify", "gate", "fan_out", "synthesize", "validate", "done"], str(nodes))
    routed = next(e for e in ev if "ev" not in e and e.get("node") == "classify")
    check("route reaches the browser", routed["route"] == [FIRST], str(routed))
    check("final answer is non-empty", bool(ev[-1]["answer"]))

    # Progress events are what keep the UI alive during a 30-90s agent call.
    kinds = [p["ev"] for p in prog]
    check("nodes announce themselves before finishing", "node_start" in kinds, str(kinds[:6]))
    check("agent start and finish both stream",
          "agent_start" in kinds and "agent_end" in kinds, str(kinds))
    starts = [p["ev"] for p in prog if p["ev"] in ("node_start", "node_end")]
    check("a node's start precedes its end", starts[0] == "node_start", str(starts[:4]))
    ends = [p for p in prog if p["ev"] == "node_end"]
    check("every node reports a duration", all("ms" in p for p in ends) and len(ends) >= 5,
          str([(p["node"], p.get("ms")) for p in ends]))
    check("every event carries elapsed time", all("elapsed_ms" in p for p in prog))
    fan = [p for p in prog if p["ev"] == "fanout_done"]
    check("fan-out reports wall vs serial time",
          bool(fan) and {"wall_ms", "serial_ms", "agents"} <= set(fan[0]), str(fan))

    ev = ask("show me downtime", "sse-b")
    nodes, prog = split(ev)
    check("retry emits a second fan_out", nodes.count("fan_out") == 2, str(nodes))
    check("critique reaches the browser",
          any(e.get("verdict") == "retry" and e.get("critique") for e in ev))
    second = [e for e in ev if "ev" not in e and e.get("node") == "fan_out"][1]
    check("second pass shows one result, not two", len(second["results"]) == 1,
          str(second["results"]))

    ev = ask("compare the two domains", "sse-c")
    _, prog = split(ev)
    ends = [p for p in prog if p["ev"] == "agent_end"]
    check("each agent in a fan-out reports independently", len(ends) == len(KEYS), str(ends))
    fan = next(p for p in prog if p["ev"] == "fanout_done")
    check("parallel wall time beats the serial sum",
          fan["wall_ms"] < fan["serial_ms"], str(fan))

    # A real agent thinks for 30-90s with nothing to report, which is long enough
    # for a proxy to drop an idle connection. Prove the keepalive frames arrive by
    # making an agent slow and the heartbeat fast.
    saved_call, saved_beat = core.call_data_agent, server.HEARTBEAT_SECONDS
    server.HEARTBEAT_SECONDS = 0.2

    async def slow_call(token, ws, da, question):
        await asyncio.sleep(1.2)
        return "slow but fine"

    core.call_data_agent = slow_call
    try:
        url = f"http://127.0.0.1:{port}/api/ask?q=slow+one&thread=sse-hb"
        beats, datas = 0, 0
        with urllib.request.urlopen(url, timeout=60) as r:
            check("stream asks proxies not to buffer",
                  r.headers.get("X-Accel-Buffering") == "no", str(dict(r.headers)))
            for raw in r:
                line = raw.decode("utf-8")
                if line.startswith(":"):
                    beats += 1
                elif line.startswith("data: "):
                    datas += 1
        check("keepalive frames arrive while an agent is slow", beats >= 2, f"{beats} beats")
        check("keepalives do not disturb the real events", datas >= 6, f"{datas} data frames")
    finally:
        core.call_data_agent, server.HEARTBEAT_SECONDS = saved_call, saved_beat


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
    test_out_of_scope()
    test_validate_explains_itself()
    test_validation_optional()
    test_config_and_model_override()
    test_keyword_routing()
    test_auth_guard()
    test_sse()

    print(f"\n{len(PASSED)} checks passed")


if __name__ == "__main__":
    main()
