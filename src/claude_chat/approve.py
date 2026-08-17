"""The Allow/Deny permission gate — a one-tool MCP server for approve-mode turns.

Claude Code's ``--permission-prompt-tool`` calls :func:`approve` whenever a
headless turn wants to do something its permission rules would prompt for. The
gate registers the request with the chat daemon (which renders the Allow/Deny
card in the conversation), polls for the operator's click, and answers with the
documented verdict contract::

    {"behavior": "allow", "updatedInput": {...}}   # approved
    {"behavior": "deny",  "message": "..."}        # denied, timeout, or any failure

Fail-closed is the whole design: no config, daemon unreachable, or an
unanswered request all deny. Routing arrives via env from the per-turn
``--mcp-config`` the daemon writes; the daemon is localhost/tailnet-only, so no
secret is involved.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

_POLL_SEC = 2.0


def _deny(message: str) -> str:
    return json.dumps({"behavior": "deny", "message": message})


def _allow(updated_input: dict[str, Any]) -> str:
    return json.dumps({"behavior": "allow", "updatedInput": updated_input})


def _chat_call(url: str, payload: dict[str, Any] | None = None,
               timeout: float = 10.0) -> dict[str, Any]:
    """One call to the chat daemon. Never raises — any failure comes back as
    ``{"ok": False, …}`` so the gate fails closed."""
    try:
        if payload is None:
            req = urllib.request.Request(url, method="GET")
        else:
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="replace"))
        return body if isinstance(body, dict) else {"ok": False}
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError):
        return {"ok": False}


def decide(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Register the request with the daemon, poll for the operator's verdict.
    The daemon owns the card UI; this process owns the deadline and the
    fail-closed default."""
    base_url = os.environ.get("CLAUDE_CHAT_URL", "").rstrip("/")
    conv_id = os.environ.get("CLAUDE_CHAT_CONVERSATION", "")
    try:
        timeout_sec = int(os.environ.get("CLAUDE_CHAT_APPROVE_TIMEOUT", "300"))
    except ValueError:
        timeout_sec = 300
    if not (base_url and conv_id):
        return _deny("approval gate misconfigured (missing daemon url/"
                     "conversation) — denying by default")

    created = _chat_call(f"{base_url}/api/approve", {
        "conversation_id": conv_id,
        "tool_name": tool_name,
        "tool_input": tool_input,
        "timeout_sec": timeout_sec,
    })
    request_id = str(created.get("id", ""))
    if not created.get("ok") or not request_id:
        return _deny("could not reach the chat daemon — denying by default")

    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        state = _chat_call(f"{base_url}/api/approve/{request_id}")
        verdict = state.get("verdict") if state.get("ok") else None
        if verdict == "allow":
            return _allow(tool_input)
        if verdict == "deny":
            return _deny("the operator denied this action in the chat")
        time.sleep(_POLL_SEC)
    _chat_call(f"{base_url}/api/approve/{request_id}/verdict",
               {"verdict": "timeout"})
    return _deny(f"the operator did not answer within {timeout_sec}s")


def build_server():
    """The one-tool MCP server. Split from :func:`main` so the tests can hold
    it and check the shape of what it puts on the wire."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("claude-chat")

    # structured_output=False is load-bearing, not a style choice. A typed
    # return (`-> str`) makes the MCP SDK derive an output schema and attach
    # `structuredContent` alongside the text block — and Claude Code's
    # permission-prompt contract accepts EXACTLY one text block, nothing else:
    #     "Permission prompt tool returned an invalid result. Expected a single
    #      text block param with type='text' and a string text value."
    # With the schema attached, EVERY gated tool call fails at the bridge. It
    # fails closed, so nothing unsafe runs — but nothing safe runs either, and
    # the error names the bridge, not the cause. See the shape test.
    @mcp.tool(structured_output=False)
    def approve(tool_name: str, input: dict, tool_use_id: str = "") -> str:
        """Ask the operator in the chat (Allow/Deny card) whether this tool
        call may run. Returns the permission-prompt verdict JSON."""
        try:
            return decide(tool_name, input or {})
        except Exception as exc:   # the gate itself failing = deny, loudly
            return _deny(f"approval gate crashed ({exc}) — denying by default")

    return mcp


def main() -> None:
    build_server().run()


if __name__ == "__main__":
    main()
