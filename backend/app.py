#!/usr/bin/env python3
"""Dependency-free backend for the dwell single-page frontend.

It serves the static UI, persists the app's data in SQLite, exposes every API
surface referenced by ``web/index.html``, and connects an OpenAI-compatible
model to the frontend's durable long-poll event stream.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import mimetypes
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .provider import (
    EmbeddingConfig,
    EmbeddingProvider,
    OpenAIProvider,
    ProviderConfig,
    ProviderError,
)
from .daily_report import DailyReportService
from .history_search import HistorySearchService, ProviderEmbedder
from .nook import DEFAULT_NOTEBOOK_PROMPT, page_context, paginate
from .push import configured as push_configured
from .push import send as send_push
from .resident_mcp import ResidentMCP
from .sticker_bridge import DEFAULT_STICKER_MCP_URL, StickerBridge
from .store import Database
from .thinking_bridge import ThinkingBridge


ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT / "web"
DATA_DIR = Path(os.environ.get("DWELL_DATA_DIR", ROOT / "data")).resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
BOOK_DIR = DATA_DIR / "books"
NEWS_DIR = DATA_DIR / "news"
DB = Database(DATA_DIR / "dwell.sqlite3")
REPORTS = DailyReportService(DB, NEWS_DIR)
_embedding_model = os.environ.get("DWELL_EMBEDDING_MODEL", "").strip()
_embedding_base = (
    os.environ.get("DWELL_EMBEDDING_BASE", "").strip()
    or DB.setting("embedding_base").strip()
    or os.environ.get("DWELL_API_BASE", "").strip()
)
_embedding_token = (
    os.environ.get("DWELL_EMBEDDING_TOKEN", "").strip()
    or DB.setting("embedding_token").strip()
    or os.environ.get("DWELL_API_TOKEN", "").strip()
)
_embedding_model = _embedding_model or DB.setting("embedding_model").strip()
_embedder = None
if _embedding_model and _embedding_base and _embedding_token:
    _embedder = ProviderEmbedder(
        EmbeddingProvider(
            EmbeddingConfig(
                base=_embedding_base,
                token=_embedding_token,
                model=_embedding_model,
            )
        ),
        _embedding_model,
    )
HISTORY = HistorySearchService(DB, _embedder)
THINKING = ThinkingBridge(
    os.environ.get("THINKING_MCP_URL", "http://127.0.0.1:8787/mcp")
)
STICKERS = StickerBridge(
    os.environ.get("DWELL_STICKER_MCP_URL", DEFAULT_STICKER_MCP_URL)
)
MCP = ResidentMCP(
    DB,
    Path(os.environ.get("DWELL_MCP_TOKEN_FILE", DATA_DIR / "mcp-route-token")),
    os.environ.get("DWELL_MCP_RESIDENT_NAME", "驻客"),
    BOOK_DIR,
    THINKING,
    STICKERS,
    REPORTS,
    HISTORY,
)
STARTED_AT = time.time()
APP_VERSION = os.environ.get("DWELL_VERSION", "dwell-backend-v1")
REPO_ROOT = Path(os.environ.get("DWELL_REPO_PATH", ROOT)).resolve()
APP_PASSWORD = os.environ.get("DWELL_PASSWORD", "")
MAX_BODY = int(os.environ.get("DWELL_MAX_BODY", str(6 * 1024 * 1024)))
# The browser sends book text inside JSON. Leave room for JSON escaping while
# keeping the larger request limit scoped to the book endpoint only.
BOOK_MAX_BYTES = 50 * 1024 * 1024
BOOK_REQUEST_MAX_BODY = BOOK_MAX_BYTES * 2 + 1024 * 1024
MCP_PUBLIC_BASE = os.environ.get("DWELL_MCP_PUBLIC_BASE", "").rstrip("/")
MCP_OWNER_CHECK_URL = os.environ.get("DWELL_MCP_OWNER_CHECK_URL", "")
MCP_OWNER_USER_ID = os.environ.get("DWELL_MCP_OWNER_USER_ID", "")
MCP_OWNER_TRUST_PROXY = os.environ.get("DWELL_MCP_OWNER_TRUST_PROXY", "0") == "1"
CN_TZ = timezone(timedelta(hours=8))

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
BOOK_DIR.mkdir(parents=True, exist_ok=True)
NEWS_DIR.mkdir(parents=True, exist_ok=True)

_busy_lock = threading.Lock()
_busy: dict[str, threading.Event] = {}
_wake_lock = threading.Lock()


def now_cn() -> datetime:
    return datetime.now(CN_TZ)


def json_file(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def safe_child(root: Path, raw: str) -> Path:
    value = urllib.parse.unquote(raw or "").replace("\\", "/").lstrip("/")
    candidate = (root / value).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("path escapes configured root")
    return candidate


def external_prefix(headers: Any) -> str:
    """Return a trusted path prefix supplied by the local reverse proxy."""
    raw = str(headers.get("X-Forwarded-Prefix") or "").strip()
    if not raw:
        return ""
    value = "/" + raw.strip("/")
    if not re.fullmatch(r"/[A-Za-z0-9._~!$&'()*+,;=:@%/-]+", value):
        return ""
    return value


def assistant_mode(chat_id: str | None = None) -> str:
    target = chat_id or DB.current_chat()["id"]
    active = DB.setting("assistant_mode", "api") == "mcp"
    return "mcp" if active and DB.setting("mcp_chat_id") == target else "api"


def resident_chat_without_switching() -> dict[str, Any]:
    """Resolve the resident chat without moving the user's selected Recents entry."""
    selected = DB.setting("current_chat")
    chat = MCP.ensure_resident_chat()
    if selected and selected != chat["id"]:
        DB.set_setting("current_chat", selected)
    return chat


def mcp_public_base(headers: Any) -> str:
    if MCP_PUBLIC_BASE:
        return MCP_PUBLIC_BASE
    proto = str(headers.get("X-Forwarded-Proto") or "http").lower()
    if proto not in ("http", "https"):
        proto = "http"
    host = str(headers.get("Host") or "127.0.0.1").strip()
    if not re.fullmatch(r"[A-Za-z0-9.-]+(?::[0-9]{1,5})?", host):
        host = "127.0.0.1"
    return proto + "://" + host + "/dwell-mcp"


def mcp_owner_allowed(headers: Any) -> bool:
    """Allow the private-link page only to the configured Dwell owner."""
    if MCP_OWNER_CHECK_URL and MCP_OWNER_USER_ID:
        cookie = str(headers.get("Cookie") or "")
        if not cookie:
            return False
        request = urllib.request.Request(
            MCP_OWNER_CHECK_URL,
            headers={"Cookie": cookie, "Accept": "application/json"},
        )
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(request, timeout=3) as response:
                payload = json.loads(response.read() or b"{}")
            user = payload.get("user") or {}
            return hmac.compare_digest(str(user.get("id") or ""), MCP_OWNER_USER_ID)
        except (OSError, ValueError, TypeError):
            return False
    return MCP_OWNER_TRUST_PROXY


API_PROFILE_KEY = "api_profiles"
API_ACTIVE_PROFILE_KEY = "api_active_profile"
API_PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")


def _api_profiles() -> list[dict[str, str]]:
    """Read saved OpenAI-compatible endpoints without exposing their tokens."""
    raw = DB.setting(API_PROFILE_KEY)
    profiles: list[dict[str, str]] = []
    try:
        values = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        values = []
    if isinstance(values, list):
        for value in values:
            if not isinstance(value, dict):
                continue
            profile_id = str(value.get("id") or "").strip()
            base = str(value.get("base") or "").strip()
            token = str(value.get("token") or "").strip()
            if not API_PROFILE_ID_RE.fullmatch(profile_id) or not base or not token:
                continue
            profiles.append({
                "id": profile_id,
                "name": str(value.get("name") or profile_id)[:80],
                "base": base[:500],
                "token": token[:4096],
            })
    if profiles:
        return profiles

    # Migrate the original single saved endpoint in memory.  It is persisted
    # the next time the user saves a profile, so old installations keep working
    # without a schema migration or a token being sent to the browser.
    base = DB.setting("api_base").strip()
    token = DB.setting("api_token").strip()
    if base and token:
        return [{"id": "legacy", "name": "当前配置", "base": base[:500], "token": token[:4096]}]
    return []


def _active_api_profile(profiles: list[dict[str, str]] | None = None) -> dict[str, str] | None:
    values = profiles if profiles is not None else _api_profiles()
    if not values:
        return None
    active_id = DB.setting(API_ACTIVE_PROFILE_KEY)
    for profile in values:
        if profile["id"] == active_id:
            return profile
    return values[0]


def _save_api_profiles(profiles: list[dict[str, str]], active_id: str = "") -> None:
    DB.set_setting(API_PROFILE_KEY, json.dumps(profiles, ensure_ascii=False, separators=(",", ":")))
    if active_id:
        DB.set_setting(API_ACTIVE_PROFILE_KEY, active_id)
    else:
        DB.delete_setting(API_ACTIVE_PROFILE_KEY)


def _sync_legacy_api_settings(profile: dict[str, str] | None) -> None:
    if profile:
        DB.set_setting("api_base", profile["base"])
        DB.set_setting("api_token", profile["token"])
    else:
        DB.delete_setting("api_base")
        DB.delete_setting("api_token")


def _public_api_profiles(profiles: list[dict[str, str]], active_id: str = "") -> list[dict[str, Any]]:
    return [
        {
            "id": profile["id"],
            "name": profile["name"],
            "base": profile["base"],
            "active": profile["id"] == active_id,
            "has_token": bool(profile["token"]),
        }
        for profile in profiles
    ]


def api_authmode_payload() -> dict[str, Any]:
    profiles = _api_profiles()
    active = _active_api_profile(profiles)
    cfg = provider_config()
    return {
        "mode": "api" if cfg.token else "none",
        "base": cfg.base or "",
        "model": cfg.model or "",
        "active": active["id"] if active else "",
        "profiles": _public_api_profiles(profiles, active["id"] if active else ""),
        "models": {"model_opus": cfg.model},
        "embedding": embedding_authmode_payload(),
    }


def provider_config(second: bool = False) -> ProviderConfig:
    prefix = "SECOND_" if second else ""
    env_prefix = "DWELL_SECOND_" if second else "DWELL_"
    base = os.environ.get(env_prefix + "API_BASE") or DB.setting(prefix + "api_base")
    token = os.environ.get(env_prefix + "API_TOKEN") or DB.setting(prefix + "api_token")
    if not second and not (os.environ.get("DWELL_API_BASE") or os.environ.get("DWELL_API_TOKEN")):
        active = _active_api_profile()
        if active:
            base, token = active["base"], active["token"]
    model = (
        os.environ.get(env_prefix + "MODEL")
        or DB.setting(prefix + "model")
        or (DB.setting("model") if second else "")
    )
    effort = DB.setting(prefix + "effort", DB.setting("effort", "high"))
    return ProviderConfig(base=base, token=token, model=model, effort=effort)


def embedding_config() -> EmbeddingConfig:
    """Resolve env-managed or owner-saved OpenAI-compatible embeddings."""
    base = (
        os.environ.get("DWELL_EMBEDDING_BASE", "").strip()
        or DB.setting("embedding_base").strip()
    )
    token = (
        os.environ.get("DWELL_EMBEDDING_TOKEN", "").strip()
        or DB.setting("embedding_token").strip()
    )
    model = (
        os.environ.get("DWELL_EMBEDDING_MODEL", "").strip()
        or DB.setting("embedding_model").strip()
    )
    if model and (not base or not token):
        chat = provider_config()
        base = base or chat.base
        token = token or chat.token
    return EmbeddingConfig(base=base, token=token, model=model)


def reload_history_embedder() -> None:
    cfg = embedding_config()
    if cfg.base and cfg.token and cfg.model:
        HISTORY.set_embedder(
            ProviderEmbedder(EmbeddingProvider(cfg), cfg.model)
        )
    else:
        HISTORY.set_embedder(None)


def embedding_authmode_payload() -> dict[str, Any]:
    cfg = embedding_config()
    return {
        "base": cfg.base or "",
        "model": cfg.model or "",
        "has_token": bool(cfg.token),
        "configured": bool(cfg.base and cfg.token and cfg.model),
        "semantic_available": HISTORY.semantic_available,
        "env_managed": any(
            os.environ.get(key)
            for key in (
                "DWELL_EMBEDDING_BASE",
                "DWELL_EMBEDDING_TOKEN",
                "DWELL_EMBEDDING_MODEL",
            )
        ),
    }


def system_prompt(second: bool = False) -> str:
    name = "second_persona.md" if second else "system_prompt.md"
    path = DATA_DIR / name
    if path.exists():
        prompt = path.read_text(encoding="utf-8", errors="replace")[:50000]
    elif second:
        prompt = "你是住在另一间房里的独立助手。保持自己的连续性，诚实、简洁，不冒充主助手。"
    else:
        prompt = (
            "你是这个私人空间里的常驻助手。用用户正在使用的语言自然回答。"
            "不要声称执行过没有执行的动作，不泄露令牌或私密数据。"
            "非简单问题应先调用 render_thinking_block，给出可见、可推翻的工作摘要，"
            "再给最终回答；摘要不是隐藏思维链。"
        )
    if not second:
        prompt += (
            "\n\n主聊天里可以自然地发一张表情包，但不要为了装饰而滥用。"
            "确实适合当前语境时，先调用 search_stickers，再从本轮真实候选中选一个 "
            "sticker_id 调用 send_sticker；禁止编造 ID，一轮最多一张。"
            "send_sticker 成功后，图片会由 Dwell 自己显示，不要重复贴 URL，也不用解释工具过程。"
        )
    return prompt


