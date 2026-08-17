"""
Check the configured data agents against real Fabric, as you.

Two jobs:

  1. Proves the plumbing before you touch Entra or Render. If this connects and
     answers, then the token, the MCP transport, the workspace/agent GUIDs and
     the permission grant are all good, and anything still broken is auth
     wiring, not Fabric.
  2. Prints what each agent actually exposes, so the `description` fields in
     halo_core.py can be written from reality. `classify` routes on those
     descriptions alone, so guessing at them is the fastest way to get
     questions sent to the wrong agent.

    az login
    python probe_agents.py
    python probe_agents.py "how many active subscriptions are there?"

The second form asks every agent the same question, which is a quick way to see
where the domain boundary really falls.

Uses your az CLI identity, so it exercises the same per-user RLS path the app
does. No client secret involved.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
logging.basicConfig(level=logging.WARNING)

import halo_core as core


def get_token() -> str:
    from azure.identity import AzureCliCredential, InteractiveBrowserCredential
    try:
        cred = AzureCliCredential()
        return cred.get_token(core.FABRIC_SCOPE).token
    except Exception as e:
        print(f"az CLI credential unavailable ({type(e).__name__}), opening a browser instead")
        return InteractiveBrowserCredential().get_token(core.FABRIC_SCOPE).token


def whoami(token: str) -> str:
    """Read the unverified JWT payload just to show which identity is being used."""
    import base64
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return claims.get("upn") or claims.get("unique_name") or claims.get("oid", "?")
    except Exception:
        return "?"


async def probe(key: str, cfg: dict, token: str, question: str | None) -> bool:
    url = (f"https://{core.FABRIC_HOST}/v1/mcp/workspaces/{cfg['workspace_id']}"
           f"/dataagents/{cfg['data_agent_id']}/agent")
    print(f"\n{'=' * 78}\n{key}\n  {url}")
    try:
        async with core._mcp_session(url, {"Authorization": f"Bearer {token}"}) as session:
            tools = await session.list_tools()
            if not tools.tools:
                print("  NO TOOLS EXPOSED — is the data agent published?")
                return False
            for t in tools.tools:
                print(f"\n  tool: {t.name}")
                if getattr(t, "description", None):
                    print(f"  description: {t.description.strip()}")
                schema = core._tool_schema(t)
                if schema.get("properties"):
                    print(f"  input: {json.dumps(schema['properties'], indent=2)[:900]}")
    except Exception as e:
        print(f"  FAILED: {type(e).__name__}: {e}")
        return False

    if question:
        print(f"\n  asking: {question!r}")
        try:
            answer = await core.call_data_agent(
                token, cfg["workspace_id"], cfg["data_agent_id"], question)
            print("  answer:")
            for line in answer.splitlines():
                print(f"    {line}")
        except Exception as e:
            print(f"  ASK FAILED: {type(e).__name__}: {e}")
            return False
    return True


async def main() -> int:
    question = sys.argv[1] if len(sys.argv) > 1 else None
    print(f"host: {core.FABRIC_HOST}")
    print(f"mcp client takes headers (1.x style): {core._CLIENT_TAKES_HEADERS}")
    token = get_token()
    print(f"signed in as: {whoami(token)}")

    results = {}
    for key, cfg in core.AGENTS.items():
        results[key] = await probe(key, cfg, token, question)

    print(f"\n{'=' * 78}")
    for key, ok in results.items():
        print(f"  {'OK  ' if ok else 'FAIL'}  {key}")
    if not all(results.values()):
        print("\nIf it is a 401: the delegated DataAgent.Execute.All grant is the usual cause.")
        print("If it is a 403 or 404: check the workspace/agent GUIDs and that you have access.")
        return 1
    print("\nAll agents reachable. Update the `description` fields in halo_core.py to match")
    print("what you saw above, then run the app.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
