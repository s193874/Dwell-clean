"""Capability-link Streamable HTTP MCP for the resident Dwell assistant."""

from __future__ import annotations

import copy
import hmac
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .daily_report import DailyReportService
from .history_search import HistorySearchService
from .nook import DEFAULT_NOTEBOOK_PROMPT, load_book
from .store import Database
from .thinking_bridge import ThinkingBridge


PROTOCOL_VERSION = "2025-06-18"
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{40,120}")


def _iso(at: float) -> str:
    return datetime.fromtimestamp(float(at), timezone.utc).isoformat()


def _diary_entry(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(row.get("id") or 0),
        "text": str(row.get("text") or ""),
        "created_at": _iso(float(row.get("at") or 0)),
        "author_type": str(row.get("author_type") or "user"),
        "author_id": str(row.get("author_id") or ""),
    }


def _daily_report(row: dict[str, Any], date: str) -> dict[str, Any]:
    return {
        "date": date,
        "body": str(row.get("body") or ""),
        "resident_comment": str(row.get("resident_comment") or ""),
        "commented_at": _iso(float(row.get("commented_at") or 0)) if row.get("commented_at") else "",
        "updated_at": _iso(float(row.get("updated_at") or 0)) if row.get("updated_at") else "",
        "source": str(row.get("source") or ""),
    }


def _today_local() -> str:
    # Server local date in YYYY-MM-DD. Used when MCP caller omits the date argument.
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")


def _day_bounds(date_str: str) -> tuple[float, float]:
    # Local midnight bounds for a YYYY-MM-DD date, including local DST rules.
    try:
        from datetime import timedelta
        parsed = datetime.strptime(date_str, "%Y-%m-%d")
        return (
            float(parsed.astimezone().timestamp()),
            float((parsed + timedelta(days=1)).astimezone().timestamp()),
        )
    except ValueError:
        return 0.0, float("inf")


def _hhmm_within_today(at_time: str, date_str: str) -> bool:
    # at_time is "HH:MM" or ""; we accept any non-empty value as "scheduled for today".
    return bool(at_time and at_time.strip())


def _snippet(text: str, needle: str, radius: int = 80) -> str:
    text = text or ""
    idx = text.casefold().find(needle.casefold())
    if idx < 0:
        return text[:160]
    start = max(0, idx - radius)
    end = min(len(text), idx + len(needle) + radius)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return prefix + text[start:end] + suffix


def _longest_meaningful_substring(text: str) -> str:
    """Return the longest contiguous run of "meaningful" characters (letters,
    digits, CJK, or inclusive punctuation inside a word). Useful for picking a
    needle out of a user message that may contain emojis / whitespace."""
    import re
    runs = re.findall(r"[\w一-鿿]+", text or "")
    return max(runs, key=len) if runs else ""


def _bigrams(text: str) -> list[str]:
    """Return up to ~10 distinct 2-character substrings from `text`, ignoring
    whitespace and punctuation. Used as multi-needle input for OR-style LIKE
    recall over past messages."""
    cleaned = "".join(ch for ch in (text or "") if ch.isalnum() or "一" <= ch <= "鿿")
    if len(cleaned) < 2:
        return []
    grams: list[str] = []
    seen: set[str] = set()
    # Walk in 2-char windows; sample every other to keep the query cheap.
    for i in range(0, len(cleaned) - 1, 2):
        gram = cleaned[i:i + 2]
        if gram in seen:
            continue
        seen.add(gram)
        grams.append(gram)
        if len(grams) >= 10:
            break
    return grams


def _attachment_metadata(value: dict[str, Any]) -> dict[str, Any]:
    item = {
        "id": str(value.get("id") or ""),
        "type": str(value.get("type") or "file"),
        "name": str(value.get("name") or "附件"),
        "mime": str(value.get("mime") or "application/octet-stream"),
        "size": int(value.get("size") or 0),
    }
    for key in ("url", "width", "height", "duration"):
        if value.get(key) is not None and value.get(key) != "":
            item[key] = value[key]
    return item


def _message(row: dict[str, Any]) -> dict[str, Any]:
    roles = {"me": "user", "gu": "assistant"}
    epoch = float(row.get("at") or 0)
    item = {
        "seq": int(row["seq"]),
        "role": roles.get(str(row.get("kind") or ""), "system"),
        "text": str(row.get("text") or ""),
        "created_at": _iso(epoch),
        "created_at_epoch": epoch,
        "attachments": [],
    }
    try:
        extra = json.loads(row.get("extra") or "{}")
    except (TypeError, ValueError):
        extra = {}
    raw_attachments = extra.get("attachments") if isinstance(extra, dict) else None
    if isinstance(raw_attachments, list):
        item["attachments"] = [
            _attachment_metadata(value)
            for value in raw_attachments
            if isinstance(value, dict) and value.get("id")
        ]
    if extra.get("source") in ("nook-page", "nook-chat") and isinstance(extra.get("reading"), dict):
        reading = dict(extra["reading"])
        reading.pop("notebook", None)
        item["reading"] = reading
    # 用户消息可能带 quote（引用上一条消息的某一段）。MCP 把它一并暴露给 resident，
    # 这样它知道"用户具体在追问上一条长消息里的哪一句"。
    quote = extra.get("quote") if isinstance(extra, dict) else None
    if isinstance(quote, dict) and quote:
        item["quote"] = {
            "message_seq": int(quote.get("message_seq") or 0),
            "text": str(quote.get("text") or "")[:2000],
        }
        start = quote.get("start_offset")
        end = quote.get("end_offset")
        if isinstance(start, int):
            item["quote"]["start_offset"] = start
        if isinstance(end, int):
            item["quote"]["end_offset"] = end
    sticker = extra.get("sticker") if isinstance(extra, dict) else None
    if isinstance(sticker, dict) and sticker.get("url"):
        item["sticker"] = {
            "sticker_id": str(sticker.get("sticker_id") or ""),
            "url": str(sticker.get("url") or ""),
            "alt": str(sticker.get("alt") or ""),
        }
    return item


def _tool_result(
    data: dict[str, Any],
    summary: str,
    content: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "structuredContent": data,
        "content": content or [{"type": "text", "text": summary}],
        "isError": False,
    }


def _tool_error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