def model_history(chat_id: str) -> list[dict[str, Any]]:
    rows = DB.query(
        "SELECT kind,text FROM messages WHERE chat_id=? AND kind IN ('me','gu') "
        "ORDER BY seq DESC LIMIT 80",
        (chat_id,),
    )
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt()}]
    for row in reversed(rows):
        messages.append(
            {"role": "user" if row["kind"] == "me" else "assistant", "content": row["text"]}
        )
    return messages


def emit_thinking(chat_id: str, text: str, meta: dict[str, Any] | None = None) -> None:
    if not text:
        return
    DB.append_message("think", text, json.dumps(meta or {}, ensure_ascii=False), chat_id)
    DB.append_event(
        {
            "chat_id": chat_id,
            "type": "stream_event",
            "event": {"delta": {"type": "thinking_delta", "thinking": text}},
        }
    )


def emit_text_delta(chat_id: str, text: str) -> None:
    if text:
        DB.append_event(
            {
                "chat_id": chat_id,
                "type": "stream_event",
                "event": {"delta": {"type": "text_delta", "text": text}},
            }
        )


def chunk_text(text: str, size: int = 120) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


def heartbeat_once(at: datetime | None = None) -> bool:
    """Run one guarded nighttime wake attempt; return whether one completed."""
    if DB.setting("wake_on", "0") != "1" or not _wake_lock.acquire(blocking=False):
        return False
    try:
        now = at or now_cn()
        mins = now.hour * 60 + now.minute
        if os.environ.get("DWELL_WAKE_WINDOW") != "all" and not (
            mins >= 23 * 60 + 30 or mins < 6 * 60 + 30
        ):
            return False
        chat = DB.current_chat()
        if assistant_mode(chat["id"]) == "mcp":
            api_chat = DB.one(
                "SELECT * FROM chats WHERE archived=0 AND id<>? ORDER BY last DESC LIMIT 1",
                (chat["id"],),
            )
            if not api_chat:
                return False
            chat = api_chat
        with _busy_lock:
            if chat["id"] in _busy:
                return False
        last_user = DB.one(
            "SELECT MAX(at) AS at FROM messages WHERE chat_id=? AND kind='me'",
            (chat["id"],),
        )
        last_at = float((last_user or {}).get("at") or 0)
        quiet = float(os.environ.get("DWELL_WAKE_QUIET_SECONDS", str(40 * 60)))
        if not last_at or time.time() - last_at < quiet:
            return False

        state_path = DATA_DIR / "wake.json"
        state = json_file(state_path, {})
        if not isinstance(state, dict):
            state = {}
        night = (now - timedelta(hours=12)).strftime("%Y-%m-%d")
        if state.get("night") != night:
            state = {"night": night, "count": 0, "last": 0}
        wake_max = max(0, int(os.environ.get("DWELL_WAKE_MAX", "2")))
        gap = float(os.environ.get("DWELL_WAKE_GAP_SECONDS", str(190 * 60)))
        if int(state.get("count") or 0) >= wake_max:
            return False
        if time.time() - float(state.get("last") or 0) < gap:
            return False

        cfg = provider_config()
        if not (cfg.base and cfg.token and cfg.model):
            state["last_error"] = "模型 API 尚未配置"
            write_json(state_path, state)
            return False
        count = int(state.get("count") or 0) + 1
        prompt = (
            f"【夜间心跳】没人叫你，这是你自己的时间（今晚第 {count} 次，最多 {wake_max} 次）。"
            "结合这段对话，只挑一件值得记下的小事。请直接写一张简短夜间便条；"
            "不要假装执行文件、搜索或外部动作，也不要输出隐藏推理。"
        )
        message = OpenAIProvider(cfg, timeout=90).complete(
            [*model_history(chat["id"]), {"role": "user", "content": prompt}],
            max_tokens=500,
        )
        reply = str(message.get("content") or "").strip()[:4000]
        if not reply:
            return False

        DB.append_message("gu", reply, chat_id=chat["id"])
        DB.execute("INSERT INTO notes(who,text,at) VALUES('gu',?,?)", (reply, time.time()))
        DB.append_event(
            {
                "chat_id": chat["id"],
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": reply}]},
            }
        )
        DB.append_event({"chat_id": chat["id"], "type": "result", "heartbeat": True})

        days = json_file(DATA_DIR / "night.json", [])
        if not isinstance(days, list):
            days = []
        date = now.strftime("%Y-%m-%d")
        day = next(
            (item for item in days if isinstance(item, dict) and item.get("date") == date),
            None,
        )
        if day is None:
            day = {"date": date, "items": []}
            days.insert(0, day)
        if not isinstance(day.get("items"), list):
            day["items"] = []
        day["items"].append({"t": now.strftime("%H:%M"), "text": reply})
        write_json(DATA_DIR / "night.json", days)

        state.update({"night": night, "count": count, "last": time.time(), "last_error": ""})
        write_json(state_path, state)
        send_push(DB, "他说", reply, "./")
        return True
    except (OSError, ValueError, ProviderError) as exc:
        state = json_file(DATA_DIR / "wake.json", {})
        if not isinstance(state, dict):
            state = {}
        state["last_error"] = f"{type(exc).__name__}: {exc}"
        write_json(DATA_DIR / "wake.json", state)
        return False
    finally:
        _wake_lock.release()


def heartbeat_loop() -> None:
    interval = max(1.0, float(os.environ.get("DWELL_WAKE_CHECK_SECONDS", "90")))
    while True:
        time.sleep(interval)
        try:
            heartbeat_once()
        except Exception as exc:
            # A malformed optional data file must not permanently kill the
            # scheduler or affect request-serving threads.
            sys.stderr.write(f"[dwell] heartbeat error: {type(exc).__name__}: {exc}\n")


