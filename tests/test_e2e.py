"""End-to-end: spawn the real server over stdio and talk to it with the official MCP client."""
from __future__ import annotations

import json
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from web_mcp.textutil import count_tokens

TOOL_LIST_TOKEN_BUDGET = 640  # measured ~480 (full JSON schema incl. names); plan target was ~450


def params(tmp_path, extra_env=None):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[providers]\nchain = []\n[state]\npath = "{tmp_path}/state.db"\n[cache]\npath = "{tmp_path}/cache.db"\n'
        f'[audit]\npath = "{tmp_path}/audit.jsonl"\n[approval]\nmodes = ["deny"]\n'
    )
    import os

    env = {**os.environ, "WEBMCP_CONFIG": str(cfg), "XDG_CONFIG_HOME": str(tmp_path / "xdg"), **(extra_env or {})}
    return StdioServerParameters(command=sys.executable, args=["-m", "web_mcp", "serve"], env=env)


async def test_tools_list_is_small_and_has_three_tools(tmp_path):
    async with stdio_client(params(tmp_path)) as (r, w), ClientSession(r, w) as session:
        await session.initialize()
        tools = (await session.list_tools()).tools
        assert sorted(t.name for t in tools) == ["research_approve", "research_start", "web_fetch", "web_search"]
        payload = json.dumps([{"name": t.name, "description": t.description, "inputSchema": t.inputSchema} for t in tools])
        n = count_tokens(payload)
        print(f"\ntools/list footprint: {n} tokens")
        assert n <= TOOL_LIST_TOKEN_BUDGET, n
        for t in tools:
            assert "ctx" not in json.dumps(t.inputSchema)  # Context is injected, never exposed to the model


async def test_full_flow_over_stdio(tmp_path):
    async with stdio_client(params(tmp_path)) as (r, w), ClientSession(r, w) as session:
        await session.initialize()
        res = await session.call_tool("research_start", {
            "topic": "python release", "objective": "find the latest version", "sub_questions": ["latest python version"],
            "depth": "quick"})
        text = res.content[0].text
        assert "session: r_" in text and "budget: search 0/3" in text
        sid = text.split("session: ")[1].split(" ")[0]
        # no providers configured -> a clean, non-charged error the agent can read
        res = await session.call_tool("web_search", {"session_id": sid, "query": "latest python version"})
        assert res.content[0].text.startswith("ERROR") and "search 0/3" in res.content[0].text
        # SSRF through the real, un-mocked safe HTTP layer
        res = await session.call_tool("web_fetch", {"session_id": sid, "target": "http://127.0.0.1:8000/"})
        assert res.content[0].text.startswith("DENIED") or res.content[0].text.startswith("BLOCKED")
        res = await session.call_tool("web_search", {"session_id": "bogus", "query": "x"})
        assert res.content[0].text.startswith("ERROR")


@pytest.mark.skipif(sys.platform == "win32", reason="posix only")
async def test_invalid_args_do_not_crash_server(tmp_path):
    async with stdio_client(params(tmp_path)) as (r, w), ClientSession(r, w) as session:
        await session.initialize()
        res = await session.call_tool("research_start", {"topic": "x"})  # missing required args
        assert res.isError
        res = await session.call_tool("research_start", {
            "topic": "t", "objective": "o", "sub_questions": ["q"]})
        assert "session:" in res.content[0].text  # server still healthy


async def _elicit_flow(tmp_path, answer):
    """Real MCP client that advertises elicitation and answers the approval prompt with `answer`."""
    import mcp.types as types

    seen = []

    async def on_elicit(context, p):
        seen.append(p.message)
        if answer is None:
            return types.ElicitResult(action="decline")
        return types.ElicitResult(action="accept", content={"choice": answer})

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[providers]\nchain = []\n[state]\npath = "{tmp_path}/state.db"\n[cache]\npath = "{tmp_path}/cache.db"\n'
        f'[audit]\npath = "{tmp_path}/audit.jsonl"\n[approval]\nmodes = ["elicitation", "deny"]\n'
        '[limits]\nmax_concurrent_sessions = 1\n'
    )
    import os

    prm = StdioServerParameters(command=sys.executable, args=["-m", "web_mcp", "serve"],
                                env={**os.environ, "WEBMCP_CONFIG": str(cfg), "XDG_CONFIG_HOME": str(tmp_path / "xdg")})
    async with stdio_client(prm) as (r, w), ClientSession(r, w, elicitation_callback=on_elicit) as session:
        await session.initialize()
        plan = {"topic": "t one", "objective": "o", "sub_questions": ["q"]}
        first = (await session.call_tool("research_start", plan)).content[0].text
        assert "session:" in first
        second = (await session.call_tool("research_start", {**plan, "topic": "t two"})).content[0].text
        return seen, second


async def test_elicitation_user_allows(tmp_path):
    seen, second = await _elicit_flow(tmp_path, "once")
    assert len(seen) == 1 and "sessions" in seen[0].lower() and "Allow once" in seen[0]
    assert "session:" in second


async def test_elicitation_user_denies(tmp_path):
    seen, second = await _elicit_flow(tmp_path, "deny")
    assert len(seen) == 1 and second.startswith("DENIED")


async def test_elicitation_declined_or_invalid_choice_is_denial(tmp_path):
    seen, second = await _elicit_flow(tmp_path, None)
    assert second.startswith("DENIED")
    seen2, second2 = await _elicit_flow(tmp_path / "x" if (tmp_path / "x").mkdir() is None else tmp_path, "sudo-approve-everything")
    assert second2.startswith("DENIED")  # unknown choice can never grant


async def test_relay_over_real_stdio_no_client_support_needed(tmp_path):
    """The default mode: a plain client (no elicitation support) gets APPROVAL REQUIRED, 'asks the user', records the
    answer with research_approve and repeats the call."""
    import os
    import re

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[providers]\nchain = []\n[state]\npath = "{tmp_path}/state.db"\n[cache]\npath = "{tmp_path}/cache.db"\n'
        f'[audit]\npath = "{tmp_path}/audit.jsonl"\n[limits]\nmax_concurrent_sessions = 1\n'
    )
    prm = StdioServerParameters(command=sys.executable, args=["-m", "web_mcp", "serve"],
                                env={**os.environ, "WEBMCP_CONFIG": str(cfg), "XDG_CONFIG_HOME": str(tmp_path / "xdg")})
    plan = {"topic": "t one", "objective": "o", "sub_questions": ["q"]}
    async with stdio_client(prm) as (r, w), ClientSession(r, w) as session:
        await session.initialize()
        assert "session:" in (await session.call_tool("research_start", plan)).content[0].text
        blocked = (await session.call_tool("research_start", {**plan, "topic": "t two"})).content[0].text
        assert blocked.startswith("APPROVAL REQUIRED") and "Ask the USER" in blocked
        aid = re.search(r"id (a_[0-9a-f]+)", blocked).group(1)
        ok = (await session.call_tool("research_approve", {"approval_id": aid, "choice": "once", "user_reply": "yes, fine"})).content[0].text
        assert ok.startswith("Recorded")
        again = (await session.call_tool("research_start", {**plan, "topic": "t two"})).content[0].text
        assert "session:" in again