NOAUTH = [{"type": "noauth"}]
TOOLS: list[dict[str, Any]] = [
    {
        "name": "enter_dwell",
        "title": "入住 Dwell",
        "description": (
            "Call once when the user asks you to live in this Dwell. The private "
            "connection URL is the access key; this tool fixes the resident name "
            "configured by the owner and switches Dwell away from local API mode."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "outputSchema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string"},
                "resident_name": {"type": "string"},
                "chat_id": {"type": "string"},
                "chat_name": {"type": "string"},
                "cursor": {"type": "integer"},
                "recent_messages": {"type": "array", "items": {"type": "object"}},
                "regeneration_requests": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["mode", "resident_name", "chat_id", "chat_name", "cursor", "recent_messages", "regeneration_requests"],
        },
        "securitySchemes": NOAUTH,
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    },
    {
        "name": "read_dwell_messages",
        "title": "读取 Dwell 消息",
        "description": "Read the current Dwell chat after a cursor to recover context or catch up.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "after_seq": {"type": "integer", "minimum": 0, "default": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 100},
            },
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "chat_id": {"type": "string"},
                "chat_name": {"type": "string"},
                "mode": {"type": "string"},
                "resident_name": {"type": "string"},
                "cursor": {"type": "integer"},
                "messages": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["chat_id", "chat_name", "mode", "resident_name", "cursor", "messages"],
        },
        "securitySchemes": NOAUTH,
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "read_attachment",
        "title": "读取 Dwell 附件",
        "description": (
            "Read one attachment referenced by a Dwell message. Text files are returned as "
            "UTF-8 text; images are returned as an MCP image content block."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "attachment_id": {"type": "string", "minLength": 1, "maxLength": 100},
            },
            "required": ["attachment_id"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "attachment": {"type": "object"},
                "text": {"type": "string"},
            },
            "required": ["attachment"],
        },
        "securitySchemes": NOAUTH,
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "wait_for_user_message",
        "title": "等待 Dwell 新消息",
        "description": (
            "Wait for new user messages after the cursor. continuous=true (the default) "
            "keeps the resident present and sends transport heartbeats until a new message "
            "arrives. Use continuous=false only when a finite timeout is explicitly wanted."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "after_seq": {"type": "integer", "minimum": 0},
                "timeout_seconds": {"type": "integer", "minimum": 0, "maximum": 3600, "default": 45},
                "continuous": {"type": "boolean", "default": True},
            },
            "required": ["after_seq"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "request_id": {"type": "string"},
                "cursor": {"type": "integer"},
                "timed_out": {"type": "boolean"},
                "continuous": {"type": "boolean"},
                "disconnect_reason": {"type": "string"},
                "user_messages": {"type": "array", "items": {"type": "object"}},
                "reading_pages": {"type": "array", "items": {"type": "object"}},
                "regeneration_requests": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["cursor", "timed_out", "user_messages", "reading_pages", "regeneration_requests"],
        },
        "securitySchemes": NOAUTH,
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "send_dwell_reply_and_wait",
        "title": "回复并继续驻守 Dwell",
        "description": (
            "Send one idempotent resident reply with its visible thinking block, then immediately "
            "continue waiting for the next Dwell message. Use this as the normal resident loop "
            "after handling a user message so the turn does not end in the gap after sending."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "reply_to_seq": {"type": "integer", "minimum": 1},
                "text": {"type": "string", "minLength": 1, "maxLength": 20000},
                "style": {"type": "string", "enum": ["deep_think", "relational"]},
                "thinking": {"type": "string", "minLength": 1, "maxLength": 8000},
                "effort": {"type": "string", "enum": ["low", "medium", "high"]},
                "skin": {"type": "string", "enum": ["botanical", "microglow"]},
                "continuous": {"type": "boolean", "default": True},
                "timeout_seconds": {"type": "integer", "minimum": 0, "maximum": 3600, "default": 45},
            },
            "required": ["reply_to_seq", "text", "style", "thinking", "effort", "skin"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "sent_reply": {"type": "object"},
                "request_id": {"type": "string"},
                "cursor": {"type": "integer"},
                "timed_out": {"type": "boolean"},
                "continuous": {"type": "boolean"},
                "disconnect_reason": {"type": "string"},
                "user_messages": {"type": "array", "items": {"type": "object"}},
                "reading_pages": {"type": "array", "items": {"type": "object"}},
                "regeneration_requests": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["sent_reply", "request_id", "cursor", "timed_out", "continuous", "disconnect_reason", "user_messages", "reading_pages", "regeneration_requests"],
        },
        "securitySchemes": NOAUTH,
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    },
    {
        "name": "send_dwell_reply",
        "title": "回复 Dwell 消息",
        "description": (
            "Send one user-visible resident reply to a specific user message. Before the "
            "final text, provide the required visible thinking block fields. Dwell validates "
            "them with gpt-thinking-block-mcp and shows thinking in its existing Thought "
            "process UI. The call is idempotent per reply_to_seq."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "reply_to_seq": {"type": "integer", "minimum": 1},
                "text": {"type": "string", "minLength": 1, "maxLength": 20000},
                "style": {"type": "string", "enum": ["deep_think", "relational"]},
                "thinking": {"type": "string", "minLength": 1, "maxLength": 8000},
                "effort": {"type": "string", "enum": ["low", "medium", "high"]},
                "skin": {"type": "string", "enum": ["botanical", "microglow"]},
            },
            "required": ["reply_to_seq", "text", "style", "thinking", "effort", "skin"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "ok": {"type": "boolean"},
                "duplicate": {"type": "boolean"},
                "regenerated": {"type": "boolean"},
                "assistant_seq": {"type": "integer"},
                "cursor": {"type": "integer"},
            },
            "required": ["ok", "duplicate", "regenerated", "assistant_seq", "cursor"],
        },
        "securitySchemes": NOAUTH,
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    },
    {
        "name": "read_shared_reading",
        "title": "读取当前共读页",
        "description": "Read the page currently shared by the owner, the fixed chapter-update rule, and a title/summary-only notebook index.",
        "inputSchema": {
            "type": "object",
            "properties": {"slug": {"type": "string", "maxLength": 100, "default": ""}},
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "current_reading": {"type": "object"},
                "slug": {"type": "string"},
                "notebook_prompt": {"type": "string"},
                "note_index": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["current_reading", "slug", "notebook_prompt", "note_index"],
        },
        "securitySchemes": NOAUTH,
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "search_book_notes",
        "title": "检索本书记事本",
        "description": "Search one book's notebook. Results intentionally contain only id, title, summary, pin state, and update time; use read_book_note for the body of one selected result.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "slug": {"type": "string", "minLength": 1, "maxLength": 100},
                "query": {"type": "string", "maxLength": 500, "default": ""},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
            },
            "required": ["slug"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {"slug": {"type": "string"}, "results": {"type": "array", "items": {"type": "object"}}},
            "required": ["slug", "results"],
        },
        "securitySchemes": NOAUTH,
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "read_book_note",
        "title": "读取一条本书记事",
        "description": "Read one selected notebook record in full, returning its title, summary, and body.",
        "inputSchema": {
            "type": "object",
            "properties": {"slug": {"type": "string", "minLength": 1, "maxLength": 100}, "note_id": {"type": "integer", "minimum": 1}},
            "required": ["slug", "note_id"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {"slug": {"type": "string"}, "note": {"type": "object"}},
            "required": ["slug", "note"],
        },
        "securitySchemes": NOAUTH,
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "save_book_note",
        "title": "写入或置顶本书记事",
        "description": (
            "Create or update one durable resident note for a shared book. Include note_id "
            "to update; pinned controls pin state. client_message_id is required for retry "
            "idempotency; for an automatic chapter-end write, derive it from the reading event key."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "slug": {"type": "string", "minLength": 1, "maxLength": 100},
                "note_id": {"type": "integer", "minimum": 1},
                "title": {"type": "string", "minLength": 1, "maxLength": 240},
                "summary": {"type": "string", "maxLength": 4000, "default": ""},
                "body": {"type": "string", "maxLength": 30000, "default": ""},
                "pinned": {"type": "boolean", "default": False},
                "client_message_id": {"type": "string", "minLength": 1, "maxLength": 200},
            },
            "required": ["slug", "title", "client_message_id"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}, "duplicate": {"type": "boolean"}, "note_id": {"type": "integer"}, "note_index": {"type": "array", "items": {"type": "object"}}},
            "required": ["ok", "duplicate", "note_id", "note_index"],
        },
        "securitySchemes": NOAUTH,
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "delete_book_note",
        "title": "删除本书记事",
        "description": "Delete one durable note from a shared book by its id.",
        "inputSchema": {
            "type": "object",
            "properties": {"slug": {"type": "string", "minLength": 1, "maxLength": 100}, "note_id": {"type": "integer", "minimum": 1}},
            "required": ["slug", "note_id"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}, "note_index": {"type": "array", "items": {"type": "object"}}},
            "required": ["ok", "note_index"],
        },
        "securitySchemes": NOAUTH,
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "send_dwell_message",
        "title": "主动发一条 Dwell 消息",
        "description": (
            "Send one resident-authored message that is NOT a reply to a specific user "
            "message. Pass reply_to_seq to turn it into a normal reply; omit it (or pass 0) "
            "to send a proactive message. client_message_id makes the call idempotent — "
            "retries with the same id return the original assistant_seq without sending again. "
            "thinking / style / effort / skin are still required because Dwell renders every "
            "resident turn with its visible working-summary drawer."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "minLength": 1, "maxLength": 20000},
                "reply_to_seq": {"type": "integer", "minimum": 0, "default": 0},
                "client_message_id": {"type": "string", "maxLength": 200, "default": ""},
                "style": {"type": "string", "enum": ["deep_think", "relational"]},
                "thinking": {"type": "string", "minLength": 1, "maxLength": 8000},
                "effort": {"type": "string", "enum": ["low", "medium", "high"]},
                "skin": {"type": "string", "enum": ["botanical", "microglow"]},
                "quote": {
                    "type": "object",
                    "description": "Optional quote of a previous message or a slice of it.",
                    "properties": {
                        "message_seq": {"type": "integer", "minimum": 1},
                        "text": {"type": "string", "maxLength": 2000},
                        "start_offset": {"type": "integer", "minimum": 0},
                        "end_offset": {"type": "integer", "minimum": 0},
                    },
                    "required": ["message_seq", "text"],
                    "additionalProperties": False,
                },
            },
            "required": ["text", "style", "thinking", "effort", "skin"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "ok": {"type": "boolean"},
                "duplicate": {"type": "boolean"},
                "proactive": {"type": "boolean"},
                "assistant_seq": {"type": "integer"},
                "reply_to_seq": {"type": "integer"},
                "cursor": {"type": "integer"},
            },
            "required": ["ok", "duplicate", "proactive", "assistant_seq", "cursor"],
        },
        "securitySchemes": NOAUTH,
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    },
    {
        "name": "search_stickers",
        "title": "搜索表情包",
        "description": (
            "Search the owner's sticker library by description or tag. Returns id, "
            "description, tags and a preview url for each candidate. Pick one and call "
            "send_sticker with its id."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 1000},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {"candidates": {"type": "array", "items": {"type": "object"}}},
            "required": ["candidates"],
        },
        "securitySchemes": NOAUTH,
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    },
    {
        "name": "send_sticker",
        "title": "发送表情包",
        "description": (
            "Send one sticker into the Dwell chat. sticker_id must come from a recent "
            "search_stickers call; do not invent ids."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "sticker_id": {"type": "string", "minLength": 1, "maxLength": 100},
                "reply_to_seq": {"type": "integer", "minimum": 0, "default": 0},
                "client_message_id": {"type": "string", "maxLength": 200, "default": ""},
            },
            "required": ["sticker_id"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "ok": {"type": "boolean"},
                "sticker_id": {"type": "string"},
                "url": {"type": "string"},
                "alt": {"type": "string"},
                "message_seq": {"type": "integer"},
                "reply_to_seq": {"type": "integer"},
                "duplicate": {"type": "boolean"},
            },
            "required": ["ok", "sticker_id"],
        },
        "securitySchemes": NOAUTH,
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "todos",
        "title": "双人待办清单",
        "description": (
            "Coarse-grained access to the shared todos list. action is one of "
            "list / create / update / complete / uncomplete / delete. side is 'mine' "
            "(resident's) or 'hers' (user's)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "create", "update", "complete", "uncomplete", "delete"],
                    "default": "list",
                },
                "side": {"type": "string", "enum": ["mine", "hers"], "default": "mine"},
                "id": {"type": "integer", "minimum": 1},
                "text": {"type": "string", "maxLength": 500},
                "at_time": {"type": "string", "maxLength": 5},
                "due_date": {"type": "string", "maxLength": 10},
                "fixed": {"type": "boolean"},
                "by_who": {"type": "string", "maxLength": 20, "default": "resident"},
            },
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "ok": {"type": "boolean"},
                "mine": {"type": "array", "items": {"type": "object"}},
                "hers": {"type": "array", "items": {"type": "object"}},
                "affected_id": {"type": "integer"},
            },
            "required": ["ok", "mine", "hers"],
        },
        "securitySchemes": NOAUTH,
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    },
    {
        "name": "calendar",
        "title": "日历 / 心情 / 经期",
        "description": (
            "Coarse-grained access to calendar events and per-day mood/flow/menstrual state. "
            "action is one of list_events / add_event / update_event / delete_event / set_day / set_menstrual / clear_menstrual / read_day. "
            "Use set_menstrual with date='YYYY-MM-DD' to mark a day; the resident should only do this when the "
            "user has clearly said so — never guess."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "list_events", "add_event", "update_event", "delete_event",
                        "set_day", "set_menstrual", "clear_menstrual", "read_day",
                    ],
                    "default": "list_events",
                },
                "date": {"type": "string", "maxLength": 10},
                "text": {"type": "string", "maxLength": 500},
                "time": {"type": "string", "maxLength": 5},
                "yearly": {"type": "boolean"},
                "special": {"type": "boolean"},
                "event_id": {"type": "integer", "minimum": 1},
                "mood": {"type": "string", "maxLength": 40},
                "flow": {"type": "string", "maxLength": 100},
                "pain": {"type": "integer", "minimum": 0, "maximum": 10},
                "note": {"type": "string", "maxLength": 2000},
                "private": {"type": "string", "maxLength": 2000},
            },
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "ok": {"type": "boolean"},
                "events": {"type": "array", "items": {"type": "object"}},
                "days": {"type": "object"},
                "day": {"type": "object"},
                "affected_id": {"type": "integer"},
            },
            "required": ["ok"],
        },
        "securitySchemes": NOAUTH,
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    },
    {
        "name": "diary",
        "title": "日记（按作者区分）",
        "description": (
            "Coarse-grained access to diary entries. Every entry has an author_type: "
            "'user' (the owner's diary) or 'resident' (the resident's diary). author_type filters list/timeline; "
            "create/update/delete are restricted to resident entries and can never mutate "
            "the user's diary. action is one of list / read / create / update / delete / timeline."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "read", "create", "update", "delete", "timeline"],
                    "default": "list",
                },
                "author_type": {"type": "string", "enum": ["user", "resident"], "default": "resident"},
                "entry_id": {"type": "integer", "minimum": 1},
                "text": {"type": "string", "maxLength": 20000},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 200},
            },
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "ok": {"type": "boolean"},
                "entries": {"type": "array", "items": {"type": "object"}},
                "entry": {"type": "object"},
                "affected_id": {"type": "integer"},
            },
            "required": ["ok"],
        },
        "securitySchemes": NOAUTH,
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    },
    {
        "name": "daily_report",
        "title": "日报与点评",
        "description": (
            "Read the daily report for a date or comment on it. action is one of list / read / save / comment. "
            "save overwrites the report body (rare for the resident); comment stores resident_comment + commented_at."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "read", "save", "comment"], "default": "read"},
                "date": {"type": "string", "maxLength": 10},
                "text": {"type": "string", "maxLength": 30000},
                "comment": {"type": "string", "maxLength": 8000},
            },
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "ok": {"type": "boolean"},
                "dates": {"type": "array", "items": {"type": "string"}},
                "report": {"type": "object"},
            },
            "required": ["ok"],
        },
        "securitySchemes": NOAUTH,
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    },
    {
        "name": "get_day_context",
        "title": "读取一天的完整上下文",
        "description": (
            "Return one day's combined context: calendar events, day state (mood/menstrual/note), "
            "todos due or recently touched, diary entries from both authors, and the daily report + "
            "your existing comment. Use this instead of calling todos/calendar/diary/daily_report "
            "separately when you just need to understand 'what happened today'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "maxLength": 10,
                    "description": "YYYY-MM-DD. Defaults to today in the server's local timezone.",
                },
            },
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "ok": {"type": "boolean"},
                "date": {"type": "string"},
                "calendar_events": {"type": "array", "items": {"type": "object"}},
                "day": {"type": "object"},
                "todos": {"type": "object"},
                "diary_user": {"type": "array", "items": {"type": "object"}},
                "diary_resident": {"type": "array", "items": {"type": "object"}},
                "daily_report": {"type": "object"},
            },
            "required": ["ok", "date"],
        },
        "securitySchemes": NOAUTH,
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "read_dwell_events",
        "title": "读取统一业务事件",
        "description": (
            "Read durable business events after an event cursor. Types include chat.message, "
            "reading.page_changed, todo.updated, calendar.updated, diary.updated, and report.updated."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "after_event_id": {"type": "integer", "minimum": 0, "default": 0},
                "types": {"type": "array", "items": {"type": "string", "maxLength": 120}, "maxItems": 20},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 100},
            },
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "events": {"type": "array", "items": {"type": "object"}},
                "event_cursor": {"type": "integer"},
            },
            "required": ["events", "event_cursor"],
        },
        "securitySchemes": NOAUTH,
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "search_chat_history",
        "title": "检索历史聊天",
        "description": (
            "Keyword, semantic, or hybrid search over past chat messages. Hybrid uses FTS5 + vector "
            "retrieval and reciprocal-rank fusion. Returns only id / date / speaker / snippet / score — "
            "never the full message. Use read_message_context to expand a hit."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 200},
                "mode": {"type": "string", "enum": ["keyword", "semantic", "hybrid"], "default": "hybrid"},
                "speaker": {"type": "string", "enum": ["user", "assistant", "any"], "default": "any"},
                "date_from": {"type": "string", "maxLength": 10, "description": "Inclusive YYYY-MM-DD."},
                "date_to": {"type": "string", "maxLength": 10, "description": "Inclusive YYYY-MM-DD."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string"},
                "results": {"type": "array", "items": {"type": "object"}},
                "semantic": {"type": "object"},
            },
            "required": ["mode", "results", "semantic"],
        },
        "securitySchemes": NOAUTH,
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "read_message_context",
        "title": "读取一条消息的上下文",
        "description": (
            "Expand around a message seq: return up to `before` earlier and `after` later messages. "
            "Use this after search_chat_history to read the actual conversation around a hit instead "
            "of pulling full threads into context."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "seq": {"type": "integer", "minimum": 1},
                "before": {"type": "integer", "minimum": 0, "maximum": 30, "default": 5},
                "after": {"type": "integer", "minimum": 0, "maximum": 30, "default": 5},
            },
            "required": ["seq"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {"messages": {"type": "array", "items": {"type": "object"}}},
            "required": ["messages"],
        },
        "securitySchemes": NOAUTH,
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
]


