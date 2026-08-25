"""Narrow MCP client for the owner's Supabase-backed sticker library."""

from __future__ import annotations

import copy
import ipaddress
import json
import threading
import urllib.parse
import urllib.request
from typing import Any


# No bundled default: configure DWELL_STICKER_MCP_URL explicitly. When empty,
# sticker search/send is disabled rather than reaching some unrelated endpoint.
DEFAULT_STICKER_MCP_URL = ""
STICKER_ID_MAX = 100


def _urlopen(req: urllib.request.Request, timeout: float):
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


def _json_rpc_payload(raw: bytes) -> dict[str, Any]:
    text = raw.decode("utf-8", "replace").strip()
    if text.startswith("data:"):
        frames = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
        text = next((frame for frame in reversed(frames) if frame and frame != "[DONE]"), "{}")
    payload = json.loads(text or "{}")
    if not isinstance(payload, dict):
        raise ValueError("MCP returned a non-object response")
    return payload


class StickerBridge:
    def __init__(self, url: str, timeout: float = 12.0):
        self.url = str(url or "").strip()
        self.timeout = timeout
        self._tools: dict[str, dict[str, Any]] | None = None
        self._media_cache: dict[str, dict[str, str]] = {}
        self._lock = threading.Lock()
        self.last_error = ""

    def _rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.url:
            raise RuntimeError("sticker MCP URL is not configured")
        body = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
            ensure_ascii=False,
        ).encode()
        req = urllib.request.Request(
            self.url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "User-Agent": "dwell-sticker-bridge/1.0",
            },
            method="POST",
        )
        with _urlopen(req, timeout=self.timeout) as response:
            payload = _json_rpc_payload(response.read())
        if payload.get("error"):
            error = payload["error"] if isinstance(payload["error"], dict) else {}
            raise RuntimeError(str(error.get("message") or "sticker MCP request failed"))
        result = payload.get("result") or {}
        if not isinstance(result, dict):
            raise ValueError("sticker MCP result is not an object")
        return result

    def tools(self, refresh: bool = False) -> dict[str, dict[str, Any]]:
        if self._tools is not None and not refresh:
            return self._tools
        result = self._rpc("tools/list")
        found = {
            str(tool.get("name")): tool
            for tool in (result.get("tools") or [])
            if isinstance(tool, dict) and tool.get("name") in ("search_stickers", "send_sticker")
        }
        if set(found) != {"search_stickers", "send_sticker"}:
            raise RuntimeError("sticker MCP is missing search_stickers or send_sticker")
        self._tools = found
        self.last_error = ""
        return found

    def openai_tools(self) -> list[dict[str, Any]]:
        tools = self.tools()
        output = []
        for name in ("search_stickers", "send_sticker"):
            item = copy.deepcopy(tools[name])
            description = str(item.get("description") or "")
            if name == "search_stickers":
                description += " 只有真想在当前主聊天发一张表情时才调用；先搜索，再从真实候选中选择。"
            else:
                description += " sticker_id 必须来自本轮 search_stickers，禁止编造；一轮最多发送一张。"
            output.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": description,
                        "parameters": item.get("inputSchema") or {"type": "object"},
                    },
                }
            )
        return output

    def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self._rpc("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            messages = [
                str(item.get("text") or "")
                for item in (result.get("content") or [])
                if isinstance(item, dict) and item.get("type") == "text"
            ]
            raise RuntimeError(next((message for message in messages if message), "sticker MCP tool failed"))
        return result

    @staticmethod
    def _text_json(result: dict[str, Any]) -> dict[str, Any]:
        for item in result.get("content") or []:
            if not isinstance(item, dict) or item.get("type") != "text":
                continue
            try:
                value = json.loads(str(item.get("text") or ""))
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
        return {}

    @staticmethod
    def _candidate(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        sticker_id = str(value.get("id") or "").strip()
        if not sticker_id or len(sticker_id) > STICKER_ID_MAX or sticker_id.startswith("st_test_"):
            return None
        return {
            "id": sticker_id,
            "ocr_text": str(value.get("ocr_text") or "")[:300],
            "visual_description": str(value.get("visual_description") or "")[:800],
            "semantic_intent": str(value.get("semantic_intent") or "")[:800],
            "tone_tags": [str(tag)[:80] for tag in (value.get("tone_tags") or [])[:12]],
            "use_intents": [str(tag)[:160] for tag in (value.get("use_intents") or [])[:12]],
            "avoid_when": [str(tag)[:160] for tag in (value.get("avoid_when") or [])[:12]],
            "score": value.get("score") if isinstance(value.get("score"), (int, float)) else 0,
        }

    def search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query") or "").strip()[:1000]
        if not query:
            raise ValueError("sticker search query is required")
        payload: dict[str, Any] = {"query": query}
        for key in ("emotion", "tone"):
            value = str(arguments.get(key) or "").strip()[:200]
            if value:
                payload[key] = value
        excluded = [str(item)[:STICKER_ID_MAX] for item in (arguments.get("exclude_ids") or [])[:100]]
        if excluded:
            payload["exclude_ids"] = excluded
        try:
            limit = int(arguments.get("limit") or 8)
        except (TypeError, ValueError):
            limit = 8
        payload["limit"] = max(1, min(limit, 20))
        result = self._text_json(self._call("search_stickers", payload))
        candidates = []
        for raw in result.get("candidates") or []:
            candidate = self._candidate(raw)
            if candidate:
                candidates.append(candidate)
        self.last_error = ""
        return {"candidates": candidates}

    def _valid_media_url(self, raw: Any) -> str:
        url = str(raw or "").strip()
        parsed = urllib.parse.urlsplit(url)
        endpoint = urllib.parse.urlsplit(self.url)
        if parsed.hostname != endpoint.hostname:
            raise ValueError("sticker media host does not match the configured MCP")
        if parsed.scheme != "https":
            try:
                loopback = ipaddress.ip_address(parsed.hostname or "").is_loopback
            except ValueError:
                loopback = (parsed.hostname or "").lower() == "localhost"
            if parsed.scheme != "http" or not loopback:
                raise ValueError("sticker media URL must use HTTPS")
        if not parsed.path.startswith("/storage/v1/object/public/stickers/"):
            raise ValueError("sticker media URL is outside the public sticker bucket")
        return url

    def send(self, sticker_id: str) -> dict[str, str]:
        sticker_id = str(sticker_id or "").strip()
        if not sticker_id or len(sticker_id) > STICKER_ID_MAX or sticker_id.startswith("st_test_"):
            raise ValueError("invalid sticker id")
        with self._lock:
            cached = self._media_cache.get(sticker_id)
        if cached:
            return dict(cached)
        result = self._call("send_sticker", {"sticker_id": sticker_id})
        data = result.get("structuredContent") or {}
        if not isinstance(data, dict) or str(data.get("sticker_id") or "") != sticker_id:
            raise ValueError("sticker MCP returned a mismatched sticker")
        media = {
            "sticker_id": sticker_id,
            "url": self._valid_media_url(data.get("url")),
            "alt": str(data.get("alt") or data.get("caption") or "表情包")[:500],
            "caption": str(data.get("caption") or data.get("alt") or "表情包")[:500],
            "visual_description": str(data.get("visual_description") or "")[:800],
            "semantic_intent": str(data.get("semantic_intent") or "")[:800],
        }
        with self._lock:
            self._media_cache[sticker_id] = dict(media)
        self.last_error = ""
        return media

    def picker(self, query: str = "") -> dict[str, Any]:
        query = str(query or "").strip()
        search = self.search(
            {
                "query": query or "常用聊天表情包，覆盖开心、难过、无语、撒娇、抱抱、催促、道歉和打招呼",
                "limit": 20,
            }
        )
        candidates = search["candidates"]
        resolved: dict[str, dict[str, str]] = {}
        for candidate in candidates:
            try:
                media = self.send(candidate["id"])
            except (OSError, RuntimeError, ValueError):
                continue
            resolved[media["sticker_id"]] = media
        stickers = []
        for candidate in candidates:
            media = resolved.get(candidate["id"])
            if media:
                stickers.append({**candidate, **media})
        return {"query": query, "stickers": stickers}

    def health(self) -> dict[str, Any]:
        try:
            tools = self.tools(refresh=True)
            self.last_error = ""
            return {"ok": True, "tools": list(tools)}
        except (OSError, RuntimeError, ValueError) as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return {"ok": False, "tools": [], "error": self.last_error}
