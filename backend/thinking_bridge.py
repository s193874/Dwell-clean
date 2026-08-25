"""Small client for sibylsea-hub/gpt-thinking-block-mcp.

The upstream service owns the tool description and the rendering contract.  We
load that schema over MCP instead of copying it, then forward successful model
tool calls back to the MCP service.  The dwell UI renders the returned visible
working summary in its existing Thought process drawer.
"""

from __future__ import annotations

import json
import ipaddress
import copy
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


FALLBACK_TOOL = {
    "name": "render_thinking_block",
    "description": (
        "Before a non-trivial final answer, provide a concise, user-visible "
        "working summary. Do not include secrets or claim this is hidden "
        "chain-of-thought. Then continue with the normal answer."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "style": {"type": "string", "enum": ["deep_think", "relational"]},
            "thinking": {"type": "string"},
            "effort": {"type": "string", "enum": ["low", "medium", "high"]},
            "skin": {"type": "string", "enum": ["botanical", "microglow"]},
        },
        "required": ["style", "thinking", "effort", "skin"],
    },
}


def _urlopen(req: urllib.request.Request, timeout: float):
    """The thinking MCP is normally local and must not leak through a proxy."""
    host = urllib.parse.urlsplit(req.full_url).hostname or ""
    direct = host.lower() == "localhost"
    if not direct:
        try:
            direct = ipaddress.ip_address(host).is_loopback
        except ValueError:
            pass
    if direct:
        return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
            req, timeout=timeout
        )
    return urllib.request.urlopen(req, timeout=timeout)


class ThinkingBridge:
    def __init__(self, url: str, timeout: float = 4.0):
        self.url = url
        self.timeout = timeout
        self._tool: dict[str, Any] | None = None
        self.last_error = ""

    def _rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        body = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
            ensure_ascii=False,
        ).encode()
        req = urllib.request.Request(
            self.url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        with _urlopen(req, timeout=self.timeout) as response:
            raw = response.read()
        payload = json.loads(raw or b"{}")
        if payload.get("error"):
            raise RuntimeError(payload["error"].get("message") or "MCP request failed")
        return payload.get("result") or {}

    def tool(self, refresh: bool = False) -> dict[str, Any]:
        if self._tool is not None and not refresh:
            return self._tool
        try:
            result = self._rpc("tools/list")
            tools = result.get("tools") or []
            selected = next(t for t in tools if t.get("name") == "render_thinking_block")
            self._tool = selected
            self.last_error = ""
        except (OSError, ValueError, KeyError, StopIteration, RuntimeError) as exc:
            self._tool = dict(FALLBACK_TOOL)
            self.last_error = f"{type(exc).__name__}: {exc}"
        return self._tool

    def openai_tool(self) -> dict[str, Any]:
        tool = copy.deepcopy(self.tool())
        schema = tool.get("inputSchema") or copy.deepcopy(FALLBACK_TOOL["inputSchema"])
        properties = schema.setdefault("properties", {})
        thinking = properties.setdefault("thinking", {"type": "string"})
        # Upstream supports hosts that keep this field private.  Dwell places it
        # in a user-visible drawer, so the model-facing wording must make that
        # visibility explicit and must not solicit hidden chain-of-thought.
        thinking["description"] = (
            "A concise user-visible work summary: key constraints checked, "
            "evidence used, and the conclusion path. Do not include private "
            "scratchpad, hidden chain-of-thought, secrets, or token-by-token reasoning."
        )
        return {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": (
                    "Provide a concise, user-visible work summary before the final answer. "
                    "This is not a request for hidden chain-of-thought."
                ),
                "parameters": schema,
            },
        }

    def render(self, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self._rpc(
            "tools/call",
            {"name": "render_thinking_block", "arguments": arguments},
        )
        if result.get("isError"):
            raise RuntimeError("MCP render_thinking_block returned an error")
        self.last_error = ""
        return result

    def health(self) -> dict[str, Any]:
        tool = self.tool(refresh=True)
        return {
            "ok": not bool(self.last_error),
            "url": self.url,
            "tool": tool.get("name"),
            "fallback": bool(self.last_error),
            "error": self.last_error,
        }