class ResidentMCP:
    def __init__(
        self,
        db: Database,
        token_file: Path,
        resident_name: str,
        book_dir: Path | None = None,
        thinking: ThinkingBridge | None = None,
        stickers: Any = None,
        reports: DailyReportService | None = None,
        history: HistorySearchService | None = None,
    ):
        self.db = db
        self.token_file = token_file
        self.resident_name = resident_name.strip()[:80] or "驻客"
        self.book_dir = book_dir or db.path.parent / "books"
        self.thinking = thinking
        self.stickers = stickers
        self.reports = reports or DailyReportService(db, db.path.parent / "news")
        self.history = history or HistorySearchService(db)
        self._token_lock = threading.Lock()
        self._token = self._load_or_create_token()
        self._rotation_csrf = secrets.token_urlsafe(32)
        # Candidates from the latest search only. They expire quickly and are
        # consumed after one send so an old process-wide id cannot be reused.
        self._turn_sticker_ids: dict[str, float] = {}
        self._turn_lock = threading.Lock()

    def _read_token(self) -> str:
        value = self.token_file.read_text(encoding="utf-8").strip()
        if not TOKEN_RE.fullmatch(value):
            raise RuntimeError("invalid Dwell MCP connection token file")
        return value

    def _load_or_create_token(self) -> str:
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            value = self._read_token()
        except FileNotFoundError:
            value = secrets.token_urlsafe(32)
            try:
                fd = os.open(self.token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                return self._read_token()
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(value + "\n")
        os.chmod(self.token_file, 0o600)
        return value

    def rotate_token(self) -> None:
        value = secrets.token_urlsafe(32)
        temp = self.token_file.with_name(self.token_file.name + "." + secrets.token_hex(8) + ".tmp")
        with self._token_lock:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(value + "\n")
                os.replace(temp, self.token_file)
                os.chmod(self.token_file, 0o600)
                self._token = value
            finally:
                try:
                    temp.unlink()
                except FileNotFoundError:
                    pass
        self.db.set_setting("assistant_mode", "api")
        self.db.delete_setting("mcp_pending_seq")

    def ensure_resident_chat(self) -> dict[str, Any]:
        """Return one durable chat owned by the MCP resident.

        Older deployments used the globally selected Claude chat for MCP replies.
        When upgrading an active installation, adopt that chat once so its existing
        conversation stays with the resident, then recreate an independent Claude
        chat for the original API-backed entry.
        """
        chat_id = self.db.setting("mcp_chat_id")
        chat = self.db.one("SELECT * FROM chats WHERE id=?", (chat_id,)) if chat_id else None
        if chat:
            if chat["archived"]:
                self.db.execute("UPDATE chats SET archived=0 WHERE id=?", (chat["id"],))
            if chat["name"] != self.resident_name:
                self.db.execute(
                    "UPDATE chats SET name=? WHERE id=?",
                    (self.resident_name, chat["id"]),
                )
            return self.db.one("SELECT * FROM chats WHERE id=?", (chat["id"],)) or chat

        if self.db.setting("assistant_mode", "api") == "mcp":
            chat = self.db.current_chat()
            self.db.execute(
                "UPDATE chats SET name=?, archived=0 WHERE id=?",
                (self.resident_name, chat["id"]),
            )
            claude = self.db.one(
                "SELECT id FROM chats WHERE id<>? AND archived=0 AND name='Claude' "
                "ORDER BY last DESC LIMIT 1",
                (chat["id"],),
            )
            if not claude:
                self.db.create_chat("Claude")
                self.db.set_setting("current_chat", chat["id"])
        else:
            chat = self.db.create_chat(self.resident_name)

        self.db.set_setting("mcp_chat_id", chat["id"])
        return self.db.one("SELECT * FROM chats WHERE id=?", (chat["id"],)) or chat

    def migrate_active_resident_chat(self) -> None:
        if self.db.setting("assistant_mode", "api") == "mcp":
            self.ensure_resident_chat()

    def rotation_csrf(self) -> str:
        return self._rotation_csrf

    def verify_rotation_csrf(self, value: str) -> bool:
        return hmac.compare_digest(value, self._rotation_csrf)

    def connection_url(self, public_base: str) -> str:
        with self._token_lock:
            token = self._token
        return public_base.rstrip("/") + "/" + token + "/mcp"

    def matches_path(self, path: str) -> bool:
        with self._token_lock:
            expected = "/" + self._token + "/mcp"
        return hmac.compare_digest(path, expected)

    @staticmethod
    def redact_request_log(message: str) -> str:
        return re.sub(r"/[A-Za-z0-9_-]{40,120}/mcp", "/[private-link]/mcp", message)

    @staticmethod
    def instructions() -> str:
        return (
            "Call enter_dwell once, keep its cursor, then call wait_for_user_message with "
            "continuous=true. It keeps the resident present and sends heartbeats until a "
            "new message arrives; continuous=false is only for an explicitly finite wait. "
            "After answering, prefer send_dwell_reply_and_wait so sending and re-entering "
            "the wait are one operation. reading_pages are automatic shared-reading events; use their "
            "page text and update the book notebook at a chapter's last page according to "
            "notebook_prompt. Search returns title/summary only; call read_book_note for one "
            "selected body. Reply once to user_messages with send_dwell_reply. Its thinking "
            "fields are required, shown only in Dwell's current Thought process UI, and are "
            "intentionally omitted from resident history reads. A regeneration_request asks "
            "you to answer its user_message again with send_dwell_reply using reply_to_seq; "
            "Dwell will replace that answer and its thinking in their original positions, "
            "without deleting later conversation rounds. The request remains pending until "
            "the replacement is saved. Update the cursor and keep waiting; a wait timeout "
            "or transport reconnect is not permission to end the resident turn. The "
            "connection URL is private and must never be revealed."
        )

    def tools(self) -> list[dict[str, Any]]:
        """Return resident tools with the live thinking MCP's schema descriptions."""
        tools = copy.deepcopy(TOOLS)
        if not self.thinking:
            return tools
        upstream = self.thinking.openai_tool()["function"]["parameters"]
        upstream_properties = upstream.get("properties") or {}
        for reply in (
            tool for tool in tools
            if tool.get("name") in ("send_dwell_reply", "send_dwell_reply_and_wait")
        ):
            properties = reply["inputSchema"]["properties"]
            for key in ("style", "thinking", "effort", "skin"):
                if key in upstream_properties:
                    properties[key] = copy.deepcopy(upstream_properties[key])
            properties["thinking"]["minLength"] = 1
            properties["thinking"]["maxLength"] = 8000
        return tools

    def _active_reading(self) -> dict[str, Any]:
        try:
            value = json.loads(self.db.setting("nook_active_reading") or "{}")
        except (TypeError, ValueError):
            value = {}
        if not isinstance(value, dict):
            return {}
        value.pop("notebook", None)
        slug = str(value.get("slug") or "")
        if slug:
            value["notebook_prompt"] = DEFAULT_NOTEBOOK_PROMPT
            value["notebook_index"] = self.db.search_book_notes(slug, "", 100)
        return value

    def _regeneration_requests(self, chat_id: str) -> list[dict[str, Any]]:
        requests: list[dict[str, Any]] = []
        for row in self.db.query(
            "SELECT seq,text,extra,at FROM messages WHERE chat_id=? AND kind='mcp_regenerate' "
            "ORDER BY seq",
            (chat_id,),
        ):
            try:
                reply_to = int(row.get("text") or 0)
            except (TypeError, ValueError):
                continue
            user = self.db.one(
                "SELECT seq,kind,text,extra,at FROM messages "
                "WHERE chat_id=? AND seq=? AND kind='me'",
                (chat_id, reply_to),
            )
            if not user:
                continue
            try:
                request_extra = json.loads(row.get("extra") or "{}")
            except (TypeError, ValueError):
                request_extra = {}
            requests.append({
                "request_seq": int(row["seq"]),
                "reply_to_seq": reply_to,
                "assistant_seq": int(request_extra.get("assistant_seq") or 0),
                "user_message": _message(user),
                "created_at": _iso(float(row.get("at") or 0)),
            })
        return requests

    def _require_book(self, slug: str) -> dict[str, Any]:
        book = load_book(self.book_dir, slug)
        if not book:
            raise ValueError("book not found")
        return book

    def _enrich_with_related_history(
        self,
        message: dict[str, Any],
        chat_id: str,
        top_k: int = 3,
        min_query_len: int = 3,
        max_age_seconds: float = 180.0,
    ) -> dict[str, Any]:
        """Attach only high-confidence old snippets; full context stays opt-in."""
        text = str(message.get("text") or "").strip()
        if len(text) < min_query_len:
            return message
        try:
            current_seq = int(message.get("seq") or 0)
            current_at = float(message.get("created_at_epoch") or 0)
        except (TypeError, ValueError):
            current_seq, current_at = 0, 0.0
        bigrams = set(_bigrams(text))
        try:
            found = self.history.search(
                text,
                mode="hybrid",
                chat_id=chat_id,
                kinds=("me", "gu"),
                limit=top_k * 6,
            )
        except (OSError, RuntimeError, ValueError):
            found = {"results": [], "semantic": {"available": False}}
        hits = list(found.get("results") or [])
        semantic_available = bool((found.get("semantic") or {}).get("available"))
        if not semantic_available and bigrams:
            try:
                hits = self.db.search_message_bigrams(
                    list(bigrams), chat_id=chat_id, kinds=("me", "gu"), limit=top_k * 6,
                )
            except (OSError, RuntimeError, ValueError):
                hits = []
        related: list[dict[str, Any]] = []
        seen_seqs: set[int] = set()
        for row in hits:
            seq = int(row.get("seq") or 0)
            if seq == current_seq or seq in seen_seqs:
                continue
            at = float(row.get("at") or 0)
            if current_at and abs(current_at - at) < max_age_seconds:
                continue
            similarity = row.get("similarity")
            if semantic_available:
                if similarity is None or float(similarity) < 0.72:
                    continue
                score = float(similarity)
            else:
                candidate_bigrams = set(_bigrams(str(row.get("text") or "")))
                union = bigrams | candidate_bigrams
                score = len(bigrams & candidate_bigrams) / len(union) if union else 0.0
                if score < 0.25:
                    continue
            seen_seqs.add(seq)
            related.append({
                "result_id": seq,
                "date": _iso(at),
                "speaker": "user" if row.get("kind") == "me" else "assistant",
                "snippet": _snippet(str(row.get("text") or ""), text[:40]),
                "score": score,
            })
            if len(related) >= top_k:
                break
        if related:
            message["related_history"] = related
        return message

    def _wait_for_user_message(
        self,
        chat_id: str,
        after: int,
        timeout: int,
        continuous: bool,
        cancel_event: threading.Event | None = None,
        heartbeat: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Wait for visible resident input while keeping a durable audit trail."""
        request_id = secrets.token_hex(16)
        started_at = _iso(time.time())
        scan_after = max(0, int(after))
        timeout = max(0, min(int(timeout), 3600))
        deadline = None if continuous else time.monotonic() + timeout
        buffered: list[dict[str, Any]] = []
        returned_cursor = self.db.latest_message(chat_id)
        timed_out = False
        disconnect_reason = ""
        self.db.begin_mcp_wait(
            request_id,
            chat_id,
            started_at,
            scan_after,
            continuous,
        )
        print(
            "[DWELL_MCP_WAIT_START] "
            + json.dumps(
                {
                    "request_id": request_id,
                    "started_at": started_at,
                    "after_seq": scan_after,
                    "continuous": bool(continuous),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        try:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    disconnect_reason = "client_disconnect"
                    break

                pending_regenerations = self._regeneration_requests(chat_id)
                if pending_regenerations:
                    rows = self.db.messages_after(
                        chat_id,
                        scan_after,
                        100,
                        ("me", "nook", "mcp_regenerate"),
                    )
                    returned_cursor = max(returned_cursor, self.db.latest_message(chat_id))
                else:
                    remaining = 15.0
                    if deadline is not None:
                        remaining = min(remaining, max(0.0, deadline - time.monotonic()))
                    if remaining <= 0:
                        timed_out = True
                        disconnect_reason = "timeout"
                        break
                    returned_cursor, rows = self.db.wait_messages(
                        chat_id,
                        scan_after,
                        remaining,
                        ("me", "nook", "mcp_regenerate"),
                    )
                    pending_regenerations = self._regeneration_requests(chat_id)

                if rows:
                    buffered.extend(rows)
                    scan_after = max(
                        scan_after,
                        max(int(row["seq"]) for row in rows),
                    )
                scan_after = max(scan_after, returned_cursor)

                user_rows = [row for row in buffered if row.get("kind") == "me"]
                reading_rows = [row for row in buffered if row.get("kind") == "nook"]
                if user_rows or reading_rows or pending_regenerations:
                    disconnect_reason = "message_received"
                    break

                if deadline is not None and time.monotonic() >= deadline:
                    timed_out = True
                    disconnect_reason = "timeout"
                    break

                print(
                    "[DWELL_MCP_WAIT_HEARTBEAT] "
                    + json.dumps(
                        {
                            "request_id": request_id,
                            "cursor": returned_cursor,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                if heartbeat is not None:
                    heartbeat(request_id)

            user_rows = [row for row in buffered if row.get("kind") == "me"]
            reading_rows = [row for row in buffered if row.get("kind") == "nook"]
            returned_cursor = max(returned_cursor, self.db.latest_message(chat_id))
            user_messages = [self._enrich_with_related_history(_message(row), chat_id) for row in user_rows]
            return {
                "request_id": request_id,
                "cursor": returned_cursor,
                "timed_out": timed_out,
                "continuous": bool(continuous),
                "disconnect_reason": disconnect_reason,
                "user_messages": user_messages,
                "reading_pages": [_message(row) for row in reading_rows],
                "regeneration_requests": self._regeneration_requests(chat_id),
            }
        except Exception:
            if not disconnect_reason:
                disconnect_reason = "error"
            raise
        finally:
            ended_at = _iso(time.time())
            self.db.finish_mcp_wait(
                request_id,
                ended_at,
                returned_cursor,
                timed_out,
                disconnect_reason,
            )
            print(
                "[DWELL_MCP_WAIT_END] "
                + json.dumps(
                    {
                        "request_id": request_id,
                        "ended_at": ended_at,
                        "after_seq": max(0, int(after)),
                        "returned_cursor": returned_cursor,
                        "timed_out": timed_out,
                        "disconnect_reason": disconnect_reason,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    def call_tool(
        self,
        name: str,
        args: dict[str, Any],
        cancel_event: threading.Event | None = None,
        heartbeat: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        try:
            if name == "enter_dwell":
                chat = self.ensure_resident_chat()
                self.db.set_setting("assistant_mode", "mcp")
                self.db.set_setting("resident_name", self.resident_name)
                self.db.set_setting("current_chat", chat["id"])
                cursor = self.db.latest_message(chat["id"])
                rows = list(reversed(self.db.query(
                    "SELECT seq,kind,text,extra,at FROM messages "
                    "WHERE chat_id=? AND kind IN ('me','gu','nook') "
                    "ORDER BY seq DESC LIMIT 20",
                    (chat["id"],),
                )))
                data = {
                    "mode": "mcp",
                    "resident_name": self.resident_name,
                    "chat_id": chat["id"],
                    "chat_name": chat["name"],
                    "cursor": cursor,
                    "recent_messages": [_message(row) for row in rows],
                    "current_reading": self._active_reading(),
                    "regeneration_requests": self._regeneration_requests(chat["id"]),
                }
                return _tool_result(data, f"{self.resident_name} 已入住 Dwell；当前游标 {cursor}。")
            if self.db.setting("assistant_mode", "api") != "mcp":
                raise ValueError("call enter_dwell before using resident tools")
            chat = self.ensure_resident_chat()
            cursor = self.db.latest_message(chat["id"])
            if name == "read_dwell_messages":
                after = max(0, int(args.get("after_seq") or 0))
                limit = max(1, min(int(args.get("limit") or 100), 200))
                rows = self.db.messages_after(chat["id"], after, limit, ("me", "gu", "nook"))
                cursor = self.db.latest_message(chat["id"])
                data = {
                    "chat_id": chat["id"],
                    "chat_name": chat["name"],
                    "mode": "mcp",
                    "resident_name": self.resident_name,
                    "cursor": cursor,
                    "messages": [_message(row) for row in rows],
                    "current_reading": self._active_reading(),
                }
                return _tool_result(data, f"读取到 {len(rows)} 条消息；当前游标 {cursor}。")
            if name == "read_dwell_events":
                after_event_id = max(0, int(args.get("after_event_id") or 0))
                raw_types = args.get("types") or []
                types = tuple(
                    str(value)[:120]
                    for value in raw_types
                    if isinstance(value, str) and value.strip()
                )
                limit = max(1, min(int(args.get("limit") or 100), 200))
                rows = self.db.domain_events_after(after_event_id, types, limit)
                events = [
                    {
                        "id": int(row["id"]),
                        "type": str(row["type"]),
                        "created_at": _iso(float(row["created_at"])),
                        "actor_type": str(row["actor_type"]),
                        "actor_id": str(row["actor_id"]),
                        "payload": row.get("payload") or {},
                    }
                    for row in rows
                ]
                event_cursor = events[-1]["id"] if events else after_event_id
                return _tool_result(
                    {"events": events, "event_cursor": event_cursor},
                    f"读取到 {len(events)} 个业务事件。",
                )
            if name == "read_attachment":
                attachment_id = str(args.get("attachment_id") or "").strip()
                if not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", attachment_id):
                    raise ValueError("invalid attachment_id")
                row = self.db.one(
                    "SELECT id,type,name,mime,url,size,width,height,duration,data "
                    "FROM attachments WHERE id=? AND chat_id=?",
                    (attachment_id, chat["id"]),
                )
                if not row:
                    raise ValueError("attachment not found")
                metadata = _attachment_metadata(row)
                payload = row.get("data") or b""
                if isinstance(payload, str):
                    payload = payload.encode("utf-8")
                payload = bytes(payload)
                summary = f"已读取附件 {metadata['name']}。"
                if metadata["type"] == "file":
                    text = payload.decode("utf-8", "replace")
                    return _tool_result(
                        {"attachment": metadata, "text": text},
                        summary,
                        [
                            {"type": "text", "text": summary},
                            {"type": "text", "text": text},
                        ],
                    )
                import base64
                encoded = base64.b64encode(payload).decode("ascii")
                if metadata["type"] in ("image", "audio"):
                    block = {
                        "type": metadata["type"],
                        "data": encoded,
                        "mimeType": metadata["mime"],
                    }
                    return _tool_result(
                        {"attachment": metadata},
                        summary,
                        [{"type": "text", "text": summary}, block],
                    )
                return _tool_result(
                    {"attachment": metadata, "data_base64": encoded},
                    summary,
                )
            if name == "send_dwell_reply_and_wait":
                reply_args = {
                    key: args[key]
                    for key in ("reply_to_seq", "text", "style", "thinking", "effort", "skin")
                    if key in args
                }
                sent = self.call_tool(
                    "send_dwell_reply",
                    reply_args,
                    cancel_event=cancel_event,
                    heartbeat=heartbeat,
                )
                if sent.get("isError"):
                    return sent
                sent_data = sent.get("structuredContent") or {}
                waited = self.call_tool(
                    "wait_for_user_message",
                    {
                        "after_seq": int(sent_data.get("cursor") or 0),
                        "timeout_seconds": args.get("timeout_seconds", 45),
                        "continuous": args.get("continuous", True),
                    },
                    cancel_event=cancel_event,
                    heartbeat=heartbeat,
                )
                if waited.get("isError"):
                    return waited
                data = dict(waited.get("structuredContent") or {})
                data["sent_reply"] = sent_data
                return _tool_result(
                    data,
                    "回复已送达 Dwell，已继续驻守。"
                    if not data.get("timed_out")
                    else "回复已送达 Dwell；本次有限等待已结束。",
                )
            if name == "wait_for_user_message":
                after = max(0, int(args.get("after_seq") or 0))
                timeout = max(0, min(int(args.get("timeout_seconds", 45) or 0), 3600))
                continuous = bool(args.get("continuous", True))
                data = self._wait_for_user_message(
                    chat["id"],
                    after,
                    timeout,
                    continuous,
                    cancel_event=cancel_event,
                    heartbeat=heartbeat,
                )
                user_rows = data["user_messages"]
                reading_rows = data["reading_pages"]
                pending_regenerations = data["regeneration_requests"]
                cursor = data["cursor"]
                summary = (
                    f"收到 {len(user_rows)} 条用户消息、{len(reading_rows)} 个共读页和 "
                    f"{len(pending_regenerations)} 个重新回答请求；当前游标 {cursor}。"
                    if user_rows or reading_rows or pending_regenerations
                    else f"没有新消息；本次等待结束，游标为 {cursor}。"
                )
                return _tool_result(data, summary)
            if name == "send_dwell_reply":
                reply_to = int(args.get("reply_to_seq") or 0)
                text = str(args.get("text") or "").strip()
                thinking = str(args.get("thinking") or "").strip()
                style = str(args.get("style") or "")
                effort = str(args.get("effort") or "")
                skin = str(args.get("skin") or "")
                if not text:
                    raise ValueError("text is required")
                if not thinking:
                    raise ValueError("thinking is required")
                if len(text) > 20000 or len(thinking) > 8000:
                    raise ValueError("reply is too long")
                if style not in ("deep_think", "relational"):
                    raise ValueError("style must be deep_think or relational")
                if effort not in ("low", "medium", "high"):
                    raise ValueError("effort must be low, medium, or high")
                if skin not in ("botanical", "microglow"):
                    raise ValueError("skin must be botanical or microglow")
                if not self.thinking:
                    raise RuntimeError("thinking MCP is not configured")
                self.thinking.render({
                    "style": style,
                    "thinking": thinking,
                    "effort": effort,
                    "skin": skin,
                })
                meta = json.dumps(
                    {
                        "source": "gpt-thinking-block-mcp",
                        "style": style,
                        "effort": effort,
                        "skin": skin,
                        "user_visible": True,
                        "resident_history_visible": False,
                    },
                    ensure_ascii=False,
                )
                regeneration = self.db.one(
                    "SELECT seq FROM messages WHERE chat_id=? AND kind='mcp_regenerate' AND text=?",
                    (chat["id"], str(reply_to)),
                )
                regenerated = bool(regeneration)
                regeneration_cursor = int(regeneration["seq"]) if regeneration else 0
                if regenerated:
                    assistant_seq, thinking_seq = self.db.replace_mcp_reply(
                        chat["id"], reply_to, text, thinking, meta
                    )
                    duplicate = False
                    self.db.append_event({
                        "chat_id": chat["id"],
                        "type": "message_regenerated",
                        "message_seq": assistant_seq,
                        "thinking_seq": thinking_seq,
                        "text": text,
                        "thinking": thinking,
                    })
                    self.db.append_event({
                        "chat_id": chat["id"],
                        "type": "result",
                        "source": "mcp",
                        "regenerated": True,
                    })
                else:
                    assistant_seq, _thinking_seq, duplicate = self.db.append_mcp_reply(
                        chat["id"], reply_to, text, thinking, meta
                    )
                if not duplicate and not regenerated:
                    if thinking:
                        self.db.append_event({
                            "chat_id": chat["id"],
                            "type": "stream_event",
                            "event": {"delta": {"type": "thinking_delta", "thinking": thinking}},
                        })
                    for start in range(0, len(text), 120):
                        self.db.append_event({
                            "chat_id": chat["id"],
                            "type": "stream_event",
                            "event": {"delta": {"type": "text_delta", "text": text[start:start + 120]}},
                        })
                    parts: list[dict[str, str]] = []
                    if thinking:
                        parts.append({"type": "thinking", "thinking": thinking})
                    parts.append({"type": "text", "text": text})
                    self.db.append_event({
                        "chat_id": chat["id"],
                        "type": "assistant",
                        "message_seq": assistant_seq,
                        "message": {"content": parts},
                    })
                    self.db.append_event({"chat_id": chat["id"], "type": "result", "source": "mcp"})
                if not duplicate:
                    self.db.append_domain_event(
                        "chat.message_updated" if regenerated else "chat.message",
                        "resident",
                        self.resident_name,
                        {
                            "chat_id": chat["id"],
                            "message_seq": assistant_seq,
                            "reply_to_seq": reply_to,
                            "role": "assistant",
                            "regenerated": regenerated,
                        },
                        idempotency_key=(
                            f"chat.message_updated:{assistant_seq}:{regeneration_cursor}"
                            if regenerated else f"chat.message:{assistant_seq}"
                        ),
                    )
                    pending = int(self.db.setting("mcp_pending_seq", "0") or 0)
                    if pending and pending <= reply_to:
                        self.db.delete_setting("mcp_pending_seq")
                cursor = max(self.db.latest_message(chat["id"]), regeneration_cursor)
                data = {
                    "ok": True,
                    "duplicate": duplicate,
                    "regenerated": regenerated,
                    "assistant_seq": assistant_seq,
                    "cursor": cursor,
                }
                summary = (
                    "回复已存在，未重复发送。" if duplicate
                    else "回答和思考已在原位置刷新。" if regenerated
                    else "回复已送达 Dwell。"
                )
                return _tool_result(data, summary)
            if name == "read_shared_reading":
                current = self._active_reading()
                slug = str(args.get("slug") or current.get("slug") or "")
                if slug:
                    self._require_book(slug)
                data = {
                    "current_reading": current,
                    "slug": slug,
                    "notebook_prompt": DEFAULT_NOTEBOOK_PROMPT if slug else "",
                    "note_index": self.db.search_book_notes(slug, "", 100) if slug else [],
                }
                return _tool_result(data, f"已读取《{slug}》的当前共读页和 {len(data['note_index'])} 条记事目录。" if slug else "当前没有打开的共读书页。")
            if name == "search_book_notes":
                slug = str(args.get("slug") or "")
                self._require_book(slug)
                query = str(args.get("query") or "")[:500]
                limit = max(1, min(int(args.get("limit") or 50), 100))
                results = self.db.search_book_notes(slug, query, limit)
                data = {"slug": slug, "results": results}
                return _tool_result(data, f"检索到 {len(results)} 条记事摘要；按 note_id 读取选中的完整记录。")
            if name == "read_book_note":
                slug = str(args.get("slug") or "")
                self._require_book(slug)
                note = self.db.book_note(slug, int(args.get("note_id") or 0))
                if not note:
                    raise ValueError("note not found")
                data = {"slug": slug, "note": note}
                return _tool_result(data, "已读取这条记事的标题、摘要和正文。")
            if name == "save_book_note":
                slug = str(args.get("slug") or "")
                self._require_book(slug)
                title = str(args.get("title") or "").strip()
                summary = str(args.get("summary") or "").strip()
                body = str(args.get("body") or "").strip()
                if not title:
                    raise ValueError("title is required")
                if len(title) > 240 or len(summary) > 4000 or len(body) > 30000:
                    raise ValueError("note is too long")
                note_id = int(args.get("note_id") or 0)
                pinned = bool(args.get("pinned"))
                client_message_id = str(args.get("client_message_id") or "").strip()[:200]
                note_id, duplicate = self.db.save_book_note_idempotent(
                    slug,
                    "resident",
                    title,
                    summary,
                    body,
                    pinned,
                    client_message_id,
                    note_id,
                )
                data = {
                    "ok": True,
                    "duplicate": duplicate,
                    "note_id": note_id,
                    "note_index": self.db.search_book_notes(slug, "", 100),
                }
                if not duplicate:
                    self.db.append_domain_event(
                        "reading.note_updated",
                        "resident",
                        self.resident_name,
                        {"action": "save", "slug": slug, "note_id": note_id},
                        idempotency_key=f"reading.note_updated:{client_message_id}",
                    )
                return _tool_result(
                    data,
                    "这次记事之前已经保存过，未重复写入。" if duplicate else "本书记事已保存。",
                )
            if name == "delete_book_note":
                slug = str(args.get("slug") or "")
                self._require_book(slug)
                if not self.db.delete_book_note(slug, int(args.get("note_id") or 0)):
                    raise ValueError("note not found")
                self.db.append_domain_event(
                    "reading.note_updated",
                    "resident",
                    self.resident_name,
                    {"action": "delete", "slug": slug, "note_id": int(args.get("note_id") or 0)},
                )
                data = {"ok": True, "note_index": self.db.search_book_notes(slug, "", 100)}
                return _tool_result(data, "本书记事已删除。")
            if name == "search_stickers":
                if not self.stickers:
                    raise RuntimeError("sticker library is not configured")
                query = str(args.get("query") or "").strip()
                if not query:
                    raise ValueError("query is required")
                try:
                    limit = int(args.get("limit") or 8)
                except (TypeError, ValueError):
                    limit = 8
                result = self.stickers.search({"query": query, "limit": max(1, min(limit, 20))})
                candidates = []
                with self._turn_lock:
                    self._turn_sticker_ids.clear()
                    expires_at = time.monotonic() + 300.0
                    for candidate in result.get("candidates") or []:
                        sticker_id = str(candidate.get("id") or "")
                        if sticker_id:
                            self._turn_sticker_ids[sticker_id] = expires_at
                        candidates.append({
                            "id": sticker_id,
                            "description": str(candidate.get("semantic_intent") or candidate.get("visual_description") or ""),
                            "tags": list(candidate.get("tone_tags") or []) + list(candidate.get("use_intents") or []),
                            "ocr_text": str(candidate.get("ocr_text") or ""),
                            "score": candidate.get("score") if isinstance(candidate.get("score"), (int, float)) else 0,
                        })
                # Try to attach preview urls lazily; if send fails for any candidate we just omit url.
                enriched = []
                for candidate in candidates:
                    item = dict(candidate)
                    try:
                        media = self.stickers.send(candidate["id"])
                        item["preview_url"] = media.get("url", "")
                        item["url"] = media.get("url", "")
                    except (OSError, RuntimeError, ValueError):
                        item["preview_url"] = ""
                    enriched.append(item)
                return _tool_result(
                    {"candidates": enriched},
                    f"找到 {len(enriched)} 张候选表情；用 send_sticker(sticker_id) 发送其中一张。",
                )
            if name == "send_sticker":
                if not self.stickers:
                    raise RuntimeError("sticker library is not configured")
                sticker_id = str(args.get("sticker_id") or "").strip()
                if not sticker_id:
                    raise ValueError("sticker_id is required")
                reply_to = int(args.get("reply_to_seq") or 0)
                client_message_id = str(args.get("client_message_id") or "").strip()[:200]
                previous = self.db.proactive_message(client_message_id)
                if previous:
                    if str(previous.get("chat_id") or "") != chat["id"]:
                        raise ValueError("client_message_id belongs to another chat")
                    try:
                        previous_extra = json.loads(previous.get("extra") or "{}")
                    except (TypeError, ValueError):
                        previous_extra = {}
                    previous_sticker = previous_extra.get("sticker") if isinstance(previous_extra, dict) else None
                    if not isinstance(previous_sticker, dict) or str(previous_sticker.get("sticker_id") or "") != sticker_id:
                        raise ValueError("client_message_id was already used for another message")
                    previous_quote = previous_extra.get("quote") if isinstance(previous_extra, dict) else None
                    previous_reply = int(previous_quote.get("message_seq") or 0) if isinstance(previous_quote, dict) else 0
                    return _tool_result(
                        {
                            "ok": True,
                            "duplicate": True,
                            "sticker_id": sticker_id,
                            "url": str(previous_sticker.get("url") or ""),
                            "alt": str(previous_sticker.get("alt") or ""),
                            "message_seq": int(previous["assistant_seq"]),
                            "reply_to_seq": previous_reply,
                            "cursor": max(self.db.latest_message(chat["id"]), int(previous["assistant_seq"])),
                        },
                        "这张表情之前已经发过，未重复发送。",
                    )
                with self._turn_lock:
                    now_mono = time.monotonic()
                    self._turn_sticker_ids = {
                        key: expiry
                        for key, expiry in self._turn_sticker_ids.items()
                        if expiry > now_mono
                    }
                    known = sticker_id in self._turn_sticker_ids
                if not known:
                    raise ValueError("sticker_id is not from the latest unexpired search_stickers result")
                quote: dict[str, Any] | None = None
                if reply_to:
                    target = self.db.one(
                        "SELECT text FROM messages WHERE chat_id=? AND seq=? AND kind='me'",
                        (chat["id"], reply_to),
                    )
                    if not target:
                        raise ValueError("reply_to_seq is not a user message in the current chat")
                    quote = self.db.validate_message_quote(
                        chat["id"],
                        {
                            "message_seq": reply_to,
                            "text": str(target.get("text") or "")[:2000],
                        },
                    )
                media = self.stickers.send(sticker_id)
                alt = re.sub(r"[\]\r\n]+", " ", media.get("alt") or "表情包").strip()
                sticker_markdown = f"![{alt}]({media['url']})"
                extra_data: dict[str, Any] = {"source": "sticker", "sticker": {
                    "sticker_id": sticker_id,
                    "url": media["url"],
                    "alt": alt,
                }}
                if quote:
                    extra_data["quote"] = quote
                extra = json.dumps(extra_data, ensure_ascii=False)
                seq, _thinking_seq, duplicate = self.db.append_resident_message(
                    chat["id"],
                    sticker_markdown,
                    client_message_id=client_message_id,
                    message_extra=extra,
                )
                with self._turn_lock:
                    self._turn_sticker_ids.clear()
                if duplicate:
                    cursor = max(self.db.latest_message(chat["id"]), seq)
                    return _tool_result(
                        {
                            "ok": True,
                            "duplicate": True,
                            "sticker_id": sticker_id,
                            "url": media["url"],
                            "alt": alt,
                            "message_seq": seq,
                            "reply_to_seq": reply_to,
                            "cursor": cursor,
                        },
                        "这张表情之前已经发过，未重复发送。",
                    )
                self.db.append_domain_event(
                    "chat.message",
                    "resident",
                    self.resident_name,
                    {
                        "chat_id": chat["id"],
                        "message_seq": seq,
                        "reply_to_seq": reply_to,
                        "role": "assistant",
                        "sticker_id": sticker_id,
                    },
                    idempotency_key=f"chat.message:{seq}",
                )
                self.db.append_event({
                    "chat_id": chat["id"],
                    "type": "assistant",
                    "message_seq": seq,
                    "message": {"content": [{"type": "text", "text": sticker_markdown}]},
                    "quote": quote,
                })
                self.db.append_event({"chat_id": chat["id"], "type": "result", "source": "mcp"})
                cursor = self.db.latest_message(chat["id"])
                return _tool_result(
                    {
                        "ok": True,
                        "duplicate": False,
                        "sticker_id": sticker_id,
                        "url": media["url"],
                        "alt": alt,
                        "message_seq": seq,
                        "reply_to_seq": reply_to,
                        "cursor": cursor,
                    },
                    "表情包已发出。",
                )
            if name == "send_dwell_message":
                text = str(args.get("text") or "").strip()
                if not text:
                    raise ValueError("text is required")
                if len(text) > 20000:
                    raise ValueError("text is too long")
                client_message_id = str(args.get("client_message_id") or "").strip()[:200]
                reply_to = int(args.get("reply_to_seq") or 0)
                previous = self.db.proactive_message(client_message_id)
                if previous:
                    if str(previous.get("chat_id") or "") != chat["id"]:
                        raise ValueError("client_message_id belongs to another chat")
                    try:
                        previous_extra = json.loads(previous.get("extra") or "{}")
                    except (TypeError, ValueError):
                        previous_extra = {}
                    if isinstance(previous_extra, dict) and previous_extra.get("source") == "sticker":
                        raise ValueError("client_message_id was already used for another message")
                    assistant_seq = int(previous["assistant_seq"])
                    cursor = max(self.db.latest_message(chat["id"]), assistant_seq)
                    return _tool_result({
                        "ok": True,
                        "duplicate": True,
                        "proactive": reply_to <= 0,
                        "assistant_seq": assistant_seq,
                        "reply_to_seq": reply_to,
                        "cursor": cursor,
                    }, "这条消息之前已经发过，未重复发送。")
                if reply_to and not self.db.one(
                    "SELECT 1 FROM messages WHERE seq=? AND chat_id=? AND kind='me'",
                    (reply_to, chat["id"]),
                ):
                    raise ValueError("reply_to_seq is not a user message in the current chat")
                if reply_to:
                    previous_reply = self.db.one(
                        "SELECT assistant_seq FROM mcp_replies WHERE user_seq=?",
                        (reply_to,),
                    )
                    if previous_reply:
                        assistant_seq = int(previous_reply["assistant_seq"])
                        return _tool_result({
                            "ok": True,
                            "duplicate": True,
                            "proactive": False,
                            "assistant_seq": assistant_seq,
                            "reply_to_seq": reply_to,
                            "cursor": max(self.db.latest_message(chat["id"]), assistant_seq),
                        }, "这条用户消息已经回复过，未重复发送。")
                quote: dict[str, Any] | None = None
                raw_quote = args.get("quote")
                if isinstance(raw_quote, dict) and raw_quote.get("message_seq"):
                    quote = self.db.validate_message_quote(chat["id"], raw_quote)
                # thinking contract — same as send_dwell_reply.
                thinking = str(args.get("thinking") or "").strip()
                style = str(args.get("style") or "")
                effort = str(args.get("effort") or "")
                skin = str(args.get("skin") or "")
                if not thinking:
                    raise ValueError("thinking is required")
                if style not in ("deep_think", "relational"):
                    raise ValueError("style must be deep_think or relational")
                if effort not in ("low", "medium", "high"):
                    raise ValueError("effort must be low, medium, or high")
                if skin not in ("botanical", "microglow"):
                    raise ValueError("skin must be botanical or microglow")
                if not self.thinking:
                    raise RuntimeError("thinking MCP is not configured")
                self.thinking.render({
                    "style": style, "thinking": thinking, "effort": effort, "skin": skin,
                })
                meta = json.dumps({
                    "source": "gpt-thinking-block-mcp",
                    "style": style, "effort": effort, "skin": skin,
                    "user_visible": True, "resident_history_visible": False,
                    "proactive": reply_to <= 0,
                }, ensure_ascii=False)
                message_extra_data: dict[str, Any] = {"source": "resident-message"}
                if quote:
                    message_extra_data["quote"] = quote
                message_extra = json.dumps(message_extra_data, ensure_ascii=False)
                assistant_seq, _thinking_seq, duplicate = self.db.append_resident_message(
                    chat["id"],
                    text,
                    thinking=thinking,
                    thinking_extra=meta,
                    reply_to_seq=reply_to,
                    client_message_id=client_message_id,
                    message_extra=message_extra,
                )
                if duplicate:
                    cursor = max(self.db.latest_message(chat["id"]), assistant_seq)
                    return _tool_result({
                        "ok": True,
                        "duplicate": True,
                        "proactive": reply_to <= 0,
                        "assistant_seq": assistant_seq,
                        "reply_to_seq": reply_to,
                        "cursor": cursor,
                    }, "这条消息之前已经发过，未重复发送。")
                self.db.append_domain_event(
                    "chat.message",
                    "resident",
                    self.resident_name,
                    {
                        "chat_id": chat["id"],
                        "message_seq": assistant_seq,
                        "reply_to_seq": reply_to,
                        "role": "assistant",
                        "proactive": reply_to <= 0,
                        "quote": quote,
                    },
                    idempotency_key=f"chat.message:{assistant_seq}",
                )
                # Emit visible events so the user's web UI updates.
                if thinking:
                    self.db.append_event({
                        "chat_id": chat["id"], "type": "stream_event",
                        "event": {"delta": {"type": "thinking_delta", "thinking": thinking}},
                    })
                for start in range(0, len(text), 120):
                    self.db.append_event({
                        "chat_id": chat["id"], "type": "stream_event",
                        "event": {"delta": {"type": "text_delta", "text": text[start:start + 120]}},
                    })
                parts: list[dict[str, str]] = []
                if thinking:
                    parts.append({"type": "thinking", "thinking": thinking})
                parts.append({"type": "text", "text": text})
                self.db.append_event({
                    "chat_id": chat["id"], "type": "assistant",
                    "message_seq": assistant_seq,
                    "message": {"content": parts},
                    "quote": quote,
                })
                self.db.append_event({"chat_id": chat["id"], "type": "result", "source": "mcp"})
                cursor = self.db.latest_message(chat["id"])
                return _tool_result({
                    "ok": True, "duplicate": duplicate, "proactive": reply_to <= 0,
                    "assistant_seq": assistant_seq, "reply_to_seq": reply_to, "cursor": cursor,
                }, "主动消息已发出。" if reply_to <= 0 else "回复已发出。")
            if name == "todos":
                action = str(args.get("action") or "list")
                side = "mine" if str(args.get("side") or "mine") == "mine" else "hers"
                affected = 0
                if action == "list":
                    pass
                elif action == "create":
                    text = str(args.get("text") or "").strip()
                    if not text:
                        raise ValueError("text is required")
                    affected = self.db.todos_add(
                        side, text,
                        at_time=str(args.get("at_time") or ""),
                        due_date=str(args.get("due_date") or ""),
                        fixed=bool(args.get("fixed")),
                        by_who=str(args.get("by_who") or "resident")[:20],
                    )
                elif action == "update":
                    todo_id = int(args.get("id") or 0)
                    if not todo_id:
                        raise ValueError("id is required")
                    ok = self.db.todos_update(
                        side, todo_id,
                        text=args.get("text"),
                        at_time=args.get("at_time"),
                        due_date=args.get("due_date"),
                        fixed=args.get("fixed"),
                    )
                    if not ok:
                        raise ValueError("todo not found")
                    affected = todo_id
                elif action in ("complete", "uncomplete"):
                    todo_id = int(args.get("id") or 0)
                    if not todo_id:
                        raise ValueError("id is required")
                    if not self.db.todos_set_done(side, todo_id, action == "complete"):
                        raise ValueError("todo not found")
                    affected = todo_id
                elif action == "delete":
                    todo_id = int(args.get("id") or 0)
                    if not todo_id:
                        raise ValueError("id is required")
                    if not self.db.todos_delete(side, todo_id):
                        raise ValueError("todo not found")
                    affected = todo_id
                else:
                    raise ValueError("unknown todo action")
                if affected:
                    self.db.append_domain_event(
                        "todo.updated",
                        "resident",
                        self.resident_name,
                        {"action": action, "side": side, "todo_id": affected},
                    )
                payload = self.db.todos_list()
                return _tool_result(
                    {"ok": True, "mine": payload["mine"], "hers": payload["hers"], "affected_id": affected},
                    f"待办已更新（{side} #{affected}）。" if affected else "已读取待办列表。",
                )
            if name == "calendar":
                action = str(args.get("action") or "list_events")
                date = str(args.get("date") or "")[:10]
                affected = 0
                if action == "list_events":
                    pass
                elif action == "add_event":
                    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                        raise ValueError("valid date is required")
                    text = str(args.get("text") or "").strip()
                    if not text:
                        raise ValueError("text is required")
                    affected = self.db.calendar_add_event(
                        date, text,
                        time_text=str(args.get("time") or ""),
                        yearly=bool(args.get("yearly")),
                        special=bool(args.get("special")),
                    )
                elif action == "delete_event":
                    event_id = int(args.get("event_id") or args.get("id") or 0)
                    if not event_id:
                        raise ValueError("event_id is required")
                    if not self.db.calendar_delete_event(event_id):
                        raise ValueError("event not found")
                    affected = event_id
                elif action == "update_event":
                    event_id = int(args.get("event_id") or args.get("id") or 0)
                    if not event_id:
                        raise ValueError("event_id is required")
                    if not self.db.calendar_update_event(
                        event_id,
                        date=date if args.get("date") is not None else None,
                        text=str(args.get("text")) if args.get("text") is not None else None,
                        time_text=str(args.get("time")) if args.get("time") is not None else None,
                        yearly=bool(args.get("yearly")) if args.get("yearly") is not None else None,
                        special=bool(args.get("special")) if args.get("special") is not None else None,
                    ):
                        raise ValueError("event not found")
                    affected = event_id
                elif action == "set_day":
                    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                        raise ValueError("valid date is required")
                    self.db.calendar_upsert_day(
                        date,
                        mood=args.get("mood"),
                        flow=args.get("flow"),
                        pain=args.get("pain"),
                        note=args.get("note"),
                        private=args.get("private"),
                    )
                elif action == "set_menstrual":
                    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                        raise ValueError("valid date is required")
                    self.db.calendar_upsert_day(date, menstrual=True)
                elif action == "clear_menstrual":
                    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                        raise ValueError("valid date is required")
                    self.db.calendar_upsert_day(date, menstrual=False)
                elif action == "read_day":
                    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                        raise ValueError("valid date is required")
                    events = [e for e in self.db.calendar_events() if e["date"] == date]
                    days = self.db.calendar_day_states()
                    return _tool_result(
                        {"ok": True, "events": events, "day": days.get(date, {})},
                        f"已读取 {date} 的日历状态。",
                    )
                else:
                    raise ValueError("unknown calendar action")
                if action != "list_events":
                    self.db.append_domain_event(
                        "calendar.updated",
                        "resident",
                        self.resident_name,
                        {"action": action, "date": date, "event_id": affected},
                    )
                return _tool_result(
                    {
                        "ok": True,
                        "events": self.db.calendar_events(),
                        "days": self.db.calendar_day_states(),
                        "affected_id": affected,
                    },
                    f"日历已更新（#{affected}）。" if affected else "已读取日历。",
                )
            if name == "diary":
                action = str(args.get("action") or "list")
                author_type = "resident" if str(args.get("author_type") or "resident") == "resident" else "user"
                affected = 0
                if action in ("list", "timeline"):
                    entries = self.db.diary_entries(author_type=author_type, limit=int(args.get("limit") or 200))
                    return _tool_result(
                        {"ok": True, "entries": [_diary_entry(e) for e in entries]},
                        f"已读取 {len(entries)} 条日记（author={author_type}）。",
                    )
                if action == "read":
                    entry_id = int(args.get("entry_id") or 0)
                    if not entry_id:
                        raise ValueError("entry_id is required")
                    entry = self.db.diary_entry(entry_id)
                    if not entry:
                        raise ValueError("entry not found")
                    return _tool_result(
                        {"ok": True, "entry": _diary_entry(entry)},
                        "已读取这条日记。",
                    )
                if action == "create":
                    if author_type != "resident":
                        raise ValueError("resident cannot write the user's diary")
                    text = str(args.get("text") or "").strip()
                    if not text:
                        raise ValueError("text is required")
                    affected = self.db.add_diary_entry(
                        text,
                        author_type="resident",
                        author_id="resident",
                    )
                    entry = self.db.diary_entry(affected) or {}
                    self.db.append_domain_event(
                        "diary.updated",
                        "resident",
                        self.resident_name,
                        {"action": "create", "entry_id": affected, "author_type": "resident"},
                    )
                    return _tool_result(
                        {"ok": True, "entry": _diary_entry(entry), "affected_id": affected},
                        "已写一条 resident 日记。",
                    )
                if action == "update":
                    entry_id = int(args.get("entry_id") or 0)
                    if not entry_id:
                        raise ValueError("entry_id is required")
                    text = str(args.get("text") or "").strip()
                    if not text:
                        raise ValueError("text is required")
                    if not self.db.update_diary_entry(
                        entry_id,
                        text,
                        required_author_type="resident",
                    ):
                        raise ValueError("resident diary entry not found")
                    entry = self.db.diary_entry(entry_id) or {}
                    self.db.append_domain_event(
                        "diary.updated",
                        "resident",
                        self.resident_name,
                        {"action": "update", "entry_id": entry_id, "author_type": "resident"},
                    )
                    return _tool_result(
                        {"ok": True, "entry": _diary_entry(entry), "affected_id": entry_id},
                        "日记已更新。",
                    )
                if action == "delete":
                    entry_id = int(args.get("entry_id") or 0)
                    if not entry_id:
                        raise ValueError("entry_id is required")
                    if not self.db.delete_diary_entry(
                        entry_id,
                        required_author_type="resident",
                    ):
                        raise ValueError("resident diary entry not found")
                    self.db.append_domain_event(
                        "diary.updated",
                        "resident",
                        self.resident_name,
                        {"action": "delete", "entry_id": entry_id, "author_type": "resident"},
                    )
                    return _tool_result({"ok": True, "affected_id": entry_id}, "日记已删除。")
                raise ValueError("unknown diary action")
            if name == "daily_report":
                action = str(args.get("action") or "read")
                date = str(args.get("date") or _today_local())[:10]
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                    raise ValueError("valid date is required")
                if action == "list":
                    dates = self.reports.dates()
                    return _tool_result({"ok": True, "dates": dates}, f"共 {len(dates)} 天日报。")
                if action == "read":
                    row = self.reports.read(date) or {}
                    return _tool_result({"ok": True, "report": _daily_report(row, date)}, f"已读取 {date} 日报。")
                if action == "save":
                    text = str(args.get("text") or "").strip()
                    if not text:
                        raise ValueError("text is required")
                    row = self.reports.save(date, text)
                    self.db.append_domain_event(
                        "report.updated",
                        "resident",
                        self.resident_name,
                        {"action": "save", "date": date},
                    )
                    return _tool_result({"ok": True, "report": _daily_report(row, date)}, "日报已保存。")
                if action == "comment":
                    comment = str(args.get("comment") or "").strip()
                    if not comment:
                        raise ValueError("comment is required")
                    row = self.reports.comment(date, comment)
                    self.db.append_domain_event(
                        "report.updated",
                        "resident",
                        self.resident_name,
                        {"action": "comment", "date": date},
                    )
                    return _tool_result({"ok": True, "report": _daily_report(row, date)}, "已写点评。")
                raise ValueError("unknown daily_report action")
            if name == "get_day_context":
                date = str(args.get("date") or _today_local())[:10]
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                    raise ValueError("valid date is required")
                events = [e for e in self.db.calendar_events() if e["date"] == date]
                days = self.db.calendar_day_states()
                todos = self.db.todos_list()
                # Dated todos belong to their due date. Undated open items are
                # carried only into today's context; historical dates use creation.
                today_todos = {"mine": [], "hers": []}
                today_unix_min, today_unix_max = _day_bounds(date)
                is_today = date == _today_local()
                for side in ("mine", "hers"):
                    for item in todos[side]:
                        due_date = str(item.get("due_date") or "")
                        created = float(item.get("created") or 0)
                        due_today = due_date == date
                        created_today = today_unix_min <= created < today_unix_max
                        undated_carry = is_today and not due_date
                        not_done = not item.get("done")
                        if (due_today or created_today or undated_carry) and not_done:
                            today_todos[side].append(item)
                diary_user = self.db.diary_entries(author_type="user", date_from=today_unix_min, date_to=today_unix_max)
                diary_resident = self.db.diary_entries(author_type="resident", date_from=today_unix_min, date_to=today_unix_max)
                row = self.reports.read(date) or {}
                return _tool_result({
                    "ok": True,
                    "date": date,
                    "calendar_events": events,
                    "day": days.get(date, {}),
                    "todos": today_todos,
                    "diary_user": [_diary_entry(e) for e in diary_user],
                    "diary_resident": [_diary_entry(e) for e in diary_resident],
                    "daily_report": _daily_report(row, date),
                }, f"已读取 {date} 的上下文。")
            if name == "search_chat_history":
                query = str(args.get("query") or "").strip()
                if not query:
                    raise ValueError("query is required")
                mode = str(args.get("mode") or "hybrid")
                speaker = str(args.get("speaker") or "any")
                kinds: tuple[str, ...]
                if speaker == "user":
                    kinds = ("me",)
                elif speaker == "assistant":
                    kinds = ("gu",)
                else:
                    kinds = ("me", "gu")
                limit = max(1, min(int(args.get("limit") or 10), 50))
                raw_date_from = str(args.get("date_from") or "").strip()
                raw_date_to = str(args.get("date_to") or "").strip()
                date_from: float | None = None
                date_to: float | None = None
                if raw_date_from:
                    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_date_from):
                        raise ValueError("date_from must be YYYY-MM-DD")
                    try:
                        datetime.strptime(raw_date_from, "%Y-%m-%d")
                    except ValueError as exc:
                        raise ValueError("date_from must be a real YYYY-MM-DD date") from exc
                    date_from = _day_bounds(raw_date_from)[0]
                if raw_date_to:
                    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_date_to):
                        raise ValueError("date_to must be YYYY-MM-DD")
                    try:
                        datetime.strptime(raw_date_to, "%Y-%m-%d")
                    except ValueError as exc:
                        raise ValueError("date_to must be a real YYYY-MM-DD date") from exc
                    date_to = _day_bounds(raw_date_to)[1]
                if date_from is not None and date_to is not None and date_from >= date_to:
                    raise ValueError("date_from must not be after date_to")
                found = self.history.search(
                    query,
                    mode=mode,
                    chat_id=chat["id"],
                    kinds=kinds,
                    date_from=date_from,
                    date_to=date_to,
                    limit=limit,
                )
                results = []
                for row in found["results"]:
                    snippet = _snippet(str(row.get("text") or ""), query)
                    item = {
                        "id": int(row["seq"]),
                        "seq": int(row["seq"]),
                        "created_at": _iso(float(row.get("at") or 0)),
                        "speaker": "user" if row.get("kind") == "me" else "assistant" if row.get("kind") == "gu" else "reading",
                        "snippet": snippet,
                        "score": float(row.get("score") or 0),
                        "match_type": str(row.get("match_type") or "message"),
                        "sources": list(row.get("sources") or []),
                    }
                    if row.get("similarity") is not None:
                        item["similarity"] = float(row["similarity"])
                    if row.get("segment_end_seq") is not None:
                        item["segment_end_seq"] = int(row["segment_end_seq"])
                    results.append(item)
                data = {
                    "mode": mode,
                    "results": results,
                    "semantic": found.get("semantic") or {"available": False},
                }
                return _tool_result(data, f"检索到 {len(results)} 条聊天记录。")
            if name == "read_message_context":
                target_seq = int(args.get("seq") or 0)
                if target_seq <= 0:
                    raise ValueError("seq is required")
                before = max(0, min(int(args.get("before") or 5), 30))
                after = max(0, min(int(args.get("after") or 5), 30))
                rows = self.db.message_context(chat["id"], target_seq, before, after)
                return _tool_result(
                    {"messages": [_message(row) for row in rows]},
                    f"读取到 {len(rows)} 条上下文消息。",
                )
            raise ValueError(f"unknown tool: {name}")
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return _tool_error(str(exc))

    def handle_rpc(
        self,
        request: dict[str, Any],
        cancel_event: threading.Event | None = None,
        heartbeat: Callable[[str], None] | None = None,
    ) -> dict[str, Any] | None:
        method = request.get("method")
        rid = request.get("id")
        if rid is None:
            return None
        if method == "initialize":
            requested = str((request.get("params") or {}).get("protocolVersion") or "")
            version = requested if requested == PROTOCOL_VERSION else PROTOCOL_VERSION
            return {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "dwell-resident-mcp", "version": "1.0.0"},
                    "instructions": self.instructions(),
                },
            }
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": rid, "result": {"tools": self.tools()}}
        if method == "tools/call":
            params = request.get("params") or {}
            args = params.get("arguments") or {}
            result = (
                self.call_tool(
                    str(params.get("name") or ""),
                    args,
                    cancel_event=cancel_event,
                    heartbeat=heartbeat,
                )
                if isinstance(args, dict)
                else _tool_error("arguments must be an object")
            )
            return {"jsonrpc": "2.0", "id": rid, "result": result}
        if method == "ping":
            return {"jsonrpc": "2.0", "id": rid, "result": {}}
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "error": {"code": -32601, "message": f"method not found: {method}"},
        }