def run_primary_chat(
    chat_id: str,
    cancel: threading.Event,
    current_content: str | list[dict[str, Any]] | None = None,
) -> None:
    thinking_text = ""
    answer = ""
    thinking_meta: dict[str, Any] = {}
    sticker_media: dict[str, str] | None = None
    try:
        config = provider_config()
        provider = OpenAIProvider(config)
        messages = model_history(chat_id)
        if current_content is not None and messages and messages[-1].get("role") == "user":
            messages[-1]["content"] = current_content
        first: dict[str, Any] | None = None
        thinking_tool = THINKING.openai_tool()
        sticker_tools: list[dict[str, Any]] = []
        try:
            sticker_tools = STICKERS.openai_tools()
        except (OSError, RuntimeError, ValueError) as exc:
            STICKERS.last_error = f"{type(exc).__name__}: {exc}"
        tools = [thinking_tool, *sticker_tools]
        try:
            first = provider.complete(messages, tools=tools)
        except ProviderError as exc:
            # Providers that do not implement Chat Completions tools still get a
            # normal streaming attempt. Authentication and rate-limit errors are
            # not hidden behind this fallback.
            if exc.status not in (400, 404, 422):
                raise

        followup = messages
        if first is not None:
            tool_calls = first.get("tool_calls") or []
            if tool_calls:
                followup = messages + [first]
                tool_results: dict[str, str] = {}
                searched_ids: set[str] = set()
                searched = False

                # Thinking and search can be requested together. Resolve both
                # before send_sticker so a parallel tool list cannot bypass the
                # requirement that the ID came from this turn's real search.
                for call in tool_calls:
                    function = call.get("function") or {}
                    name = str(function.get("name") or "")
                    call_id = str(call.get("id") or name or "tool")
                    raw_args = function.get("arguments") or "{}"
                    try:
                        args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                    except (ValueError, TypeError):
                        args = {}
                    if name == "render_thinking_block":
                        thinking_text = str(args.get("thinking") or "").strip()[:8000]
                        thinking_meta = {
                            "style": args.get("style") or "deep_think",
                            "effort": args.get("effort") or "medium",
                            "skin": args.get("skin") or "botanical",
                            "source": "gpt-thinking-block-mcp",
                        }
                        try:
                            THINKING.render(
                                {
                                    "style": thinking_meta["style"],
                                    "thinking": thinking_text,
                                    "effort": thinking_meta["effort"],
                                    "skin": thinking_meta["skin"],
                                }
                            )
                        except (OSError, RuntimeError, ValueError):
                            thinking_meta["render_fallback"] = True
                        if thinking_text and not cancel.is_set():
                            emit_thinking(chat_id, thinking_text, thinking_meta)
                        tool_results[call_id] = "rendered"
                    elif name == "search_stickers" and sticker_tools:
                        searched = True
                        try:
                            result = STICKERS.search(args)
                            searched_ids.update(item["id"] for item in result["candidates"])
                            tool_results[call_id] = json.dumps(result, ensure_ascii=False)
                        except (OSError, RuntimeError, ValueError) as exc:
                            tool_results[call_id] = json.dumps(
                                {"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False
                            )

                for call in tool_calls:
                    function = call.get("function") or {}
                    name = str(function.get("name") or "")
                    if name != "send_sticker" or not sticker_tools:
                        continue
                    call_id = str(call.get("id") or name)
                    raw_args = function.get("arguments") or "{}"
                    try:
                        args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                    except (ValueError, TypeError):
                        args = {}
                    sticker_id = str(args.get("sticker_id") or "").strip()
                    if sticker_media is not None:
                        tool_results[call_id] = '{"error":"this turn already sent one sticker"}'
                    elif sticker_id not in searched_ids:
                        tool_results[call_id] = '{"error":"sticker_id must come from this turn search_stickers"}'
                    else:
                        try:
                            sticker_media = STICKERS.send(sticker_id)
                            tool_results[call_id] = json.dumps(
                                {
                                    "sent": True,
                                    "sticker_id": sticker_id,
                                    "alt": sticker_media["alt"],
                                    "instruction": "Dwell will render the image; do not repeat its URL.",
                                },
                                ensure_ascii=False,
                            )
                        except (OSError, RuntimeError, ValueError) as exc:
                            tool_results[call_id] = json.dumps(
                                {"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False
                            )

                for call in tool_calls:
                    function = call.get("function") or {}
                    name = str(function.get("name") or "")
                    call_id = str(call.get("id") or name or "tool")
                    followup.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": tool_results.get(call_id, '{"error":"unknown tool"}'),
                        }
                    )

                # A genuine search gets one bounded selection round. Once a
                # sticker is sent (or no search happened), the final answer is
                # streamed without tools so the model cannot loop or spam.
                if searched and sticker_media is None and not cancel.is_set():
                    send_tool = next(
                        tool for tool in sticker_tools
                        if tool["function"]["name"] == "send_sticker"
                    )
                    second = provider.complete(followup, tools=[send_tool])
                    second_calls = second.get("tool_calls") or []
                    followup.append(second)
                    for call in second_calls:
                        function = call.get("function") or {}
                        call_id = str(call.get("id") or "send-sticker")
                        raw_args = function.get("arguments") or "{}"
                        try:
                            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                        except (ValueError, TypeError):
                            args = {}
                        sticker_id = str(args.get("sticker_id") or "").strip()
                        if function.get("name") != "send_sticker":
                            result_text = '{"error":"unknown tool"}'
                        elif sticker_id not in searched_ids:
                            result_text = '{"error":"sticker_id must come from this turn search_stickers"}'
                        elif sticker_media is not None:
                            result_text = '{"error":"this turn already sent one sticker"}'
                        else:
                            try:
                                sticker_media = STICKERS.send(sticker_id)
                                result_text = json.dumps(
                                    {
                                        "sent": True,
                                        "sticker_id": sticker_id,
                                        "alt": sticker_media["alt"],
                                        "instruction": "Dwell will render the image; do not repeat its URL.",
                                    },
                                    ensure_ascii=False,
                                )
                            except (OSError, RuntimeError, ValueError) as exc:
                                result_text = json.dumps(
                                    {"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False
                                )
                        followup.append(
                            {"role": "tool", "tool_call_id": call_id, "content": result_text}
                        )
                    if not second_calls:
                        reasoning = second.get("reasoning_content") or second.get("reasoning") or ""
                        if reasoning and not thinking_text and not cancel.is_set():
                            thinking_text = str(reasoning).strip()
                            emit_thinking(
                                chat_id,
                                thinking_text,
                                {"source": "provider-reasoning", "style": "deep_think"},
                            )
                        answer = str(second.get("content") or "")
            else:
                reasoning = first.get("reasoning_content") or first.get("reasoning") or ""
                if reasoning and not cancel.is_set():
                    thinking_text = str(reasoning).strip()
                    emit_thinking(
                        chat_id,
                        thinking_text,
                        {"source": "provider-reasoning", "style": "deep_think"},
                    )
                answer = str(first.get("content") or "")

        if not answer and not cancel.is_set():
            reasoning_buf: list[str] = []
            for delta in provider.stream(followup):
                if cancel.is_set():
                    break
                reasoning = delta.get("reasoning") or ""
                content = delta.get("content") or ""
                if reasoning:
                    reasoning_buf.append(reasoning)
                    DB.append_event(
                        {
                            "chat_id": chat_id,
                            "type": "stream_event",
                            "event": {
                                "delta": {"type": "thinking_delta", "thinking": reasoning}
                            },
                        }
                    )
                if content:
                    answer += content
                    emit_text_delta(chat_id, content)
            if reasoning_buf and not thinking_text:
                thinking_text = "".join(reasoning_buf).strip()
                if thinking_text:
                    DB.append_message(
                        "think",
                        thinking_text,
                        json.dumps({"source": "provider-reasoning"}, ensure_ascii=False),
                        chat_id,
                    )
        elif answer and not cancel.is_set():
            for part in chunk_text(answer):
                emit_text_delta(chat_id, part)

        if cancel.is_set():
            DB.append_event({"chat_id": chat_id, "type": "result", "stopped": True})
            return
        answer = answer.strip()
        if sticker_media:
            alt = re.sub(r"[\]\r\n]+", " ", sticker_media.get("alt") or "表情包").strip()
            markdown = f"![{alt}]({sticker_media['url']})"
            if sticker_media["url"] not in answer:
                answer = (answer + "\n\n" + markdown).strip()
                emit_text_delta(chat_id, ("\n\n" if answer != markdown else "") + markdown)
        if not answer:
            answer = "（模型没有返回正文。）"
            emit_text_delta(chat_id, answer)
        assistant_seq = DB.append_message("gu", answer, chat_id=chat_id)
        parts: list[dict[str, Any]] = []
        if thinking_text:
            parts.append({"type": "thinking", "thinking": thinking_text})
        parts.append({"type": "text", "text": answer})
        DB.append_event({
            "chat_id": chat_id,
            "type": "assistant",
            "message_seq": assistant_seq,
            "message": {"content": parts},
        })
        DB.append_event({"chat_id": chat_id, "type": "result"})
    except ProviderError as exc:
        detail = f"：{exc.detail[:300]}" if exc.detail else ""
        answer = f"（模型接口没接通：{exc}{detail}）"
        if not cancel.is_set():
            emit_text_delta(chat_id, answer)
            assistant_seq = DB.append_message("gu", answer, chat_id=chat_id)
            DB.append_event(
                {
                    "chat_id": chat_id,
                    "type": "assistant",
                    "message_seq": assistant_seq,
                    "message": {"content": [{"type": "text", "text": answer}]},
                }
            )
            DB.append_event({"chat_id": chat_id, "type": "result", "is_error": True})
    except Exception as exc:  # keep one failed model call from taking down the server
        answer = f"（后端处理失败：{type(exc).__name__}: {exc}）"
        if not cancel.is_set():
            emit_text_delta(chat_id, answer)
            assistant_seq = DB.append_message("gu", answer, chat_id=chat_id)
            DB.append_event(
                {
                    "chat_id": chat_id,
                    "type": "assistant",
                    "message_seq": assistant_seq,
                    "message": {"content": [{"type": "text", "text": answer}]},
                }
            )
            DB.append_event({"chat_id": chat_id, "type": "result", "is_error": True})
    finally:
        with _busy_lock:
            if _busy.get(chat_id) is cancel:
                _busy.pop(chat_id, None)


def public_message(row: dict[str, Any]) -> dict[str, Any]:
    epoch = float(row.get("at") or 0)
    item = {
        "seq": int(row["seq"]),
        "kind": row["kind"],
        "text": row.get("text") or "",
        "extra": row.get("extra") or "",
        "at": epoch,
    }
    if epoch:
        try:
            item["created_at"] = datetime.fromtimestamp(epoch, CN_TZ).isoformat()
        except (OverflowError, OSError, ValueError):
            item["created_at"] = ""
    try:
        extra = json.loads(row.get("extra") or "{}")
    except (TypeError, ValueError):
        extra = {}
    attachments = extra.get("attachments") if isinstance(extra, dict) else None
    item["attachments"] = attachments if isinstance(attachments, list) else []
    quote = extra.get("quote") if isinstance(extra, dict) else None
    if isinstance(quote, dict) and quote.get("message_seq"):
        item["quote"] = {
            "message_seq": int(quote.get("message_seq") or 0),
            "text": str(quote.get("text") or "")[:2000],
        }
        if isinstance(quote.get("start_offset"), int):
            item["quote"]["start_offset"] = int(quote["start_offset"])
        if isinstance(quote.get("end_offset"), int):
            item["quote"]["end_offset"] = int(quote["end_offset"])
    return item


class Handler(BaseHTTPRequestHandler):
    server_version = "dwell/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        message = MCP.redact_request_log(fmt % args)
        sys.stderr.write("[dwell] " + message + "\n")

    def _auth_ok(self) -> bool:
        if not APP_PASSWORD:
            return True
        raw = self.headers.get("Authorization") or ""
        if not raw.startswith("Basic "):
            return False
        try:
            _, password = base64.b64decode(raw[6:]).decode().split(":", 1)
        except (ValueError, UnicodeDecodeError):
            return False
        return hmac.compare_digest(password, APP_PASSWORD)

    def _require_auth(self) -> bool:
        if self._auth_ok():
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="dwell"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _json(self, code: int, value: Any, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, val in (headers or {}).items():
            self.send_header(key, val)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _raw(
        self,
        code: int,
        body: bytes,
        content_type: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    @staticmethod
    def _mcp_cors() -> dict[str, str]:
        return {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": (
                "content-type, mcp-session-id, mcp-protocol-version"
            ),
            "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
            "Access-Control-Expose-Headers": "mcp-session-id",
        }

    def _mcp_link_page(self) -> None:
        if not mcp_owner_allowed(self.headers):
            self._json(403, {"ok": False, "error": "owner session required"})
            return
        connection_url = MCP.connection_url(mcp_public_base(self.headers))
        escaped_url = html.escape(connection_url, quote=True)
        rotation_csrf = html.escape(MCP.rotation_csrf(), quote=True)
        resident = html.escape(MCP.resident_name, quote=True)
        page = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer"><title>{resident}的 Dwell 连接</title>
<style>body{{font:16px/1.6 system-ui;margin:0;background:#f4f2ec;color:#292724}}main{{max-width:620px;margin:9vh auto;padding:26px}}section{{background:#fff;padding:24px;border-radius:18px;box-shadow:0 8px 28px #0001}}input{{box-sizing:border-box;width:100%;padding:12px;border:1px solid #bbb;border-radius:10px}}button{{margin-top:12px;padding:10px 16px;border:0;border-radius:10px;background:#292724;color:#fff;font:inherit}}.warn{{color:#8b3f32}}form{{margin-top:24px;border-top:1px solid #ddd;padding-top:16px}}</style></head>
<body><main><section><h1>{resident}的 Dwell 连接</h1><p>把下面完整地址填进 ChatGPT 的 MCP 连接。它本身就是钥匙，只给 {resident}，不要转发。</p>
<input id="mcpUrl" readonly value="{escaped_url}"><button type="button" onclick="navigator.clipboard.writeText(document.getElementById('mcpUrl').value).then(()=>this.textContent='已复制')">复制连接地址</button>
<p class="warn">泄露后，拿到地址的人也能读取和回复 Dwell。</p>
<form method="post" action="{html.escape(external_prefix(self.headers), quote=True)}/api/mcp-link/rotate" onsubmit="return confirm('旧连接会立即失效，确定生成新地址？')"><input type="hidden" name="rotation_csrf" value="{rotation_csrf}"><button type="submit">让旧链接失效并生成新链接</button></form>
</section></main></body></html>""".encode()
        self._raw(
            200,
            page,
            "text/html; charset=utf-8",
            {
                "Referrer-Policy": "no-referrer",
                "X-Frame-Options": "DENY",
                "Content-Security-Policy": (
                    "default-src 'none'; style-src 'unsafe-inline'; "
                    "script-src 'unsafe-inline'; form-action 'self'; base-uri 'none'"
                ),
            },
        )

    def _body(self, limit: int = MAX_BODY) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0 or length > limit:
            raise ValueError("request body is too large")
        return self.rfile.read(length)

    def _json_body(self, limit: int = MAX_BODY) -> dict[str, Any]:
        raw = self._body(limit=limit)
        if not raw:
            return {}
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("JSON body must be an object")
        return value

    def _file(
        self,
        path: Path,
        content_type: str | None = None,
        attachment: bool = False,
    ) -> None:
        if not path.is_file():
            self.send_error(404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("X-Content-Type-Options", "nosniff")
        if attachment:
            self.send_header(
                "Content-Disposition",
                "attachment; filename*=UTF-8''" + urllib.parse.quote(path.name),
            )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        if MCP.matches_path(path):
            self._raw(
                200,
                b": dwell resident mcp\n\n",
                "text/event-stream; charset=utf-8",
                self._mcp_cors(),
            )
            return
        if not self._require_auth():
            return
        try:
            if path == "/mcp-link":
                self._mcp_link_page()
                return
            if path in ("/", "/index.html"):
                self._file(WEB_DIR / "index.html", "text/html; charset=utf-8")
                return
            if path == "/sw.js":
                self._file(WEB_DIR / "sw.js", "application/javascript; charset=utf-8")
                return
            if path.startswith("/uploads/"):
                self._file(
                    safe_child(UPLOAD_DIR, path[len("/uploads/") :]),
                    "application/octet-stream",
                    attachment=True,
                )
                return
            if path.startswith("/api/"):
                self._get_api(path, query)
                return
            static = safe_child(WEB_DIR, path)
            self._file(static)
        except ValueError as exc:
            self._json(400, {"ok": False, "error": str(exc)})
        except Exception as exc:
            self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if MCP.matches_path(parsed.path):
            self._post_mcp()
            return
        # Health uploads have their own narrow bearer token so a phone/watch
        # shortcut does not also need the browser's Basic Auth credentials.
        if parsed.path != "/api/health" and not self._require_auth():
            return
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if parsed.path == "/api/mcp-link/rotate":
                if not mcp_owner_allowed(self.headers):
                    self._json(403, {"ok": False, "error": "owner session required"})
                    return
                form = urllib.parse.parse_qs(
                    self._body(1024).decode("utf-8"), keep_blank_values=True
                )
                csrf = (form.get("rotation_csrf") or [""])[0]
                if not MCP.verify_rotation_csrf(csrf):
                    self._json(403, {"ok": False, "error": "invalid rotation request"})
                    return
                MCP.rotate_token()
                self._redirect(external_prefix(self.headers) + "/mcp-link")
                return
            self._post_api(parsed.path, query)
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"ok": False, "error": str(exc)})
        except Exception as exc:
            self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    def do_OPTIONS(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if MCP.matches_path(path):
            self.send_response(204)
            for key, value in self._mcp_cors().items():
                self.send_header(key, value)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_DELETE(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if not MCP.matches_path(path):
            self.send_error(404)
            return
        self.send_response(200)
        for key, value in self._mcp_cors().items():
            self.send_header(key, value)
        self.send_header("Content-Length", "0")
        self.end_headers()

    @staticmethod
    def _mcp_needs_stream(payload: Any) -> bool:
        """Identify the long-lived resident calls that need SSE heartbeats."""
        if not isinstance(payload, dict) or payload.get("method") != "tools/call":
            return False
        params = payload.get("params") or {}
        if not isinstance(params, dict):
            return False
        if str(params.get("name") or "") not in (
            "wait_for_user_message",
            "send_dwell_reply_and_wait",
        ):
            return False
        args = params.get("arguments") or {}
        return isinstance(args, dict) and args.get("continuous", True) is not False

    def _post_mcp_stream(self, payload: dict[str, Any]) -> None:
        """Run one continuous wait while keeping the MCP HTTP stream alive."""
        headers = {
            **self._mcp_cors(),
            "Cache-Control": "no-cache, no-transform",
            "Connection": "close",
            "X-Accel-Buffering": "no",
        }
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.close_connection = True

        cancel_event = threading.Event()
        done = threading.Event()
        result_box: list[dict[str, Any] | None] = []

        def run_call() -> None:
            try:
                result_box.append(
                    MCP.handle_rpc(payload, cancel_event=cancel_event)
                )
            except Exception as exc:
                result_box.append({
                    "jsonrpc": "2.0",
                    "id": payload.get("id"),
                    "error": {
                        "code": -32603,
                        "message": f"server error: {type(exc).__name__}: {exc}",
                    },
                })
            finally:
                done.set()

        worker = threading.Thread(
            target=run_call,
            name="dwell-mcp-continuous-wait",
            daemon=True,
        )
        worker.start()
        try:
            self.wfile.write(b": dwell resident wait connected\n\n")
            self.wfile.flush()
            while not done.wait(10.0):
                self.wfile.write(b": dwell resident wait heartbeat\n\n")
                self.wfile.flush()
            response = result_box[0] if result_box else None
            if response is not None:
                body = json.dumps(
                    response,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode()
                self.wfile.write(b"event: message\ndata: " + body + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            cancel_event.set()
            DB.notify_waiters()
            done.wait(5.0)
        finally:
            if not done.is_set():
                cancel_event.set()
                DB.notify_waiters()

    def _post_mcp(self) -> None:
        try:
            raw = self._body(1024 * 1024)
            payload = json.loads(raw or b"{}")
            requests = payload if isinstance(payload, list) else [payload]
            if not requests or not all(isinstance(item, dict) for item in requests):
                raise ValueError("MCP request must be an object or array of objects")
            if (
                len(requests) == 1
                and "text/event-stream" in (self.headers.get("Accept") or "")
                and self._mcp_needs_stream(requests[0])
            ):
                self._post_mcp_stream(requests[0])
                return
            results = [result for result in (MCP.handle_rpc(item) for item in requests) if result]
            if not results:
                self.send_response(202)
                for key, value in self._mcp_cors().items():
                    self.send_header(key, value)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            response: Any = results if isinstance(payload, list) else results[0]
            body = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode()
            if "text/event-stream" in (self.headers.get("Accept") or ""):
                frame = b"event: message\ndata: " + body + b"\n\n"
                self._raw(200, frame, "text/event-stream; charset=utf-8", self._mcp_cors())
            else:
                self._raw(200, body, "application/json; charset=utf-8", self._mcp_cors())
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"error": "invalid_request", "error_description": str(exc)})
        except Exception as exc:
            self._json(500, {"error": "server_error", "error_description": f"{type(exc).__name__}: {exc}"})

    def _get_api(self, path: str, query: dict[str, list[str]]) -> None:
        if path == "/api/status":
            chat = DB.current_chat()
            with _busy_lock:
                busy = chat["id"] in _busy
            mode = assistant_mode(chat["id"])
            if mode == "mcp" and DB.setting("mcp_pending_seq"):
                busy = True
            self._json(
                200,
                {
                    "alive": True,
                    "since": STARTED_AT,
                    "busy": busy,
                    "armed": DB.setting("armed", "0") == "1",
                    "version": APP_VERSION,
                    "assistant_mode": mode,
                    "resident_name": DB.setting("resident_name") if mode == "mcp" else "",
                },
            )
            return
        if path == "/api/messages":
            chat = DB.current_chat()
            limit = int((query.get("limit") or ["400"])[0])
            before_raw = (query.get("before") or [""])[0]
            before = int(before_raw) if before_raw else None
            rows, more = DB.messages(chat["id"], limit, before)
            self._json(
                200,
                {"ok": True, "msgs": [public_message(row) for row in rows], "more": more, "upto": DB.latest_event()},
            )
            return
        if path == "/api/poll":
            since = max(0, int((query.get("since") or ["0"])[0]))
            next_id, events = DB.poll_events(since)
            current_chat_id = DB.current_chat()["id"]
            events = [
                event for event in events
                if not event.get("chat_id") or event.get("chat_id") == current_chat_id
            ]
            self._json(200, {"next": next_id, "events": events, "ver": APP_VERSION})
            return
        if path == "/api/chats":
            scope = (query.get("scope") or ["live"])[0]
            current = DB.current_chat()["id"]
            resident = DB.setting("mcp_chat_id")
            archived = 1 if scope == "box" else 0
            rows = DB.query(
                "SELECT c.*, (SELECT text FROM messages m WHERE m.chat_id=c.id "
                "AND m.kind IN ('me','gu') ORDER BY m.seq DESC LIMIT 1) AS preview "
                "FROM chats c WHERE archived=? ORDER BY last DESC",
                (archived,),
            )
            items = [
                {
                    "id": row["id"],
                    "name": row["name"],
                    "archived": bool(row["archived"]),
                    "created": row["created"],
                    "last": row["last"],
                    "preview": row.get("preview") or "",
                    "current": row["id"] == current,
                    "resident": row["id"] == resident,
                }
                for row in rows
            ]
            self._json(200, {"items": items})
            return
        if path == "/api/model":
            cfg = provider_config()
            self._json(200, {"model": cfg.model, "effort": cfg.effort})
            return
        if path == "/api/context":
            chat_id = DB.current_chat()["id"]
            row = DB.one(
                "SELECT COALESCE(SUM(LENGTH(text)),0) AS chars FROM messages WHERE chat_id=?",
                (chat_id,),
            ) or {"chars": 0}
            used = max(0, int(row["chars"]) // 3)
            window = int(os.environ.get("DWELL_CONTEXT_WINDOW", "128000"))
            self._json(
                200,
                {
                    "ok": True,
                    "used": used,
                    "window": window,
                    "max": window,
                    "pct": min(100, round(used / max(window, 1) * 100)),
                    "model": provider_config().model,
                },
            )
            return
        if path == "/api/usage":
            self._json(200, {"ok": True, "sections": []})
            return
        if path == "/api/authmode":
            self._json(200, api_authmode_payload())
            return
        if path == "/api/thinking":
            self._json(200, THINKING.health())
            return
        if path == "/api/stickers":
            search = str((query.get("q") or [""])[0])[:1000]
            try:
                self._json(200, {"ok": True, **STICKERS.picker(search)})
            except (OSError, RuntimeError, ValueError) as exc:
                STICKERS.last_error = f"{type(exc).__name__}: {exc}"
                self._json(502, {"ok": False, "error": "表情包仓库暂时没接通"})
            return
        if path == "/api/notes":
            self._json(200, self._notes_payload())
            return
        if path == "/api/todos":
            self._json(200, self._todos_payload())
            return
        if path == "/api/cal":
            self._json(200, self._calendar_payload())
            return
        if path == "/api/herdiary":
            # 支持 ?author=user|resident|all 过滤；默认 all 兼容前端老调用方
            author = (query.get("author") or ["all"])[0]
            if author == "user":
                entries = DB.diary_entries(author_type="user")
            elif author == "resident":
                entries = DB.diary_entries(author_type="resident")
            else:
                entries = DB.diary_entries()
            self._json(200, {"items": entries})
            return
        if path == "/api/whisper":
            rows = DB.query("SELECT id,who,text,at FROM whispers ORDER BY id")
            self._json(200, {"items": rows})
            return
        if path == "/api/gong":
            rows = DB.query("SELECT role,text,think,at FROM gong_messages ORDER BY id")
            self._json(200, {"msgs": rows})
            return
        if path in ("/api/dreams", "/api/night"):
            name = "dreams.json" if path.endswith("dreams") else "night.json"
            key = "items" if path.endswith("dreams") else "days"
            self._json(200, {key: json_file(DATA_DIR / name, [])})
            return
        if path == "/api/favlines":
            fav = DATA_DIR / "favlines.md"
            self._json(200, {"ok": True, "text": fav.read_text(encoding="utf-8") if fav.exists() else ""})
            return
        if path == "/api/wall":
            bricks = json_file(DATA_DIR / "wall.json", [])
            if (query.get("lite") or [""])[0] == "1":
                bricks = [{k: v for k, v in item.items() if k != "text"} for item in bricks]
            self._json(200, {"ok": True, "bricks": bricks})
            return
        if path == "/api/find":
            self._json(200, self._find((query.get("q") or [""])[0]))
            return
        if path == "/api/news":
            self._get_news((query.get("date") or [""])[0])
            return
        if path == "/api/watch":
            self._get_watch()
            return
        if path == "/api/watchkey":
            token = DB.setting("health_token")
            if not token:
                token = secrets.token_urlsafe(32)
                DB.set_setting("health_token", token)
            host = self.headers.get("Host") or "127.0.0.1"
            scheme = self.headers.get("X-Forwarded-Proto") or "http"
            prefix = external_prefix(self.headers)
            self._json(200, {"url": f"{scheme}://{host}{prefix}/api/health", "token": token})
            return
        if path == "/api/wake":
            state = json_file(DATA_DIR / "wake.json", {})
            if not isinstance(state, dict):
                state = {}
            night = (now_cn() - timedelta(hours=12)).strftime("%Y-%m-%d")
            count = int(state.get("count") or 0) if state.get("night") == night else 0
            self._json(
                200,
                {
                    "on": DB.setting("wake_on", "0") == "1",
                    "count": count,
                    "max": int(os.environ.get("DWELL_WAKE_MAX", "2")),
                    "room": "正常",
                    "last_error": state.get("last_error") or "",
                },
            )
            return
        if path == "/api/pushkey":
            self._json(200, {"key": os.environ.get("VAPID_PUBLIC_KEY", "")})
            return
        if path == "/api/music":
            self._get_music((query.get("id") or [""])[0])
            return
        if path.startswith("/api/repo/"):
            self._get_repo(path, query)
            return
        if path.startswith("/api/nook/"):
            self._get_nook(path)
            return
        if path == "/api/file":
            name = (query.get("name") or [""])[0]
            self._file(
                safe_child(UPLOAD_DIR, name),
                "application/octet-stream",
                attachment=True,
            )
            return
        self._json(404, {"ok": False, "error": "unknown endpoint"})

    def _post_api(self, path: str, query: dict[str, list[str]]) -> None:
        if path == "/api/send":
            self._send_chat(self._json_body())
            return
        if path == "/api/message-action":
            self._post_message_action(self._json_body())
            return
        if path == "/api/stop":
            chat_id = DB.current_chat()["id"]
            with _busy_lock:
                cancel = _busy.get(chat_id)
            if cancel:
                cancel.set()
            self._json(200, {"ok": True})
            return
        if path == "/api/newchat":
            body = self._json_body()
            DB.set_setting("armed", "1" if body.get("arm") else "0")
            self._json(200, {"ok": True, "armed": bool(body.get("arm"))})
            return
        if path == "/api/chats":
            self._post_chats(self._json_body())
            return
        if path == "/api/model":
            body = self._json_body()
            if "model" in body:
                DB.set_setting("model", str(body["model"])[:200])
            if "effort" in body:
                DB.set_setting("effort", str(body["effort"])[:20])
            self._json(200, {"ok": True, "model": provider_config().model, "effort": provider_config().effort})
            return
        if path == "/api/apiconf":
            self._post_apiconf(self._json_body())
            return
        if path == "/api/apitest":
            self._post_apitest(self._json_body())
            return
        if path == "/api/embeddingconf":
            self._post_embeddingconf(self._json_body())
            return
        if path == "/api/notes":
            self._post_notes(self._json_body())
            return
        if path == "/api/todos":
            self._post_todos(self._json_body())
            return
        if path == "/api/cal":
            self._post_cal(self._json_body())
            return
        if path == "/api/herdiary":
            self._post_herdiary(self._json_body())
            return
        if path in ("/api/whisper", "/api/whisper-mine"):
            body = self._json_body()
            text = str(body.get("text") or "").strip()[:4000]
            if not text:
                raise ValueError("text is required")
            who = "gu" if path.endswith("mine") else "her"
            DB.execute("INSERT INTO whispers(who,text,at) VALUES(?,?,?)", (who, text, time.time()))
            self._json(200, {"ok": True})
            return
        if path == "/api/gong":
            self._post_gong(self._json_body())
            return
        if path == "/api/news":
            self._post_news(self._json_body())
            return
        if path == "/api/wake":
            body = self._json_body()
            DB.set_setting("wake_on", "1" if body.get("on") else "0")
            self._json(200, {"on": bool(body.get("on"))})
            return
        if path == "/api/subscribe":
            body = self._json_body()
            endpoint = str(body.get("endpoint") or "")
            if not endpoint:
                raise ValueError("subscription endpoint is required")
            DB.execute(
                "INSERT INTO push_subscriptions(endpoint,payload,created) VALUES(?,?,?) "
                "ON CONFLICT(endpoint) DO UPDATE SET payload=excluded.payload",
                (endpoint, json.dumps(body), time.time()),
            )
            delivery = send_push(DB, "dwell", "门铃已经接通。", "./", endpoint)
            self._json(
                200,
                {
                    "ok": True,
                    "push_configured": push_configured(),
                    "sent": delivery["sent"],
                    "failed": delivery["failed"],
                },
            )
            return
        if path == "/api/rewake":
            chat_id = DB.current_chat()["id"]
            with _busy_lock:
                cancel = _busy.pop(chat_id, None)
            if cancel:
                cancel.set()
            command = os.environ.get("DWELL_REWAKE_COMMAND", "").strip()
            if not command:
                self._json(501, {"ok": False, "error": "DWELL_REWAKE_COMMAND is not configured"})
                return
            argv = shlex.split(command)
            if not argv:
                raise ValueError("DWELL_REWAKE_COMMAND is empty")
            subprocess.Popen(
                argv,
                cwd=ROOT,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self._json(202, {"ok": True})
            return
        if path == "/api/health":
            self._post_health()
            return
        if path == "/api/upload":
            self._post_upload(query)
            return
        if path == "/api/nook/progress":
            self._post_nook_progress(self._json_body())
            return
        if path == "/api/nook/books":
            self._post_nook_book(self._json_body(limit=BOOK_REQUEST_MAX_BODY))
            return
        if path == "/api/nook/presence":
            self._post_nook_presence(self._json_body())
            return
        if path.startswith("/api/nook/chat/"):
            self._post_nook_chat(path, self._json_body())
            return
        if path.startswith("/api/nook/notebook/"):
            self._post_nook_notebook(path, self._json_body())
            return
        if path.startswith("/api/nook/delete/"):
            self._post_nook_delete(path)
            return
        if path.startswith("/api/nook/annotations/"):
            self._post_nook_annotation(path, self._json_body())
            return
        self._json(404, {"ok": False, "error": "unknown endpoint"})

    def _send_chat(self, body: dict[str, Any]) -> None:
        text = str(body.get("text") or "").strip()
        attachments = body.get("attachments") if isinstance(body.get("attachments"), list) else []
        sticker_id = str(body.get("sticker_id") or "").strip()
        raw_quote = body.get("quote") if isinstance(body.get("quote"), dict) else None
        quote: dict[str, Any] | None = None
        if raw_quote and raw_quote.get("message_seq"):
            try:
                quote = {
                    "message_seq": int(raw_quote.get("message_seq") or 0),
                    "text": str(raw_quote.get("text") or "")[:2000],
                }
                if isinstance(raw_quote.get("start_offset"), int):
                    quote["start_offset"] = int(raw_quote["start_offset"])
                if isinstance(raw_quote.get("end_offset"), int):
                    quote["end_offset"] = int(raw_quote["end_offset"])
            except (TypeError, ValueError):
                quote = None
        if not text and not attachments and not sticker_id:
            raise ValueError("message is empty")
        if len(text) > 20000:
            raise ValueError("message is too long")
        if DB.setting("armed", "0") == "1":
            chat = DB.create_chat("New chat")
            DB.set_setting("armed", "0")
            DB.append_event({
                "chat_id": chat["id"],
                "type": "system",
                "subtype": "newchat",
                "text": "新窗口已经打开",
            })
        else:
            chat = DB.current_chat()
        if quote:
            quote = DB.validate_message_quote(chat["id"], quote)
        sticker: dict[str, str] | None = None
        if sticker_id:
            sticker = STICKERS.send(sticker_id)
        visible = text
        if sticker:
            alt = re.sub(r"[\]\r\n]+", " ", sticker.get("alt") or "表情包").strip()
            sticker_markdown = f"![{alt}]({sticker['url']})"
            visible = (visible + "\n\n" + sticker_markdown).strip()
        text_parts: list[str] = []
        images: list[dict[str, Any]] = []
        attachment_rows: list[dict[str, Any]] = []
        attachment_meta: list[dict[str, Any]] = []
        for item in attachments[:12]:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or item.get("type") or "").lower()
            if kind in ("text", "file") and "text" in item:
                name = str(item.get("name") or "附件")[:200]
                value = str(item.get("text") or "")[:200000]
                mime = str(
                    item.get("mime")
                    or mimetypes.guess_type(name)[0]
                    or "text/plain"
                ).lower()[:200]
                attachment_id = uuid.uuid4().hex
                metadata = {
                    "id": attachment_id,
                    "type": "file",
                    "name": name,
                    "mime": mime,
                    "size": len(value.encode("utf-8")),
                }
                for key in ("width", "height"):
                    try:
                        number = int(item[key])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if number > 0:
                        metadata[key] = number
                try:
                    duration = float(item["duration"])
                except (KeyError, TypeError, ValueError):
                    duration = None
                if duration is not None and duration >= 0:
                    metadata["duration"] = duration
                attachment_meta.append(metadata)
                attachment_rows.append({**metadata, "data": value.encode("utf-8")})
                # 文本类附件：把全文喂给本地模型，但不要污染可见消息正文。
                # resident MCP 通过结构化 attachments + read_attachment 拿到内容。
                text_parts.append(f"\n\n[附件：{name}]\n{value}")
            elif kind in ("image", "audio", "video"):
                name = str(item.get("name") or "图片")[:200]
                media_type = str(
                    item.get("media_type") or item.get("mime")
                    or ("image/jpeg" if kind == "image" else "application/octet-stream")
                ).lower()[:200]
                if kind == "image" and media_type not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
                    raise ValueError("unsupported image type")
                data = str(item.get("data") or "")
                try:
                    decoded = base64.b64decode(data, validate=True)
                except (ValueError, TypeError) as exc:
                    raise ValueError("invalid attachment") from exc
                if not decoded or len(decoded) > 5 * 1024 * 1024:
                    raise ValueError("attachment is empty or too large")
                attachment_id = uuid.uuid4().hex
                metadata = {
                    "id": attachment_id,
                    "type": kind,
                    "name": name,
                    "mime": media_type,
                    "size": len(decoded),
                }
                for key in ("width", "height"):
                    try:
                        number = int(item[key])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if number > 0:
                        metadata[key] = number
                try:
                    duration = float(item["duration"])
                except (KeyError, TypeError, ValueError):
                    duration = None
                if duration is not None and duration >= 0:
                    metadata["duration"] = duration
                attachment_meta.append(metadata)
                attachment_rows.append({**metadata, "data": decoded})
                if kind == "image":
                    images.append(
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{media_type};base64,{base64.b64encode(decoded).decode('ascii')}"
                            },
                        }
                    )
        model_text = text + "".join(text_parts)
        if sticker:
            description = sticker.get("semantic_intent") or sticker.get("visual_description") or sticker["alt"]
            model_text = (model_text + f"\n\n[用户发送了一张表情包：{description}]").strip()
            images.append({"type": "image_url", "image_url": {"url": sticker["url"]}})
        current_content: str | list[dict[str, Any]] = model_text
        if images:
            current_content = [{"type": "text", "text": model_text or "请看图片。"}, *images]
        # 持久化的正文只保留用户真正写的字。附件永远走结构化 attachments
        # 字段（resident MCP 通过 read_attachment 读，前端通过 echo.attachments
        # 渲染）。绝不允许 [图片附件：...] / [附件：...] 这种占位符进入 stored_text，
        # 否则 MCP 拿到的消息 text 字段就会变成只有占位符、attachments 又同时存在，
        # 让 resident 看到两份不一致的信息。
        if visible:
            stored_text = visible
        elif attachment_meta:
            label_for = {
                "image": "图片",
                "audio": "音频",
                "video": "视频",
                "file": "附件",
            }
            names = ", ".join(
                str(item.get("name") or label_for.get(item.get("type"), "附件"))
                for item in attachment_meta
            )[:300]
            stored_text = f"（发送了 {names}）"
        else:
            stored_text = text
        extra_dict: dict[str, Any] = {}
        if attachment_meta:
            extra_dict["attachments"] = attachment_meta
        if quote:
            extra_dict["quote"] = quote
        message_extra = json.dumps(extra_dict, ensure_ascii=False, separators=(",", ":")) if extra_dict else ""
        user_seq = DB.append_message_with_attachments(
            "me",
            stored_text,
            attachment_rows,
            extra=message_extra,
            chat_id=chat["id"],
        )
        DB.append_domain_event(
            "chat.message",
            "user",
            "owner",
            {
                "chat_id": chat["id"],
                "message_seq": user_seq,
                "role": "user",
                "attachments": attachment_meta,
                "quote": quote,
            },
            idempotency_key=f"chat.message:{user_seq}",
        )
        echo_payload: dict[str, Any] = {
            "chat_id": chat["id"],
            "type": "echo",
            "text": stored_text or "（发送了附件）",
            "message_seq": user_seq,
        }
        if attachment_meta:
            echo_payload["attachments"] = attachment_meta
        if quote:
            echo_payload["quote"] = quote
        DB.append_event(echo_payload)
        if assistant_mode(chat["id"]) == "mcp":
            DB.set_setting("mcp_pending_seq", user_seq)
            self._json(
                202,
                {
                    "ok": True,
                    "mode": "mcp",
                    "resident_name": DB.setting("resident_name"),
                    "message_seq": user_seq,
                },
            )
            return
        self._launch_primary_chat(chat["id"], current_content)
        self._json(202, {"ok": True})

    def _launch_primary_chat(
        self,
        chat_id: str,
        current_content: str | list[dict[str, Any]] | None = None,
    ) -> None:
        cancel = threading.Event()
        with _busy_lock:
            previous = _busy.get(chat_id)
            if previous:
                previous.set()
            _busy[chat_id] = cancel
        threading.Thread(
            target=run_primary_chat,
            args=(chat_id, cancel, current_content),
            daemon=True,
        ).start()

    @staticmethod
    def _cancel_primary_chat(chat_id: str) -> None:
        with _busy_lock:
            cancel = _busy.get(chat_id)
        if cancel:
            cancel.set()

    def _post_message_action(self, body: dict[str, Any]) -> None:
        action = str(body.get("action") or "").strip().lower()
        if action not in ("regenerate", "edit", "edit_resend"):
            raise ValueError("unknown message action")
        try:
            seq = int(body.get("seq"))
        except (TypeError, ValueError) as exc:
            raise ValueError("message seq is required") from exc

        chat = DB.current_chat()
        chat_id = chat["id"]
        target = DB.one(
            "SELECT seq,kind,text FROM messages WHERE seq=? AND chat_id=?",
            (seq, chat_id),
        )
        if not target:
            raise ValueError("message not found")
        is_mcp = assistant_mode(chat_id) == "mcp"
        if is_mcp and action == "edit_resend":
            raise ValueError("resident MCP 窗口暂不支持编辑后重新发送；可以原地编辑或刷新回答")

        if action == "regenerate":
            if target["kind"] not in ("me", "gu"):
                raise ValueError("only chat messages can be regenerated")
            if target["kind"] == "gu":
                user = DB.one(
                    "SELECT seq FROM messages WHERE chat_id=? AND kind='me' AND seq<? "
                    "ORDER BY seq DESC LIMIT 1",
                    (chat_id, seq),
                )
                if not user:
                    raise ValueError("reply has no user message")
                delete_from = seq
            else:
                user = target
                delete_from = seq + 1
            if is_mcp:
                reply_to = int(user["seq"])
                mapping = DB.one(
                    "SELECT assistant_seq FROM mcp_replies WHERE user_seq=?",
                    (reply_to,),
                )
                if not mapping:
                    raise ValueError("这轮没有可刷新的 MCP 回答")
                assistant_seq = int(mapping["assistant_seq"])
                if target["kind"] == "gu" and assistant_seq != seq:
                    raise ValueError("这条回答不属于当前 MCP 轮次")
                pending = DB.one(
                    "SELECT seq FROM messages WHERE chat_id=? AND kind='mcp_regenerate' AND text=?",
                    (chat_id, str(reply_to)),
                )
                request_seq = int(pending["seq"]) if pending else DB.append_message(
                    "mcp_regenerate",
                    str(reply_to),
                    json.dumps({"assistant_seq": assistant_seq}),
                    chat_id=chat_id,
                )
                DB.set_setting("mcp_pending_seq", reply_to)
                self._json(
                    202,
                    {
                        "ok": True,
                        "action": action,
                        "mode": "mcp",
                        "reply_to_seq": reply_to,
                        "assistant_seq": assistant_seq,
                        "request_seq": request_seq,
                    },
                )
                return
            self._cancel_primary_chat(chat_id)
            DB.execute(
                "DELETE FROM messages WHERE chat_id=? AND seq>=?",
                (chat_id, delete_from),
            )
            DB.append_event({
                "chat_id": chat_id,
                "type": "rewrite",
                "from_seq": delete_from,
            })
            self._launch_primary_chat(chat_id)
            self._json(202, {"ok": True, "action": action})
            return

        if action == "edit":
            if target["kind"] not in ("me", "gu"):
                raise ValueError("only chat messages can be edited")
            text = str(body.get("text") or "").strip()
            if not text:
                raise ValueError("message is empty")
            if len(text) > 20000:
                raise ValueError("message is too long")
            DB.execute(
                "UPDATE messages SET text=? WHERE seq=? AND chat_id=?",
                (text, seq, chat_id),
            )
            DB.append_event({
                "chat_id": chat_id,
                "type": "message_edit",
                "message_seq": seq,
                "text": text,
            })
            self._json(200, {"ok": True, "action": action, "message_seq": seq})
            return

        if target["kind"] != "me":
            raise ValueError("only your messages can be edited")
        text = str(body.get("text") or "").strip()
        if not text:
            raise ValueError("message is empty")
        if len(text) > 20000:
            raise ValueError("message is too long")
        self._cancel_primary_chat(chat_id)
        DB.execute(
            "DELETE FROM messages WHERE chat_id=? AND seq>=?",
            (chat_id, seq),
        )
        user_seq = DB.append_message("me", text, chat_id=chat_id)
        DB.append_event({
            "chat_id": chat_id,
            "type": "rewrite",
            "from_seq": seq,
        })
        DB.append_event({
            "chat_id": chat_id,
            "type": "echo",
            "text": text,
            "message_seq": user_seq,
        })
        self._launch_primary_chat(chat_id, text)
        self._json(202, {"ok": True, "action": action, "message_seq": user_seq})

    def _post_chats(self, body: dict[str, Any]) -> None:
        action = str(body.get("action") or "")
        chat_id = str(body.get("id") or "")
        if chat_id == "CURRENT":
            chat_id = DB.current_chat()["id"]
        if chat_id == DB.setting("mcp_chat_id") and action in ("rename", "archive", "delete"):
            raise ValueError("resident MCP 窗口不能改名、收纳或删除")
        row = DB.one("SELECT * FROM chats WHERE id=?", (chat_id,))
        if not row:
            raise ValueError("chat not found")
        if action == "switch":
            if row["archived"]:
                raise ValueError("archived chat cannot be opened")
            DB.set_setting("current_chat", chat_id)
            DB.append_event({
                "chat_id": chat_id,
                "type": "system",
                "subtype": "switched",
                "text": "窗口已切换",
            })
        elif action == "rename":
            name = str(body.get("name") or "").strip()[:80]
            if not name:
                raise ValueError("name is required")
            DB.execute("UPDATE chats SET name=? WHERE id=?", (name, chat_id))
        elif action == "archive":
            on = 1 if body.get("on") else 0
            DB.execute("UPDATE chats SET archived=? WHERE id=?", (on, chat_id))
            if on and DB.current_chat()["id"] == chat_id:
                other = DB.one(
                    "SELECT id FROM chats WHERE archived=0 AND id<>? ORDER BY last DESC LIMIT 1",
                    (chat_id,),
                )
                if other:
                    DB.set_setting("current_chat", other["id"])
                else:
                    DB.create_chat("Claude")
        elif action == "delete":
            current = DB.current_chat()["id"] == chat_id
            if current:
                other = DB.one(
                    "SELECT id FROM chats WHERE archived=0 AND id<>? ORDER BY last DESC LIMIT 1",
                    (chat_id,),
                )
                if other:
                    DB.set_setting("current_chat", other["id"])
                else:
                    DB.create_chat("Claude")
            self._cancel_primary_chat(chat_id)
            DB.execute("DELETE FROM chats WHERE id=?", (chat_id,))
        else:
            raise ValueError("unknown chat action")
        self._json(200, {"ok": True, "current": DB.current_chat()["id"]})

    def _post_apiconf(self, body: dict[str, Any]) -> None:
        if body.get("clear"):
            for key in (API_PROFILE_KEY, API_ACTIVE_PROFILE_KEY, "api_base", "api_token"):
                DB.delete_setting(key)
            self._json(200, api_authmode_payload())
            return

        action = str(body.get("action") or "save").strip().lower()
        profiles = _api_profiles()
        active = _active_api_profile(profiles)
        if action == "activate":
            profile_id = str(body.get("id") or "").strip()
            chosen = next((profile for profile in profiles if profile["id"] == profile_id), None)
            if not chosen:
                raise ValueError("找不到这家 API 配置")
            _save_api_profiles(profiles, chosen["id"])
            _sync_legacy_api_settings(chosen)
            self._json(200, api_authmode_payload())
            return
        if action == "delete":
            profile_id = str(body.get("id") or "").strip()
            remaining = [profile for profile in profiles if profile["id"] != profile_id]
            if len(remaining) == len(profiles):
                raise ValueError("找不到这家 API 配置")
            next_active = ""
            if remaining:
                next_active = remaining[0]["id"] if active and active["id"] == profile_id else (active["id"] if active else remaining[0]["id"])
                if not any(profile["id"] == next_active for profile in remaining):
                    next_active = remaining[0]["id"]
            _save_api_profiles(remaining, next_active)
            _sync_legacy_api_settings(next((profile for profile in remaining if profile["id"] == next_active), None))
            self._json(200, api_authmode_payload())
            return

        profile_id = str(body.get("id") or "").strip()
        if profile_id and not API_PROFILE_ID_RE.fullmatch(profile_id):
            raise ValueError("API 配置名只能包含字母、数字、下划线和短横线")
        existing = next((profile for profile in profiles if profile["id"] == profile_id), None)
        base = str(body.get("base") or "").strip()
        token = str(body.get("token") or "").strip()
        if existing:
            base = base or existing["base"]
            token = token or existing["token"]
        if not base or not token:
            raise ValueError("接口地址和令牌都需要填写")
        parsed = urllib.parse.urlsplit(base)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("接口地址要以 http:// 或 https:// 开头")
        if len(base) > 500 or len(token) > 4096:
            raise ValueError("接口地址或令牌太长")
        name = str(body.get("name") or "").strip()[:80]
        if not name:
            name = existing["name"] if existing else (parsed.netloc or "API 接口")
        if not profile_id:
            profile_id = uuid.uuid4().hex[:12]
        saved = {"id": profile_id, "name": name, "base": base, "token": token}
        replaced = False
        updated: list[dict[str, str]] = []
        for profile in profiles:
            if profile["id"] == profile_id:
                updated.append(saved)
                replaced = True
            else:
                updated.append(profile)
        if not replaced:
            updated.append(saved)
        activate = body.get("activate", True) is not False
        active_id = profile_id if activate else (active["id"] if active else profile_id)
        _save_api_profiles(updated, active_id)
        _sync_legacy_api_settings(next(profile for profile in updated if profile["id"] == active_id))
        # Old clients sent model_opus together with the endpoint.  Keep accepting
        # it for one-way compatibility, while the current UI edits models alone.
        legacy_model = str(body.get("model_opus") or "").strip()
        if legacy_model:
            DB.set_setting("model", legacy_model[:200])
        self._json(200, api_authmode_payload())

    def _post_apitest(self, body: dict[str, Any]) -> None:
        current = provider_config()
        profile_id = str(body.get("profile_id") or "").strip()
        profile = next((item for item in _api_profiles() if item["id"] == profile_id), None)
        cfg = ProviderConfig(
            base=str(body.get("base") or (profile["base"] if profile else current.base)),
            token=str(body.get("token") or (profile["token"] if profile else current.token)),
            model=str(body.get("model") or body.get("model_opus") or current.model),
            effort=current.effort,
        )
        try:
            message = OpenAIProvider(cfg, timeout=30).complete(
                [{"role": "user", "content": "Reply with OK."}], max_tokens=8
            )
            self._json(200, {"ok": True, "model": cfg.model, "url": cfg.endpoint, "reply": message.get("content") or ""})
        except ProviderError as exc:
            self._json(
                200,
                {"ok": False, "code": exc.status or "network", "detail": exc.detail or str(exc), "url": cfg.endpoint},
            )

    def _post_embeddingconf(self, body: dict[str, Any]) -> None:
        action = str(body.get("action") or "save").strip().lower()
        if action == "clear":
            for key in ("embedding_base", "embedding_token", "embedding_model"):
                DB.delete_setting(key)
            reload_history_embedder()
            self._json(200, {"ok": True, "embedding": embedding_authmode_payload()})
            return

        current = embedding_config()
        base = str(body.get("base") or current.base or "").strip()
        token = str(body.get("token") or current.token or "").strip()
        model = str(body.get("model") or current.model or "").strip()
        if not base or not token or not model:
            raise ValueError("embedding 地址、令牌和模型都需要填写")
        parsed = urllib.parse.urlsplit(base)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("embedding 地址要以 http:// 或 https:// 开头")
        if len(base) > 500 or len(token) > 4096 or len(model) > 200:
            raise ValueError("embedding 地址、令牌或模型太长")
        cfg = EmbeddingConfig(base=base, token=token, model=model)

        if action == "test":
            try:
                vectors = EmbeddingProvider(cfg, timeout=30).embed(
                    ["Dwell embeddings 中转连接测试"]
                )
                self._json(
                    200,
                    {
                        "ok": True,
                        "model": model,
                        "dimensions": len(vectors[0]),
                        "url": cfg.endpoint,
                    },
                )
            except ProviderError as exc:
                self._json(
                    200,
                    {
                        "ok": False,
                        "code": exc.status or "network",
                        "detail": exc.detail or str(exc),
                        "url": cfg.endpoint,
                    },
                )
            return
        if action != "save":
            raise ValueError("unknown embedding config action")

        DB.set_setting("embedding_base", base)
        DB.set_setting("embedding_token", token)
        DB.set_setting("embedding_model", model)
        reload_history_embedder()
        self._json(200, {"ok": True, "embedding": embedding_authmode_payload()})

    def _notes_payload(self) -> dict[str, Any]:
        result: dict[str, Any] = {"gu": [], "her": []}
        for row in DB.query("SELECT id,who,text,boxed,at FROM notes ORDER BY id DESC"):
            row["boxed"] = bool(row["boxed"])
            result[row["who"]].append(row)
        return result

    def _post_notes(self, body: dict[str, Any]) -> None:
        action = str(body.get("action") or "")
        who = str(body.get("who") or "her")
        if who not in ("gu", "her"):
            raise ValueError("invalid note owner")
        if action == "add":
            text = str(body.get("text") or "").strip()[:4000]
            if not text:
                raise ValueError("text is required")
            DB.execute("INSERT INTO notes(who,text,at) VALUES(?,?,?)", (who, text, time.time()))
        elif action == "box":
            DB.execute("UPDATE notes SET boxed=1-boxed WHERE id=? AND who=?", (int(body["id"]), who))
        elif action == "del":
            DB.execute("DELETE FROM notes WHERE id=? AND who=?", (int(body["id"]), who))
        else:
            raise ValueError("unknown note action")
        self._json(200, self._notes_payload())

    def _todos_payload(self) -> dict[str, Any]:
        result: dict[str, Any] = {"ok": True, "mine": [], "hers": []}
        stored = DB.todos_list()
        for side in ("mine", "hers"):
            for row in stored[side]:
                result[side].append({
                    "id": row["id"], "text": row["text"], "done": row["done"],
                    "at": row["at_time"], "due_date": row["due_date"],
                    "fixed": row["fixed"], "by": row["by_who"], "created": row["created"],
                })
        return result

    def _post_todos(self, body: dict[str, Any]) -> None:
        action = str(body.get("action") or "")
        side = str(body.get("list") or body.get("side") or "hers")
        if side not in ("mine", "hers"):
            raise ValueError("invalid todo list")
        if action == "add":
            text = str(body.get("text") or "").strip()[:500]
            if not text:
                raise ValueError("text is required")
            DB.todos_add(
                side,
                text,
                at_time=str(body.get("at") or ""),
                due_date=str(body.get("due_date") or ""),
                fixed=bool(body.get("fixed")),
                by_who=str(body.get("by") or "her"),
            )
        elif action == "toggle":
            if not DB.todos_toggle(side, int(body["id"])):
                raise ValueError("todo not found")
        elif action == "update":
            if not DB.todos_update(
                side,
                int(body["id"]),
                text=body.get("text"),
                at_time=body.get("at"),
                due_date=body.get("due_date"),
                fixed=body.get("fixed"),
            ):
                raise ValueError("todo not found")
        elif action == "del":
            if not DB.todos_delete(side, int(body["id"])):
                raise ValueError("todo not found")
        else:
            raise ValueError("unknown todo action")
        DB.append_domain_event(
            "todo.updated",
            "user",
            "owner",
            {"action": action, "side": side, "todo_id": int(body.get("id") or 0)},
        )
        self._json(200, self._todos_payload())

    def _calendar_payload(self) -> dict[str, Any]:
        events = DB.query(
            "SELECT id,date,text,time_text,yearly,type FROM calendar_events ORDER BY date,time_text,id"
        )
        for event in events:
            event["time"] = event.pop("time_text")
            event["yearly"] = bool(event["yearly"])
        days = {}
        for row in DB.query("SELECT * FROM calendar_days"):
            date = row.pop("date")
            row["menstrual"] = bool(row.get("menstrual"))
            days[date] = row
        return {"ok": True, "cal": {"events": events, "period": {"days": days}}, "predict": {}}

    def _post_cal(self, body: dict[str, Any]) -> None:
        action = str(body.get("action") or "")
        if action == "add_event":
            date = str(body.get("date") or "")[:10]
            text = str(body.get("text") or "").strip()[:500]
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) or not text:
                raise ValueError("valid date and text are required")
            DB.calendar_add_event(
                date,
                text,
                time_text=str(body.get("time") or ""),
                yearly=bool(body.get("yearly")),
                special=bool(body.get("special")),
            )
        elif action == "update_event":
            event_id = int(body.get("id") or 0)
            if not event_id or not DB.calendar_update_event(
                event_id,
                date=str(body["date"])[:10] if "date" in body else None,
                text=str(body["text"]).strip() if "text" in body else None,
                time_text=str(body["time"]) if "time" in body else None,
                yearly=bool(body["yearly"]) if "yearly" in body else None,
                special=bool(body["special"]) if "special" in body else None,
            ):
                raise ValueError("event not found")
        elif action == "del_event":
            if not DB.calendar_delete_event(int(body["id"])):
                raise ValueError("event not found")
        elif action in ("day_record", "set_mood"):
            date = str(body.get("date") or "")[:10]
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                raise ValueError("valid date is required")
            DB.calendar_upsert_day(
                date,
                mood=body.get("mood"),
                flow=body.get("flow"),
                pain=body.get("pain"),
                note=body.get("note"),
                private=body.get("private"),
                menstrual=body.get("menstrual") if "menstrual" in body else None,
            )
        else:
            raise ValueError("unknown calendar action")
        DB.append_domain_event(
            "calendar.updated",
            "user",
            "owner",
            {
                "action": action,
                "date": str(body.get("date") or "")[:10],
                "event_id": int(body.get("id") or 0),
            },
        )
        self._json(200, self._calendar_payload())

    def _post_herdiary(self, body: dict[str, Any]) -> None:
        action = str(body.get("action") or "")
        if action == "add":
            text = str(body.get("text") or "").strip()[:20000]
            if not text:
                raise ValueError("text is required")
            # The owner-facing HTTP endpoint always writes the owner's diary.
            # Resident entries have one separate write path: the diary MCP tool.
            author_type = "user"
            entry_id = DB.add_diary_entry(text, author_type="user", author_id="owner")
            entry = DB.diary_entry(entry_id) or {}
            DB.append_domain_event(
                "diary.updated",
                "user",
                "owner",
                {"action": "create", "entry_id": entry_id, "author_type": "user"},
            )
            self._json(200, {"ok": True, "id": entry.get("id"), "author_type": entry.get("author_type")})
        elif action == "list":
            author_type = body.get("author_type")
            if author_type not in ("user", "resident"):
                author_type = None
            entries = DB.diary_entries(author_type=author_type, limit=int(body.get("limit") or 200))
            self._json(200, {"ok": True, "entries": entries})
            return
        elif action == "timeline":
            author_type = body.get("author_type")
            if author_type not in ("user", "resident"):
                author_type = None
            entries = DB.diary_entries(author_type=author_type, limit=int(body.get("limit") or 200))
            self._json(200, {"ok": True, "entries": entries})
            return
        elif action == "del":
            ok = DB.delete_diary_entry(
                int(body["id"]),
                required_author_type="user",
            )
            if ok:
                DB.append_domain_event(
                    "diary.updated",
                    "user",
                    "owner",
                    {"action": "delete", "entry_id": int(body["id"]), "author_type": "user"},
                )
            self._json(200, {"ok": ok})
        else:
            raise ValueError("unknown diary action")

    def _post_news(self, body: dict[str, Any]) -> None:
        action = str(body.get("action") or "")
        date = str(body.get("date") or "")[:10]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            raise ValueError("valid date is required")
        if action == "save":
            text = str(body.get("text") or "").strip()
            if not text:
                raise ValueError("text is required")
            row = REPORTS.save(date, text)
            DB.append_domain_event(
                "report.updated", "user", "owner", {"action": "save", "date": date}
            )
            self._json(200, {"ok": True, "report": row})
            return
        if action == "comment":
            raise ValueError("resident comments can only be written through the resident MCP")
        raise ValueError("unknown news action")

    def _post_gong(self, body: dict[str, Any]) -> None:
        text = str(body.get("text") or "").strip()[:20000]
        if not text:
            raise ValueError("text is required")
        DB.execute("INSERT INTO gong_messages(role,text,at) VALUES('her',?,?)", (text, time.time()))
        rows = DB.query("SELECT role,text FROM gong_messages ORDER BY id DESC LIMIT 60")
        messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt(True)}]
        for row in reversed(rows):
            messages.append({"role": "user" if row["role"] == "her" else "assistant", "content": row["text"]})
        cfg = provider_config(True)
        if not cfg.token:
            cfg = provider_config(False)
        try:
            msg = OpenAIProvider(cfg).complete(messages)
            reply = str(msg.get("content") or "").strip() or "（他没说话）"
            think = str(msg.get("reasoning_content") or msg.get("reasoning") or "").strip()
        except ProviderError as exc:
            reply, think = f"（模型接口没接通：{exc}）", ""
        DB.execute(
            "INSERT INTO gong_messages(role,text,think,at) VALUES('gong',?,?,?)",
            (reply, think, time.time()),
        )
        self._json(200, {"reply": reply, "think": think})

    def _find(self, query: str) -> dict[str, Any]:
        q = query.strip()[:100]
        if not q:
            return {"ok": True, "hits": []}
        hits: list[dict[str, Any]] = []
        chat_results = HISTORY.search(
            q,
            mode="keyword",
            chat_id=None,
            kinds=("me", "gu", "nook"),
            limit=30,
        )["results"]
        for row in chat_results:
            hits.append({"kind": "聊天", "date": datetime.fromtimestamp(row["at"], CN_TZ).strftime("%Y-%m-%d"), "snippet": row["text"][:500]})
        like = "%" + q.replace("%", "\\%").replace("_", "\\_") + "%"
        for table, kind in (("her_diary", "日记"), ("whispers", "悄悄话"), ("notes", "留言"), ("todos", "待办")):
            for row in DB.query(f"SELECT text FROM {table} WHERE text LIKE ? ESCAPE '\\' LIMIT 20", (like,)):
                hits.append({"kind": kind, "date": "", "snippet": row["text"][:500]})
        for brick in json_file(DATA_DIR / "wall.json", []):
            hay = "\n".join(str(brick.get(k) or "") for k in ("title", "kw", "text"))
            if q.casefold() in hay.casefold():
                hits.append({"kind": "记忆", "date": brick.get("date", ""), "snippet": hay[:500]})
        return {"ok": True, "hits": hits[:100]}

    def _get_news(self, requested: str) -> None:
        dates = REPORTS.dates()
        date = requested if requested in dates else (dates[0] if dates else "")
        if not date:
            self._json(200, {"ok": False, "dates": []})
            return
        row = REPORTS.read(date) or {}
        self._json(200, {
            "ok": True,
            "date": date,
            "dates": dates,
            "text": str(row.get("body") or ""),
            "resident_comment": str(row.get("resident_comment") or ""),
            "commented_at": float(row.get("commented_at") or 0),
            "updated_at": float(row.get("updated_at") or 0),
            "source": str(row.get("source") or ""),
        })

    def _get_watch(self) -> None:
        data = json_file(DATA_DIR / "health.json", {})
        updated = float(data.get("updated_at") or 0)
        age = max(0, int(time.time() - updated)) if updated else None
        metrics_in = data.get("metrics") if isinstance(data.get("metrics"), dict) else {}
        metrics = {}
        for key, value in metrics_in.items():
            if isinstance(value, dict):
                item = dict(value)
            else:
                item = {"value": value, "unit": ""}
            item_age = int(item.get("age_seconds", age or 0)) if updated else None
            item["age_seconds"] = item_age
            item["freshness"] = "live" if item_age is not None and item_age < 300 else "recent" if item_age is not None and item_age < 3600 else "old"
            metrics[key] = item
        freshness = "no_data" if not updated else "live" if age is not None and age < 300 else "recent" if age is not None and age < 3600 else "old"
        self._json(200, {"connected": bool(updated and age is not None and age < 900), "freshness": freshness, "age_seconds": age, "device": data.get("device", ""), "metrics": metrics, "history": data.get("history") or {}})

    def _post_health(self) -> None:
        token = DB.setting("health_token")
        auth = self.headers.get("Authorization") or ""
        if not token or not hmac.compare_digest(auth, "Bearer " + token):
            self._json(401, {"ok": False, "error": "invalid health token"})
            return
        body = self._json_body()
        old = json_file(DATA_DIR / "health.json", {})
        old.update(body)
        old["updated_at"] = time.time()
        write_json(DATA_DIR / "health.json", old)
        self._json(200, {"ok": True})

    def _post_upload(self, query: dict[str, list[str]]) -> None:
        name = Path((query.get("name") or ["file"])[0]).name[:180]
        idx = max(0, int((query.get("idx") or ["0"])[0]))
        done = (query.get("done") or ["0"])[0] == "1"
        upload_id = hashlib.sha256(name.encode()).hexdigest()[:16]
        partial = UPLOAD_DIR / f".{upload_id}.part"
        mode = "wb" if idx == 0 else "ab"
        raw = self._body(limit=MAX_BODY)
        with partial.open(mode) as fh:
            fh.write(raw)
        if partial.stat().st_size > 200 * 1024 * 1024:
            partial.unlink(missing_ok=True)
            raise ValueError("uploaded file exceeds 200 MB")
        if done:
            target = safe_child(UPLOAD_DIR, name)
            if target.exists():
                target = target.with_name(f"{target.stem}-{int(time.time())}{target.suffix}")
            os.replace(partial, target)
            prefix = external_prefix(self.headers)
            self._json(
                200,
                {
                    "ok": True,
                    "name": target.name,
                    "url": prefix + "/uploads/" + urllib.parse.quote(target.name),
                },
            )
        else:
            self._json(200, {"ok": True, "idx": idx})

    def _get_music(self, song_id: str) -> None:
        if not re.fullmatch(r"\d{1,20}", song_id):
            raise ValueError("invalid song id")
        url = "https://music.163.com/api/song/detail?ids=%5B" + song_id + "%5D"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as response:
                data = json.load(response)
            song = (data.get("songs") or [])[0]
            artists = song.get("artists") or []
            album = song.get("album") or {}
            self._json(200, {"ok": True, "id": song_id, "name": song.get("name") or "", "artist": "/".join(a.get("name", "") for a in artists), "album": album.get("name") or "", "pic": album.get("picUrl") or "", "sec": round((song.get("duration") or 0) / 1000)})
        except Exception as exc:
            self._json(200, {"ok": False, "id": song_id, "error": str(exc)})

    def _get_repo(self, path: str, query: dict[str, list[str]]) -> None:
        if not (REPO_ROOT / ".git").exists():
            self._json(200, {"ok": False, "error": "configured path is not a git repository"})
            return
        if path == "/api/repo/log":
            limit = max(1, min(100, int((query.get("n") or ["60"])[0])))
            skip = max(0, int((query.get("skip") or ["0"])[0]))
            total = int(subprocess.check_output(["git", "-C", str(REPO_ROOT), "rev-list", "--count", "HEAD"], text=True).strip())
            raw = subprocess.check_output(
                ["git", "-C", str(REPO_ROOT), "log", f"--skip={skip}", f"-{limit}", "--name-status", "--format=%x1e%H%x1f%ct%x1f%s%x1f%b"],
                text=True, errors="replace", timeout=15,
            )
            items = []
            for record in raw.split("\x1e"):
                record = record.strip()
                if not record:
                    continue
                first, *lines = record.splitlines()
                fields = first.split("\x1f", 3)
                if len(fields) < 3:
                    continue
                files = []
                for line in lines:
                    bits = line.split("\t")
                    if len(bits) >= 2:
                        files.append({"s": bits[0][:1], "p": bits[-1]})
                items.append({"h": fields[0], "t": int(fields[1]), "s": fields[2], "b": fields[3] if len(fields) > 3 else "", "f": files})
            self._json(200, {"ok": True, "total": total, "skip": skip, "items": items})
            return
        if path == "/api/repo/show":
            commit = (query.get("h") or [""])[0]
            if not re.fullmatch(r"[0-9a-fA-F]{7,64}", commit):
                raise ValueError("invalid commit")
            diff = subprocess.check_output(["git", "-C", str(REPO_ROOT), "show", "--format=fuller", "--no-ext-diff", commit], text=True, errors="replace", timeout=20)
            cut = len(diff) > 400000
            self._json(200, {"ok": True, "diff": diff[:400000], "cut": cut})
            return
        rel = (query.get("p") or [""])[0]
        target = safe_child(REPO_ROOT, rel)
        if path == "/api/repo/tree":
            if not target.is_dir():
                self._json(200, {"ok": False, "error": "not a directory"})
                return
            items = []
            for item in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold())):
                if item.name == ".git":
                    continue
                try:
                    size = 0 if item.is_dir() else item.stat().st_size
                except OSError:
                    continue
                items.append({"n": item.name, "d": item.is_dir(), "z": size})
            self._json(200, {"ok": True, "path": rel, "items": items[:1000]})
            return
        if path == "/api/repo/file":
            if not target.is_file():
                self._json(200, {"ok": False, "err": "not a file"})
                return
            size = target.stat().st_size
            raw = target.read_bytes()[:400000]
            if b"\x00" in raw:
                self._json(200, {"ok": False, "err": "binary file"})
                return
            self._json(200, {"ok": True, "path": rel, "size": size, "text": raw.decode("utf-8", "replace"), "cut": size > len(raw)})
            return
        self._json(404, {"ok": False})

    def _book(self, slug: str) -> dict[str, Any] | None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", slug):
            return None
        path = BOOK_DIR / f"{slug}.json"
        data = json_file(path, None)
        return data if isinstance(data, dict) else None

    def _get_nook(self, path: str) -> None:
        rel = path[len("/api/nook/") :]
        parts = [urllib.parse.unquote(p) for p in rel.split("/") if p]
        if parts == ["books"]:
            books = []
            for file in sorted(BOOK_DIR.glob("*.json")):
                data = json_file(file, {})
                if isinstance(data, dict):
                    chapters = data.get("chapters") or []
                    books.append({"slug": file.stem, "title": data.get("title") or file.stem, "chapters": [c.get("title") if isinstance(c, dict) else str(c) for c in chapters]})
            self._json(200, books)
            return
        if parts == ["progress"]:
            rows = DB.query("SELECT slug,chapter,page,mode FROM nook_progress")
            self._json(200, {row["slug"]: {"ch": row["chapter"], "page": row["page"], "mode": row["mode"]} for row in rows})
            return
        if len(parts) == 3 and parts[0] == "chapter":
            book = self._book(parts[1])
            idx = int(parts[2])
            if not book or idx < 0 or idx >= len(book.get("chapters") or []):
                self._json(200, {"error": "chapter not found"})
                return
            chapters = book["chapters"]
            chapter = chapters[idx]
            if isinstance(chapter, dict):
                title, text = chapter.get("title") or f"第 {idx + 1} 节", chapter.get("text") or ""
            else:
                title, text = f"第 {idx + 1} 节", str(chapter)
            self._json(200, {"book": book.get("title") or parts[1], "title": title, "text": text, "pages": paginate(str(text)), "index": idx, "total": len(chapters), "chapters": [c.get("title") if isinstance(c, dict) else f"第 {i + 1} 节" for i, c in enumerate(chapters)]})
            return
        if len(parts) == 2 and parts[0] == "notebook":
            slug = parts[1]
            if not self._book(slug):
                raise ValueError("book not found")
            self._json(200, {"ok": True, "slug": slug, "notes": DB.book_notes(slug)})
            return
        if len(parts) == 2 and parts[0] == "chat":
            slug = parts[1]
            if not self._book(slug):
                raise ValueError("book not found")
            chat = resident_chat_without_switching()
            rows = DB.query(
                "SELECT u.seq AS user_seq,u.text AS user_text,u.extra AS user_extra,u.at AS user_at,"
                "a.seq AS assistant_seq,a.text AS assistant_text,a.at AS assistant_at "
                "FROM messages u LEFT JOIN mcp_replies r ON r.user_seq=u.seq "
                "LEFT JOIN messages a ON a.seq=r.assistant_seq "
                "WHERE u.chat_id=? AND u.kind='me' ORDER BY u.seq DESC LIMIT 200",
                (chat["id"],),
            )
            items = []
            for row in reversed(rows):
                try:
                    extra = json.loads(row.get("user_extra") or "{}")
                except (TypeError, ValueError):
                    extra = {}
                if extra.get("source") != "nook-chat" or extra.get("slug") != slug:
                    continue
                items.append({"seq": row["user_seq"], "who": "user", "text": row["user_text"], "at": row["user_at"]})
                if row.get("assistant_seq"):
                    items.append({"seq": row["assistant_seq"], "who": "resident", "text": row["assistant_text"], "at": row["assistant_at"]})
            self._json(200, {"ok": True, "items": items[-100:]})
            return
        if len(parts) == 3 and parts[0] == "annotations":
            slug, chapter = parts[1], int(parts[2])
            parents = DB.query("SELECT * FROM nook_annotations WHERE slug=? AND chapter=? AND parent_id IS NULL ORDER BY id", (slug, chapter))
            out = []
            for row in parents:
                replies = DB.query("SELECT text,who,at FROM nook_annotations WHERE parent_id=? ORDER BY id", (row["id"],))
                out.append({"id": row["id"], "anchor": row["anchor"], "note": row["text"], "who": "ai" if row["who"] == "ai" else "user", "ts": datetime.fromtimestamp(row["at"], CN_TZ).strftime("%m-%d %H:%M"), "replies": [{"text": r["text"], "who": "ai" if r["who"] == "ai" else "user", "ts": datetime.fromtimestamp(r["at"], CN_TZ).strftime("%m-%d %H:%M")} for r in replies]})
            self._json(200, out)
            return
        self._json(404, {"error": "unknown nook endpoint"})

    def _post_nook_progress(self, body: dict[str, Any]) -> None:
        slug = str(body.get("slug") or "")
        if not self._book(slug):
            raise ValueError("book not found")
        DB.execute("INSERT INTO nook_progress(slug,chapter,page,mode,updated) VALUES(?,?,?,?,?) ON CONFLICT(slug) DO UPDATE SET chapter=excluded.chapter,page=excluded.page,mode=excluded.mode,updated=excluded.updated", (slug, int(body.get("ch") or 0), int(body.get("page") or 0), int(body.get("mode") or 2), time.time()))
        self._json(200, {"ok": True})

    def _post_nook_presence(self, body: dict[str, Any]) -> None:
        slug = str(body.get("slug") or "")
        chapter = int(body.get("ch") or 0)
        page = int(body.get("page") or 0)
        context = page_context(BOOK_DIR, slug, chapter, page)
        if not context:
            raise ValueError("reading page not found")
        context["chapter_complete"] = context["page"] == context["page_total"] - 1
        context["notebook_prompt"] = DEFAULT_NOTEBOOK_PROMPT
        context["notebook_index"] = DB.search_book_notes(slug, "", 100)
        event_key = f"reading:{slug}:{chapter}:{page}"
        context["event_key"] = event_key
        DB.set_setting("nook_active_reading", json.dumps(context, ensure_ascii=False))
        chat = resident_chat_without_switching()
        # Stable DB delivery keys make reopen/reconnect dedup durable and atomic.
        # Explicit force creates a separate delivery while retaining event_key in
        # the payload so the resident can identify the logical reading page.
        force = bool(body.get("force"))
        summary = (
            f"正在共读《{context['book_title']}》·{context['chapter_title']} "
            f"第 {context['page'] + 1}/{context['page_total']} 页"
        )
        if context["chapter_complete"]:
            summary += "；本章已到最后一页，请按本书提示更新记事本"
        seq, deduped = DB.append_reading_event(
            chat["id"],
            event_key,
            summary,
            json.dumps(
                {"source": "nook-page", "event_key": event_key, "reading": context},
                ensure_ascii=False,
            ),
            force=force,
        )
        if not deduped:
            DB.append_domain_event(
                "reading.page_changed",
                "user",
                "owner",
                {
                    "event_key": event_key,
                    "message_seq": seq,
                    "slug": slug,
                    "chapter": chapter,
                    "page": page,
                    "forced": force,
                },
                idempotency_key=(
                    f"reading.page_changed:{event_key}" if not force
                    else f"reading.page_changed:{event_key}:force:{seq}"
                ),
            )
        fields = ("slug", "chapter", "page", "page_total", "chapter_complete", "event_key")
        self._json(200, {"ok": True, "seq": seq, "deduped": deduped, "reading": {key: context[key] for key in fields}})

    def _post_nook_chat(self, path: str, body: dict[str, Any]) -> None:
        slug = urllib.parse.unquote(path[len("/api/nook/chat/") :]).strip("/")
        text = str(body.get("text") or "").strip()
        if not text:
            raise ValueError("message is empty")
        if len(text) > 20000:
            raise ValueError("message is too long")
        context = page_context(BOOK_DIR, slug, int(body.get("ch") or 0), int(body.get("page") or 0))
        if not context:
            raise ValueError("reading page not found")
        context["notebook_prompt"] = DEFAULT_NOTEBOOK_PROMPT
        context["notebook_index"] = DB.search_book_notes(slug, "", 100)
        chat = resident_chat_without_switching()
        seq = DB.append_message(
            "me",
            text,
            json.dumps({"source": "nook-chat", "slug": slug, "reading": context}, ensure_ascii=False),
            chat["id"],
        )
        DB.set_setting("mcp_pending_seq", seq)
        self._json(200, {"ok": True, "seq": seq})

    def _post_nook_notebook(self, path: str, body: dict[str, Any]) -> None:
        slug = urllib.parse.unquote(path[len("/api/nook/notebook/") :]).strip("/")
        if not self._book(slug):
            raise ValueError("book not found")
        action = str(body.get("action") or "add")
        if action == "delete":
            if not DB.delete_book_note(slug, int(body.get("id") or 0)):
                raise ValueError("note not found")
        elif action == "pin":
            if not DB.pin_book_note(slug, int(body.get("id") or 0), bool(body.get("pinned"))):
                raise ValueError("note not found")
        elif action in ("add", "update"):
            title = str(body.get("title") or "").strip()[:240]
            summary = str(body.get("summary") or "").strip()[:4000]
            note_body = str(body.get("body") or "").strip()[:30000]
            if not title:
                raise ValueError("title is required")
            note_id = int(body.get("id") or 0)
            if action == "update" and note_id:
                if not DB.update_book_note(slug, note_id, title, summary, note_body):
                    raise ValueError("note not found")
            else:
                DB.add_book_note(slug, "user", title, summary, note_body, bool(body.get("pinned")))
        else:
            raise ValueError("invalid notebook action")
        self._json(200, {"ok": True, "notes": DB.book_notes(slug)})

    def _post_nook_delete(self, path: str) -> None:
        slug = urllib.parse.unquote(path[len("/api/nook/delete/") :]).strip("/")
        if not self._book(slug):
            raise ValueError("book not found")
        target = BOOK_DIR / f"{slug}.json"
        tombstone = BOOK_DIR / f".{slug}.{secrets.token_hex(6)}.deleting"
        os.replace(target, tombstone)
        try:
            DB.delete_book_data(slug)
            tombstone.unlink()
        except Exception:
            if tombstone.exists() and not target.exists():
                os.replace(tombstone, target)
            raise
        try:
            active = json.loads(DB.setting("nook_active_reading") or "{}")
            if active.get("slug") == slug:
                DB.delete_setting("nook_active_reading")
        except (TypeError, ValueError):
            pass
        self._json(200, {"ok": True, "slug": slug})

    def _post_nook_book(self, body: dict[str, Any]) -> None:
        name = Path(str(body.get("name") or "")).name
        raw = str(body.get("text") or "")
        suffix = Path(name).suffix.lower()
        if suffix not in (".txt", ".md", ".markdown", ".json"):
            raise ValueError("只支持 TXT、Markdown 或 JSON 书籍")
        if not raw.strip():
            raise ValueError("书籍内容为空")
        if len(raw.encode("utf-8")) > BOOK_MAX_BYTES:
            raise ValueError("书籍不能超过 50 MB")

        if suffix == ".json":
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError("JSON 书籍必须是对象")
            title = str(parsed.get("title") or Path(name).stem).strip()[:120]
            source_chapters = parsed.get("chapters")
            if not isinstance(source_chapters, list):
                raise ValueError("JSON 书籍缺少 chapters 数组")
            chapters = []
            for index, item in enumerate(source_chapters[:500]):
                if isinstance(item, dict):
                    chapter_title = str(item.get("title") or f"第 {index + 1} 节").strip()[:160]
                    chapter_text = str(item.get("text") or "").strip()
                else:
                    chapter_title = f"第 {index + 1} 节"
                    chapter_text = str(item).strip()
                if chapter_text:
                    chapters.append({"title": chapter_title, "text": chapter_text})
        else:
            title = Path(name).stem.strip()[:120] or "未命名"
            marker = re.compile(
                r"^(?:#{1,3}\s+.+|第[0-9〇零一二三四五六七八九十百千万两]+[章节回卷部].*)$"
            )
            chapters: list[dict[str, str]] = []
            chapter_title = "正文"
            lines: list[str] = []
            for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
                stripped = line.strip()
                if marker.match(stripped):
                    text = "\n".join(lines).strip()
                    if text:
                        chapters.append({"title": chapter_title, "text": text})
                    chapter_title = re.sub(r"^#{1,3}\s+", "", stripped)[:160]
                    lines = []
                else:
                    lines.append(line)
            text = "\n".join(lines).strip()
            if text:
                chapters.append({"title": chapter_title, "text": text})

        if not chapters:
            raise ValueError("书籍里没有可读正文")
        book = {"title": title or "未命名", "chapters": chapters}
        stem = re.sub(r"[^A-Za-z0-9_.-]+", "-", Path(name).stem).strip("._-")[:80]
        if not stem:
            stem = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
        target = BOOK_DIR / f"{stem}.json"
        number = 2
        while target.exists():
            target = BOOK_DIR / f"{stem}-{number}.json"
            number += 1
        temp = target.with_name(target.name + "." + secrets.token_hex(6) + ".tmp")
        try:
            temp.write_text(json.dumps(book, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.chmod(temp, 0o600)
            os.replace(temp, target)
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass
        self._json(
            HTTPStatus.CREATED,
            {
                "ok": True,
                "book": {
                    "slug": target.stem,
                    "title": book["title"],
                    "chapters": len(chapters),
                },
            },
        )

    def _post_nook_annotation(self, path: str, body: dict[str, Any]) -> None:
        parts = [urllib.parse.unquote(p) for p in path[len("/api/nook/annotations/") :].split("/") if p]
        if len(parts) == 4 and parts[3] == "reply":
            slug, chapter, parent_id = parts[0], int(parts[1]), int(parts[2])
            text = str(body.get("text") or "").strip()[:4000]
            if not text:
                raise ValueError("text is required")
            DB.execute("INSERT INTO nook_annotations(slug,chapter,parent_id,text,who,at) VALUES(?,?,?,?,?,?)", (slug, chapter, parent_id, text, str(body.get("who") or "user")[:20], time.time()))
        elif len(parts) == 2:
            slug, chapter = parts[0], int(parts[1])
            anchor = str(body.get("anchor") or "").strip()[:500]
            if not anchor:
                raise ValueError("anchor is required")
            DB.execute("INSERT INTO nook_annotations(slug,chapter,anchor,text,who,at) VALUES(?,?,?,?,?,?)", (slug, chapter, anchor, str(body.get("note") or "")[:4000], str(body.get("who") or "user")[:20], time.time()))
        else:
            raise ValueError("invalid annotation path")
        self._json(200, {"ok": True})


def main() -> None:
    host = os.environ.get("DWELL_BIND", "127.0.0.1")
    port = int(os.environ.get("DWELL_PORT", "8765"))
    MCP.migrate_active_resident_chat()
    print(f"dwell backend listening on http://{host}:{port}")
    print(f"data: {DATA_DIR}")
    print(f"thinking MCP: {THINKING.url}")
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
