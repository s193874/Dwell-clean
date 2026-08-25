from __future__ import annotations

import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class FakeThinking(BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *_args):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        req = json.loads(raw or b"{}")
        if req.get("method") == "tools/list":
            result = {"tools": [{
                "name": "render_thinking_block",
                "description": "Write a visible working summary before the answer.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "style": {"type": "string"},
                        "thinking": {"type": "string"},
                        "effort": {"type": "string"},
                        "skin": {"type": "string"},
                    },
                    "required": ["style", "thinking", "effort", "skin"],
                },
            }]}
        elif req.get("method") == "tools/call":
            params = req.get("params") or {}
            self.__class__.calls.append(params)
            if (params.get("arguments") or {}).get("thinking") == "__force_render_error__":
                result = {"content": [{"type": "text", "text": "render failed"}], "isError": True}
            else:
                result = {"content": [{"type": "text", "text": "rendered"}], "isError": False}
        else:
            result = {}
        body = json.dumps({"jsonrpc": "2.0", "id": req.get("id"), "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FakeSticker(BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *_args):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        req = json.loads(raw or b"{}")
        method = req.get("method")
        if method == "tools/list":
            result = {"tools": [
                {
                    "name": "search_stickers",
                    "description": "Search real sticker candidates.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
                        "required": ["query"],
                    },
                },
                {
                    "name": "send_sticker",
                    "description": "Resolve one real sticker ID.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"sticker_id": {"type": "string"}},
                        "required": ["sticker_id"],
                    },
                },
            ]}
        elif method == "tools/call":
            params = req.get("params") or {}
            self.__class__.calls.append(params)
            if params.get("name") == "search_stickers":
                result = {"content": [{"type": "text", "text": json.dumps({
                    "candidates": [
                        {
                            "id": "st_hug_001",
                            "ocr_text": "",
                            "visual_description": "一只小动物抱着爱心。",
                            "semantic_intent": "给对方一个抱抱。",
                            "tone_tags": ["抱抱"],
                            "use_intents": ["安慰"],
                            "avoid_when": [],
                            "score": 3,
                        },
                        {"id": "st_test_001", "visual_description": "测试图"},
                    ]
                }, ensure_ascii=False)}]}
            elif params.get("name") == "send_sticker":
                sticker_id = str((params.get("arguments") or {}).get("sticker_id") or "")
                host = self.headers.get("Host")
                result = {
                    "content": [{"type": "text", "text": "resolved"}],
                    "structuredContent": {
                        "sticker_id": sticker_id,
                        "url": f"http://{host}/storage/v1/object/public/stickers/hug.gif",
                        "alt": "给你一个抱抱",
                        "caption": "给你一个抱抱",
                        "visual_description": "一只小动物抱着爱心。",
                        "semantic_intent": "给对方一个抱抱。",
                    },
                }
            else:
                result = {"content": [{"type": "text", "text": "unknown"}], "isError": True}
        else:
            result = {}
        body = json.dumps({"jsonrpc": "2.0", "id": req.get("id"), "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FakeProvider(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *_args):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        req = json.loads(raw or b"{}")
        self.__class__.requests.append(req)
        if self.path.endswith("/embeddings"):
            inputs = req.get("input") or []
            data = []
            for index, value in enumerate(inputs):
                text = str(value)
                vector = [
                    1.0 if any(word in text for word in ("炒饭", "米饭", "午饭", "吃饭")) else 0.0,
                    1.0 if any(word in text for word in ("散步", "公园", "走路")) else 0.0,
                    1.0 if any(word in text for word in ("难过", "伤心", "低落")) else 0.0,
                    0.1,
                ]
                data.append({"index": index, "embedding": vector})
            body = json.dumps({"data": data, "model": req.get("model")}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if req.get("stream"):
            frames = [
                {"choices": [{"delta": {"content": "测试"}}]},
                {"choices": [{"delta": {"content": "回答"}}]},
            ]
            body = b"".join(
                b"data: " + json.dumps(item, ensure_ascii=False).encode() + b"\n\n"
                for item in frames
            ) + b"data: [DONE]\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if not req.get("tools"):
            message = {"role": "assistant", "content": "夜里留一张纸条"}
            body = json.dumps({"choices": [{"message": message}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        context = json.dumps(req.get("messages") or [], ensure_ascii=False)
        if "给我一个抱抱表情" in context:
            searched = '"name": "search_stickers"' in context
            if not searched:
                message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call-sticker-search",
                        "type": "function",
                        "function": {
                            "name": "search_stickers",
                            "arguments": json.dumps({"query": "给用户一个温柔的抱抱", "limit": 3}, ensure_ascii=False),
                        },
                    }],
                }
            else:
                message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call-sticker-send",
                        "type": "function",
                        "function": {
                            "name": "send_sticker",
                            "arguments": json.dumps({"sticker_id": "st_hug_001"}),
                        },
                    }],
                }
            body = json.dumps({"choices": [{"message": message}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call-thinking",
                "type": "function",
                "function": {
                    "name": "render_thinking_block",
                    "arguments": json.dumps({
                        "style": "deep_think",
                        "thinking": "先核对约束，再给答案。",
                        "effort": "low",
                        "skin": "microglow",
                    }, ensure_ascii=False),
                },
            }],
        }
        body = json.dumps({"choices": [{"message": message}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FakeIdentity(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.path != "/api/account/me" or self.headers.get("Cookie") != "owner-session=ok":
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = json.dumps({"user": {"id": 3, "username": "owner"}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class BackendIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.data = Path(cls.temp.name)
        (cls.data / "books").mkdir()
        (cls.data / "books" / "sample.json").write_text(
            json.dumps({"title": "测试书", "chapters": [{"title": "第一章", "text": "第一段"}]}),
            encoding="utf-8",
        )
        cls.think_server = ThreadingHTTPServer(("127.0.0.1", 0), FakeThinking)
        cls.sticker_server = ThreadingHTTPServer(("127.0.0.1", 0), FakeSticker)
        cls.provider_server = ThreadingHTTPServer(("127.0.0.1", 0), FakeProvider)
        cls.identity_server = ThreadingHTTPServer(("127.0.0.1", 0), FakeIdentity)
        cls.think_port = int(cls.think_server.server_address[1])
        cls.sticker_port = int(cls.sticker_server.server_address[1])
        cls.provider_port = int(cls.provider_server.server_address[1])
        cls.identity_port = int(cls.identity_server.server_address[1])
        cls.app_port = free_port()
        threading.Thread(target=cls.think_server.serve_forever, daemon=True).start()
        threading.Thread(target=cls.sticker_server.serve_forever, daemon=True).start()
        threading.Thread(target=cls.provider_server.serve_forever, daemon=True).start()
        threading.Thread(target=cls.identity_server.serve_forever, daemon=True).start()
        env = os.environ.copy()
        env.update({
            "DWELL_DATA_DIR": str(cls.data),
            "DWELL_BIND": "127.0.0.1",
            "DWELL_PORT": str(cls.app_port),
            "DWELL_API_BASE": f"http://127.0.0.1:{cls.provider_port}",
            "DWELL_API_TOKEN": "test-token",
            "DWELL_MODEL": "test-model",
            "THINKING_MCP_URL": f"http://127.0.0.1:{cls.think_port}/mcp",
            "DWELL_STICKER_MCP_URL": f"http://127.0.0.1:{cls.sticker_port}/mcp",
            "DWELL_REPO_PATH": str(ROOT),
            "DWELL_WAKE_WINDOW": "all",
            "DWELL_WAKE_CHECK_SECONDS": "1",
            "DWELL_WAKE_QUIET_SECONDS": "0",
            "DWELL_WAKE_GAP_SECONDS": "0",
            "DWELL_WAKE_MAX": "1",
            "DWELL_MCP_PUBLIC_BASE": "https://dwell.example/dwell-mcp",
            "DWELL_MCP_OWNER_CHECK_URL": f"http://127.0.0.1:{cls.identity_port}/api/account/me",
            "DWELL_MCP_OWNER_USER_ID": "3",
            "DWELL_MCP_RESIDENT_NAME": "驻客",
        })
        cls.proc = subprocess.Popen(
            [sys.executable, "-m", "backend.app"],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.time() + 8
        while time.time() < deadline:
            try:
                cls.get("api/status")
                break
            except Exception:
                if cls.proc.poll() is not None:
                    out, err = cls.proc.communicate(timeout=1)
                    raise RuntimeError(f"backend exited\n{out}\n{err}")
                time.sleep(0.1)
        else:
            cls.proc.terminate()
            out, err = cls.proc.communicate(timeout=5)
            cls.think_server.shutdown()
            cls.sticker_server.shutdown()
            cls.provider_server.shutdown()
            cls.identity_server.shutdown()
            cls.temp.cleanup()
            raise RuntimeError(f"backend did not start\nstdout:\n{out}\nstderr:\n{err}")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
        cls.think_server.shutdown()
        cls.sticker_server.shutdown()
        cls.provider_server.shutdown()
        cls.identity_server.shutdown()
        cls.temp.cleanup()

    @classmethod
    def request(cls, path: str, body=None, headers=None):
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{cls.app_port}/{path}",
            data=data,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        with DIRECT_OPENER.open(req, timeout=30) as response:
            return response.status, json.load(response)

    @classmethod
    def get(cls, path: str):
        return cls.request(path)[1]

    def test_status_and_model(self):
        status = self.get("api/status")
        self.assertTrue(status["alive"])
        self.assertEqual(self.get("api/model")["model"], "test-model")

    def test_owner_api_cannot_spoof_resident_report_comment(self):
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request(
                "api/news",
                {
                    "action": "comment",
                    "date": "2030-01-02",
                    "comment": "伪造的 resident 点评",
                },
            )
        self.assertEqual(raised.exception.code, 400)

    def test_http_calendar_rejects_impossible_date(self):
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request(
                "api/cal",
                {"action": "add_event", "date": "2026-02-31", "text": "坏日期"},
            )
        self.assertEqual(raised.exception.code, 400)

    def test_embedding_relay_can_be_saved_tested_and_never_returns_token(self):
        initial = self.get("api/authmode")["embedding"]
        self.assertFalse(initial["configured"])
        base = f"http://127.0.0.1:{self.provider_port}/v1"
        _, probe = self.request(
            "api/embeddingconf",
            {
                "action": "test",
                "base": base,
                "token": "embedding-secret",
                "model": "relay-embedding-model",
            },
        )
        self.assertTrue(probe["ok"])
        self.assertEqual(probe["dimensions"], 4)
        _, saved = self.request(
            "api/embeddingconf",
            {
                "action": "save",
                "base": base,
                "token": "embedding-secret",
                "model": "relay-embedding-model",
            },
        )
        embedding = saved["embedding"]
        self.assertTrue(embedding["configured"])
        self.assertTrue(embedding["semantic_available"])
        self.assertTrue(embedding["has_token"])
        self.assertEqual(embedding["model"], "relay-embedding-model")
        self.assertNotIn("embedding-secret", json.dumps(saved))
        authmode = self.get("api/authmode")
        self.assertNotIn("embedding-secret", json.dumps(authmode))
        _, retry_probe = self.request(
            "api/embeddingconf",
            {
                "action": "test",
                "base": base,
                "token": "",
                "model": "relay-embedding-model",
            },
        )
        self.assertTrue(retry_probe["ok"])

    def test_multiple_api_profiles_are_saved_and_switchable_without_exposing_tokens(self):
        base = f"http://127.0.0.1:{self.provider_port}"
        status, first = self.request(
            "api/apiconf",
            {"action": "save", "id": "first", "name": "第一家", "base": base, "token": "first-secret"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(first["active"], "first")
        self.assertEqual(first["profiles"][0]["name"], "第一家")
        self.assertNotIn("first-secret", json.dumps(first, ensure_ascii=False))

        _, second = self.request(
            "api/apiconf",
            {"action": "save", "id": "second", "name": "第二家中转", "base": base, "token": "second-secret", "activate": False},
        )
        self.assertEqual(len(second["profiles"]), 2)
        self.assertEqual(second["active"], "first")

        _, switched = self.request("api/apiconf", {"action": "activate", "id": "second"})
        self.assertEqual(switched["active"], "second")
        self.assertEqual(self.get("api/model")["model"], "test-model")
        _, cleared = self.request("api/apiconf", {"clear": True})
        self.assertEqual(cleared["profiles"], [])

    def test_frontend_and_read_only_api_contract_are_served(self):
        with DIRECT_OPENER.open(
            f"http://127.0.0.1:{self.app_port}/", timeout=30
        ) as response:
            html = response.read().decode()
            self.assertIn("显式加 ?demo=1", html)
            self.assertIn('id="recMcp">驻客', html)
            self.assertIn('id="recentAdd"', html)
            self.assertIn('<span>最近</span>', html)
            self.assertIn("document.getElementById('recentAdd').onclick = () => armNewChat();", html)
            self.assertNotIn('>Recents<', html)
            self.assertNotIn('>New chat<', html)
            self.assertIn("const p = document.createElement('div');", html)
            self.assertIn("wallBricks = Array.isArray(d.bricks) ? d.bricks : [];", html)
            self.assertIn('还没有便签，先写下一段吧', html)
            self.assertIn('还没有日报', html)
            self.assertIn('保存并接入', html)
            self.assertIn("save.id = 'apSave'", html)
            self.assertIn("base.id = 'embBase'", html)
            self.assertIn("token.id = 'embTok'", html)
            self.assertIn("model.id = 'embModel'", html)
            self.assertIn("api/embeddingconf", html)
            self.assertIn("令牌只保存在本机 SQLite", html)
            self.assertIn('.ap-btns', html)
            self.assertNotIn('备用通道的占位', html)
            self.assertIn('id="modelRow"', html)
            self.assertIn('API_PRESETS', html)
            self.assertIn('action: \'activate\'', html)
            self.assertIn('自定义模型', html)
            self.assertNotIn('id="apModel"', html)
            self.assertIn('api/message-action', html)
            self.assertIn('编辑后重新发送', html)
            self.assertIn('保存修改', html)
            self.assertIn('直接改这条话，保存后不会另发一条', html)
            self.assertIn("add('复制', 'copy'", html)
            self.assertIn("add('刷新', 'refresh'", html)
            self.assertIn("add('编辑', 'pen'", html)
            self.assertIn("b.setAttribute('aria-label', label); b.appendChild(icEl(icon, 15));", html)
            self.assertIn("function bindThinkToMessage(seq)", html)
            self.assertIn("function applyMessageRegenerationBySeq(seq, text, thinking)", html)
            self.assertIn("endThink(m.seq);", html)
            self.assertIn("action: 'delete'", html)
            self.assertIn('async function renameChat(it)', html)
            self.assertIn('id="nookUpload"', html)
            self.assertIn('id="nookBgBtn"', html)
            self.assertIn('id="nookBgDefault"', html)
            self.assertIn('data-bg="#fffdf6"', html)
            self.assertIn('id="nookBgCustom"', html)
            self.assertIn("localStorage.getItem(NK_BG_KEY)", html)
            self.assertIn('id="nkTogether"', html)
            self.assertIn('data-nkmini="chat"', html)
            self.assertIn('data-nkmini="notebook"', html)
            self.assertIn('id="nkMiniClose"', html)
            self.assertIn('50 * 1024 * 1024', html)
            self.assertIn('border-bottom: 2px solid', html)
            self.assertIn('padding: 12px; border-radius: 0;', html)
            # Internal resident notebook prompt must not be baked into the frontend.
            self.assertNotIn('给驻客的提示词（每章最后一页提醒他照此更新）', html)
            self.assertEqual(response.headers.get_content_type(), "text/html")
        with DIRECT_OPENER.open(
            f"http://127.0.0.1:{self.app_port}/sw.js", timeout=30
        ) as response:
            self.assertEqual(response.headers.get_content_type(), "application/javascript")
            self.assertIn("notificationclick", response.read().decode())

        endpoints = [
            "api/messages?limit=10", "api/chats", "api/context", "api/usage",
            "api/authmode", "api/thinking", "api/notes", "api/todos", "api/cal",
            "api/herdiary", "api/whisper", "api/gong", "api/dreams", "api/night",
            "api/favlines", "api/wall", "api/find?q=test", "api/news", "api/watch",
            "api/watchkey", "api/wake", "api/pushkey",
        ]
        for endpoint in endpoints:
            with self.subTest(endpoint=endpoint):
                self.get(endpoint)

        _, watch = self.request(
            "api/watchkey",
            headers={
                "Host": "example.test",
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Prefix": "/dwell",
            },
        )
        self.assertEqual(watch["url"], "https://example.test/dwell/api/health")

    def test_notes_todos_calendar_and_nook(self):
        self.request("api/notes", {"action": "add", "who": "her", "text": "一张纸条"})
        self.assertEqual(self.get("api/notes")["her"][0]["text"], "一张纸条")
        self.request("api/todos", {"action": "add", "list": "hers", "text": "做测试", "by": "her"})
        self.assertEqual(self.get("api/todos")["hers"][0]["text"], "做测试")
        self.request("api/cal", {"action": "add_event", "date": "2026-08-20", "text": "验收"})
        self.assertEqual(self.get("api/cal")["cal"]["events"][0]["text"], "验收")
        books = self.get("api/nook/books")
        self.assertEqual(books[0]["slug"], "sample")
        chapter = self.get("api/nook/chapter/sample/0")
        self.assertEqual(chapter["text"], "第一段")
        self.assertEqual(chapter["pages"], ["第一段"])
        selected_before = next(item["id"] for item in self.get("api/chats")["items"] if item["current"])
        self.request("api/nook/presence", {"slug": "sample", "ch": 0, "page": 0})
        selected_after = next(item["id"] for item in self.get("api/chats")["items"] if item["current"])
        self.assertEqual(selected_after, selected_before)
        status, uploaded = self.request(
            "api/nook/books",
            {
                "name": "第二本.md",
                "text": "# 开始\n第一段\n\n第二段\n# 后来\n第三段",
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(uploaded["book"]["title"], "第二本")
        self.assertEqual(uploaded["book"]["chapters"], 2)
        uploaded_chapter = self.get(
            f"api/nook/chapter/{urllib.parse.quote(uploaded['book']['slug'])}/1"
        )
        self.assertEqual(uploaded_chapter.get("title"), "后来", uploaded_chapter)
        self.assertEqual(uploaded_chapter["text"], "第三段")
        _, notebook = self.request(
            f"api/nook/notebook/{urllib.parse.quote(uploaded['book']['slug'])}",
            {"action": "add", "title": "人物", "summary": "初见", "body": "记住这个人"},
        )
        self.assertEqual(notebook["notes"][0]["title"], "人物")
        note_id = notebook["notes"][0]["id"]
        _, pinned = self.request(
            f"api/nook/notebook/{urllib.parse.quote(uploaded['book']['slug'])}",
            {"action": "pin", "id": note_id, "pinned": True},
        )
        self.assertEqual(pinned["notes"][0]["pinned"], 1)
        self.assertNotIn(
            "prompt",
            self.get(f"api/nook/notebook/{urllib.parse.quote(uploaded['book']['slug'])}"),
        )
        _, deleted = self.request(f"api/nook/delete/{urllib.parse.quote(uploaded['book']['slug'])}", {})
        self.assertTrue(deleted["ok"])
        self.assertFalse(any(b["slug"] == uploaded["book"]["slug"] for b in self.get("api/nook/books")))

    def test_nook_presence_reopen_is_deduped_and_force_resends(self):
        event_key = "reading:sample:0:0"
        with sqlite3.connect(self.data / "dwell.sqlite3") as conn:
            conn.execute(
                "DELETE FROM nook_reading_deliveries WHERE event_key=?",
                (event_key,),
            )
        _, first = self.request(
            "api/nook/presence", {"slug": "sample", "ch": 0, "page": 0}
        )
        _, reopened = self.request(
            "api/nook/presence", {"slug": "sample", "ch": 0, "page": 0}
        )
        _, forced = self.request(
            "api/nook/presence",
            {"slug": "sample", "ch": 0, "page": 0, "force": True},
        )
        self.assertFalse(first["deduped"])
        self.assertTrue(reopened["deduped"])
        self.assertEqual(reopened["seq"], first["seq"])
        self.assertEqual(reopened["reading"]["event_key"], event_key)
        self.assertFalse(forced["deduped"])
        self.assertNotEqual(forced["seq"], first["seq"])
        with sqlite3.connect(self.data / "dwell.sqlite3") as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM nook_reading_deliveries WHERE event_key=?",
                (event_key,),
            ).fetchone()[0]
        self.assertEqual(count, 2)

    def test_nook_upload_limit_is_50mb_and_exceeds_default_json_limit(self):
        large_text = "# 大书\n" + ("body\n" * (7 * 1024 * 1024 // len("body\n")))
        status, uploaded = self.request(
            "api/nook/books",
            {"name": "七兆.md", "text": large_text},
        )
        self.assertEqual(status, 201)
        self.assertEqual(uploaded["book"]["title"], "七兆")
        self.request(
            f"api/nook/delete/{urllib.parse.quote(uploaded['book']['slug'])}",
            {},
        )

        too_large = "# 超限\n" + ("x" * (50 * 1024 * 1024))
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request("api/nook/books", {"name": "超限.md", "text": too_large})
        self.assertEqual(raised.exception.code, 400)
        self.assertIn("50 MB", raised.exception.read().decode())

    def test_nightly_heartbeat_runs_once_and_persists_note(self):
        deadline = time.time() + 8
        while time.time() < deadline and self.get("api/status")["busy"]:
            time.sleep(0.05)
        self.request("api/wake", {"on": True})
        try:
            while time.time() < deadline:
                wake = self.get("api/wake")
                if wake["count"] == 1:
                    break
                time.sleep(0.1)
            self.assertEqual(wake["count"], 1)
            days = self.get("api/night")["days"]
            self.assertEqual(days[0]["items"][-1]["text"], "夜里留一张纸条")
            self.assertTrue(any(n["text"] == "夜里留一张纸条" for n in self.get("api/notes")["gu"]))
        finally:
            self.request("api/wake", {"on": False})

    def test_image_attachment_reaches_vision_provider(self):
        status, body = self.request(
            "api/send",
            {
                "text": "看这张图",
                "attachments": [{
                    "kind": "image",
                    "name": "tiny.png",
                    "media_type": "image/png",
                    "data": "ZmFrZQ==",
                }],
            },
        )
        self.assertEqual(status, 202)
        self.assertTrue(body["ok"])
        stored = self.get("api/messages?limit=10")["msgs"]
        attachment_message = next(item for item in reversed(stored) if item["kind"] == "me")
        self.assertEqual(attachment_message["attachments"][0]["type"], "image")
        self.assertEqual(attachment_message["attachments"][0]["mime"], "image/png")
        self.assertEqual(attachment_message["attachments"][0]["size"], 4)
        deadline = time.time() + 8
        while time.time() < deadline:
            requests_with_tools = [r for r in FakeProvider.requests if r.get("tools")]
            if requests_with_tools and isinstance(requests_with_tools[-1]["messages"][-1]["content"], list):
                break
            time.sleep(0.05)
        content = requests_with_tools[-1]["messages"][-1]["content"]
        self.assertEqual(content[1]["type"], "image_url")
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_chat_uses_thinking_mcp_and_streams_answer(self):
        before = self.get("api/messages?limit=10")["upto"]
        status, body = self.request("api/send", {"text": "你好"})
        self.assertEqual(status, 202)
        self.assertTrue(body["ok"])
        deadline = time.time() + 8
        messages = []
        while time.time() < deadline:
            messages = self.get("api/messages?limit=20")["msgs"]
            if any(m["kind"] == "gu" for m in messages):
                break
            time.sleep(0.1)
        self.assertEqual([m["kind"] for m in messages][-3:], ["me", "think", "gu"])
        self.assertEqual(messages[-1]["text"], "测试回答")
        sent_tool = FakeProvider.requests[-2]["tools"][0]["function"]
        self.assertIn("user-visible", sent_tool["description"])
        self.assertIn("Do not include private scratchpad", sent_tool["parameters"]["properties"]["thinking"]["description"])
        self.assertTrue(FakeThinking.calls)
        self.assertEqual(
            FakeThinking.calls[-1]["arguments"]["thinking"],
            "先核对约束，再给答案。",
        )
        poll = self.get(f"api/poll?since={before}")
        kinds = [event["type"] for event in poll["events"]]
        self.assertIn("echo", kinds)
        self.assertIn("assistant", kinds)
        self.assertIn("result", kinds)

    def test_model_can_search_and_send_one_real_sticker(self):
        deadline = time.time() + 8
        while time.time() < deadline and self.get("api/status")["busy"]:
            time.sleep(0.05)
        before_calls = len(FakeSticker.calls)
        status, body = self.request("api/send", {"text": "给我一个抱抱表情"})
        self.assertEqual(status, 202)
        self.assertTrue(body["ok"])
        messages = []
        while time.time() < deadline:
            messages = self.get("api/messages?limit=20")["msgs"]
            if messages and messages[-1]["kind"] == "gu" and "hug.gif" in messages[-1]["text"]:
                break
            time.sleep(0.1)
        self.assertIn("测试回答", messages[-1]["text"])
        self.assertIn("![给你一个抱抱](http://", messages[-1]["text"])
        calls = FakeSticker.calls[before_calls:]
        self.assertEqual([call["name"] for call in calls], ["search_stickers", "send_sticker"])

    def test_sticker_picker_and_manual_send(self):
        query = urllib.parse.urlencode({"q": "抱抱"})
        data = self.get(f"api/stickers?{query}")
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["stickers"]), 1)
        sticker = data["stickers"][0]
        self.assertEqual(sticker["sticker_id"], "st_hug_001")
        self.assertTrue(sticker["url"].endswith("/storage/v1/object/public/stickers/hug.gif"))

        deadline = time.time() + 8
        while time.time() < deadline and self.get("api/status")["busy"]:
            time.sleep(0.05)
        before_provider_requests = len(FakeProvider.requests)
        status, body = self.request("api/send", {"sticker_id": "st_hug_001"})
        self.assertEqual(status, 202)
        self.assertTrue(body["ok"])
        while time.time() < deadline:
            messages = self.get("api/messages?limit=20")["msgs"]
            if messages and any(m["kind"] == "me" and "hug.gif" in m["text"] for m in messages):
                break
            time.sleep(0.05)
        sent = next(m for m in reversed(messages) if m["kind"] == "me" and "hug.gif" in m["text"])
        self.assertEqual(sent["text"].splitlines()[0], "![给你一个抱抱](http://127.0.0.1:" + str(self.sticker_port) + "/storage/v1/object/public/stickers/hug.gif)")
        vision_requests = []
        while time.time() < deadline:
            vision_requests = [
                request for request in FakeProvider.requests[before_provider_requests:]
                if isinstance((request.get("messages") or [{}])[-1].get("content"), list)
            ]
            if vision_requests:
                break
            time.sleep(0.05)
        self.assertTrue(any(
            any(
                part.get("type") == "image_url" and "hug.gif" in part["image_url"]["url"]
                for part in request["messages"][-1]["content"]
            )
            for request in vision_requests
        ))

    def test_message_actions_and_chat_management(self):
        messages = self.get("api/messages?limit=30")["msgs"]
        answer = next(item for item in reversed(messages) if item["kind"] == "gu")
        before_event = self.get("api/messages?limit=1")["upto"]
        status, body = self.request("api/message-action", {"action": "regenerate", "seq": answer["seq"]})
        self.assertEqual(status, 202)
        self.assertTrue(body["ok"])
        deadline = time.time() + 8
        while time.time() < deadline:
            messages = self.get("api/messages?limit=30")["msgs"]
            if messages and messages[-1]["kind"] == "gu":
                break
            time.sleep(0.05)
        self.assertEqual(messages[-1]["kind"], "gu")
        events = self.get(f"api/poll?since={before_event}")["events"]
        self.assertTrue(any(event["type"] == "rewrite" for event in events))

        reply = messages[-1]
        _, edited_reply = self.request(
            "api/message-action",
            {"action": "edit", "seq": reply["seq"], "text": "手动改过的回答"},
        )
        self.assertEqual(edited_reply["message_seq"], reply["seq"])
        messages = self.get("api/messages?limit=30")["msgs"]
        edited_row = next(item for item in messages if item["seq"] == reply["seq"])
        self.assertEqual(edited_row["text"], "手动改过的回答")

        user = next(item for item in reversed(messages) if item["kind"] == "me")
        _, edited = self.request(
            "api/message-action",
            {"action": "edit_resend", "seq": user["seq"], "text": "改后的问题"},
        )
        self.assertTrue(edited["ok"])
        deadline = time.time() + 8
        while time.time() < deadline:
            messages = self.get("api/messages?limit=30")["msgs"]
            if messages and messages[-1]["kind"] == "gu" and any(
                item["kind"] == "me" and item["text"] == "改后的问题" for item in messages
            ):
                break
            time.sleep(0.05)
        self.assertEqual(next(item for item in reversed(messages) if item["kind"] == "me")["text"], "改后的问题")

        _, renamed = self.request("api/chats", {"action": "rename", "id": "CURRENT", "name": "改过的最近"})
        self.assertTrue(renamed["ok"])
        current_id = self.get("api/chats?scope=live")["items"]
        current_id = next(item["id"] for item in current_id if item["current"])
        _, deleted = self.request("api/chats", {"action": "delete", "id": current_id})
        self.assertTrue(deleted["ok"])
        live = self.get("api/chats?scope=live")["items"]
        self.assertNotIn(current_id, [item["id"] for item in live])
        self.assertTrue(any(item["current"] for item in live))

    def test_repo_browsing_is_read_only(self):
        tree = self.get("api/repo/tree?p=backend")
        self.assertTrue(tree["ok"], tree)
        self.assertTrue(any(item["n"] == "app.py" for item in tree["items"]))

    def test_uploaded_html_is_served_only_as_a_download(self):
        upload = urllib.request.Request(
            f"http://127.0.0.1:{self.app_port}/api/upload?name=page.html&idx=0&done=1",
            data=b"<script>alert(1)</script>",
            method="POST",
        )
        with DIRECT_OPENER.open(upload, timeout=30) as response:
            self.assertEqual(response.status, 200)
        download = urllib.request.Request(
            f"http://127.0.0.1:{self.app_port}/api/file?name=page.html"
        )
        with DIRECT_OPENER.open(download, timeout=30) as response:
            self.assertEqual(response.headers.get_content_type(), "application/octet-stream")
            self.assertTrue(response.headers["Content-Disposition"].startswith("attachment;"))

    def test_z_private_link_mcp_residency_wait_reply_and_rotation(self):
        base_url = f"http://127.0.0.1:{self.app_port}"
        self.request(
            "api/embeddingconf",
            {
                "action": "save",
                "base": f"http://127.0.0.1:{self.provider_port}/v1",
                "token": "embedding-secret",
                "model": "relay-embedding-model",
            },
        )
        before_enter = self.get("api/chats")
        claude_chat = next(item for item in before_enter["items"] if item["current"])

        with self.assertRaises(urllib.error.HTTPError) as denied:
            DIRECT_OPENER.open(base_url + "/mcp-link", timeout=30)
        self.assertEqual(denied.exception.code, 403)

        with DIRECT_OPENER.open(
            urllib.request.Request(
                base_url + "/mcp-link",
                headers={"Cookie": "owner-session=ok"},
            ),
            timeout=30,
        ) as response:
            page = response.read().decode()
        match = re.search(r'id="mcpUrl" readonly value="([^"]+)"', page)
        self.assertIsNotNone(match)
        csrf = re.search(r'name="rotation_csrf" value="([^"]+)"', page)
        self.assertIsNotNone(csrf)
        connection_url = match.group(1)
        self.assertTrue(connection_url.startswith("https://dwell.example/dwell-mcp/"))
        secret_path = urllib.parse.urlsplit(connection_url).path.removeprefix("/dwell-mcp")
        self.assertRegex(secret_path, r"^/[A-Za-z0-9_-]{40,120}/mcp$")
        self.assertEqual((self.data / "mcp-route-token").stat().st_mode & 0o777, 0o600)

        wrong_req = urllib.request.Request(
            base_url + "/wrong-private-link/mcp",
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as wrong:
            DIRECT_OPENER.open(wrong_req, timeout=30)
        self.assertEqual(wrong.exception.code, 404)

        def rpc(method, params=None, rid=1):
            payload = {"jsonrpc": "2.0", "id": rid, "method": method}
            if params is not None:
                payload["params"] = params
            request = urllib.request.Request(
                base_url + secret_path,
                data=json.dumps(payload, ensure_ascii=False).encode(),
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
            with DIRECT_OPENER.open(request, timeout=35) as response:
                return json.load(response)

        initialized = rpc("initialize", {"protocolVersion": "2025-06-18"})
        self.assertEqual(initialized["result"]["serverInfo"]["name"], "dwell-resident-mcp")
        tools = rpc("tools/list")["result"]["tools"]
        self.assertEqual(
            {tool["name"] for tool in tools},
            {
                "enter_dwell", "read_dwell_messages", "read_dwell_events", "wait_for_user_message", "send_dwell_reply",
                "send_dwell_reply_and_wait",
                "read_attachment",
                "read_shared_reading", "search_book_notes", "read_book_note",
                "save_book_note", "delete_book_note",
                "send_dwell_message", "search_stickers", "send_sticker",
                "todos", "calendar", "diary", "daily_report", "get_day_context",
                "search_chat_history", "read_message_context",
            },
        )
        self.assertTrue(all(tool["securitySchemes"] == [{"type": "noauth"}] for tool in tools))
        reply_tool = next(tool for tool in tools if tool["name"] == "send_dwell_reply")
        self.assertEqual(
            set(reply_tool["inputSchema"]["required"]),
            {"reply_to_seq", "text", "style", "thinking", "effort", "skin"},
        )
        self.assertNotIn("working_summary", reply_tool["inputSchema"]["properties"])
        self.assertIn(
            "user-visible",
            reply_tool["inputSchema"]["properties"]["thinking"]["description"],
        )
        wait_tool = next(tool for tool in tools if tool["name"] == "wait_for_user_message")
        self.assertTrue(wait_tool["inputSchema"]["properties"]["continuous"]["default"])
        self.assertEqual(wait_tool["inputSchema"]["properties"]["timeout_seconds"]["maximum"], 3600)
        reply_wait_tool = next(tool for tool in tools if tool["name"] == "send_dwell_reply_and_wait")
        self.assertEqual(
            set(reply_wait_tool["inputSchema"]["required"]),
            {"reply_to_seq", "text", "style", "thinking", "effort", "skin"},
        )
        self.assertIn("continuous", reply_wait_tool["inputSchema"]["properties"])
        self.assertIn("user-visible", reply_wait_tool["inputSchema"]["properties"]["thinking"]["description"])

        entered = rpc("tools/call", {"name": "enter_dwell", "arguments": {}})["result"]
        self.assertFalse(entered["isError"])
        self.assertEqual(entered["structuredContent"]["resident_name"], "驻客")
        self.assertEqual(entered["structuredContent"]["regeneration_requests"], [])
        cursor = entered["structuredContent"]["cursor"]
        after_enter = self.get("api/chats")
        resident_chat = next(item for item in after_enter["items"] if item["resident"])
        self.assertEqual(resident_chat["name"], "驻客")
        self.assertTrue(resident_chat["current"])
        self.assertNotEqual(resident_chat["id"], claude_chat["id"])
        self.assertTrue(any(item["id"] == claude_chat["id"] for item in after_enter["items"]))

        with sqlite3.connect(self.data / "dwell.sqlite3") as conn:
            semantic_seq = conn.execute(
                "INSERT INTO messages(chat_id,kind,text,extra,at) VALUES(?,?,?,?,?)",
                (
                    resident_chat["id"],
                    "me",
                    "三天前我做了一盘炒饭",
                    "",
                    datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc).timestamp(),
                ),
            ).lastrowid
        hybrid = rpc(
            "tools/call",
            {
                "name": "search_chat_history",
                "arguments": {
                    "query": "午饭吃的东西",
                    "mode": "hybrid",
                    "speaker": "user",
                    "date_from": "2026-08-20",
                    "date_to": "2026-08-20",
                    "limit": 5,
                },
            },
        )["result"]
        self.assertFalse(hybrid["isError"])
        self.assertTrue(hybrid["structuredContent"]["semantic"]["available"])
        semantic_hit = next(
            item for item in hybrid["structuredContent"]["results"]
            if item["seq"] == semantic_seq
        )
        self.assertIn("semantic", semantic_hit["sources"])
        self.assertGreater(semantic_hit["score"], 0)
        cursor = max(cursor, int(semantic_seq))

        _, owner_diary = self.request(
            "api/herdiary",
            {"action": "add", "text": "用户日记", "author_type": "resident"},
        )
        owner_entry_id = int(owner_diary["id"])
        self.assertEqual(owner_diary["author_type"], "user")
        resident_diary = rpc(
            "tools/call",
            {"name": "diary", "arguments": {"action": "create", "text": "驻客日记"}},
        )["result"]
        self.assertFalse(resident_diary["isError"])
        self.assertEqual(
            resident_diary["structuredContent"]["entry"]["author_type"],
            "resident",
        )
        diary_events = rpc(
            "tools/call",
            {
                "name": "read_dwell_events",
                "arguments": {"after_event_id": 0, "types": ["diary.updated"], "limit": 50},
            },
        )["result"]["structuredContent"]["events"]
        self.assertTrue(any(event["actor_type"] == "user" for event in diary_events))
        self.assertTrue(any(event["actor_type"] == "resident" for event in diary_events))
        forbidden_diary_delete = rpc(
            "tools/call",
            {"name": "diary", "arguments": {"action": "delete", "entry_id": owner_entry_id}},
        )["result"]
        self.assertTrue(forbidden_diary_delete["isError"])
        owner_entries = self.get("api/herdiary?author=user")["items"]
        self.assertTrue(any(int(item["id"]) == owner_entry_id for item in owner_entries))

        news_dir = self.data / "news"
        news_dir.mkdir(exist_ok=True)
        (news_dir / "2026-08-22.md").write_text(
            "# 日报\n\n同一份生成稿。\n",
            encoding="utf-8",
        )
        report_read = rpc(
            "tools/call",
            {"name": "daily_report", "arguments": {"action": "read", "date": "2026-08-22"}},
        )["result"]
        self.assertFalse(report_read["isError"])
        self.assertEqual(
            report_read["structuredContent"]["report"]["body"],
            "# 日报\n\n同一份生成稿。\n",
        )
        browser_report = self.get("api/news?date=2026-08-22")
        self.assertEqual(browser_report["text"], "# 日报\n\n同一份生成稿。\n")

        _, presence = self.request(
            "api/nook/presence",
            {"slug": "sample", "ch": 0, "page": 0, "force": True},
        )
        self.assertTrue(presence["ok"])
        shared = rpc(
            "tools/call",
            {"name": "wait_for_user_message", "arguments": {"after_seq": cursor, "timeout_seconds": 2, "continuous": False}},
        )["result"]
        self.assertFalse(shared["isError"])
        self.assertEqual(shared["structuredContent"]["user_messages"], [])
        self.assertEqual(shared["structuredContent"]["reading_pages"][-1]["reading"]["page_text"], "第一段")
        self.assertTrue(shared["structuredContent"]["reading_pages"][-1]["reading"]["chapter_complete"])
        self.assertNotIn("notebook", shared["structuredContent"]["reading_pages"][-1]["reading"])
        cursor = shared["structuredContent"]["cursor"]
        reading = rpc(
            "tools/call", {"name": "read_shared_reading", "arguments": {"slug": "sample"}},
        )["result"]["structuredContent"]
        self.assertIn("每章最后一页", reading["notebook_prompt"])
        self.assertEqual(reading["note_index"], [])
        saved = rpc(
            "tools/call",
            {"name": "save_book_note", "arguments": {"slug": "sample", "title": "第一章", "summary": "开场", "body": "第一段", "pinned": True, "client_message_id": "sample-chapter-1-note"}},
        )["result"]["structuredContent"]
        self.assertNotIn("body", saved["note_index"][0])
        searched = rpc(
            "tools/call",
            {"name": "search_book_notes", "arguments": {"slug": "sample", "query": "第一段"}},
        )["result"]["structuredContent"]
        self.assertEqual(searched["results"][0]["title"], "第一章")
        self.assertNotIn("body", searched["results"][0])
        detail = rpc(
            "tools/call",
            {"name": "read_book_note", "arguments": {"slug": "sample", "note_id": saved["note_id"]}},
        )["result"]["structuredContent"]["note"]
        self.assertEqual(
            {key: detail[key] for key in ("title", "summary", "body")},
            {"title": "第一章", "summary": "开场", "body": "第一段"},
        )
        removed = rpc(
            "tools/call",
            {"name": "delete_book_note", "arguments": {"slug": "sample", "note_id": saved["note_id"]}},
        )["result"]["structuredContent"]
        self.assertEqual(removed["note_index"], [])

        _, mini_sent = self.request(
            "api/nook/chat/sample", {"text": "这一页你怎么看？", "ch": 0, "page": 0}
        )
        mini_wait = rpc(
            "tools/call",
            {"name": "wait_for_user_message", "arguments": {"after_seq": cursor, "timeout_seconds": 2, "continuous": False}},
        )["result"]["structuredContent"]
        self.assertEqual(mini_wait["user_messages"][-1]["seq"], mini_sent["seq"])
        self.assertEqual(mini_wait["user_messages"][-1]["reading"]["page_text"], "第一段")
        missing_thinking = rpc(
            "tools/call",
            {"name": "send_dwell_reply", "arguments": {"reply_to_seq": mini_sent["seq"], "text": "我也在看这一页。"}},
        )["result"]
        self.assertTrue(missing_thinking["isError"])
        self.assertIn("thinking is required", missing_thinking["content"][0]["text"])
        mini_reply = rpc(
            "tools/call",
            {"name": "send_dwell_reply", "arguments": {
                "reply_to_seq": mini_sent["seq"],
                "text": "我也在看这一页。",
                "style": "relational",
                "thinking": "我在结合这一页的文字回应。",
                "effort": "low",
                "skin": "botanical",
            }},
        )["result"]
        self.assertFalse(mini_reply["isError"])
        mini_chat = self.get("api/nook/chat/sample")["items"]
        self.assertEqual([item["text"] for item in mini_chat[-2:]], ["这一页你怎么看？", "我也在看这一页。"])
        cursor = mini_wait["cursor"]

        _, failed_sent = self.request(
            "api/nook/chat/sample", {"text": "渲染失败时不要只发正文", "ch": 0, "page": 0}
        )
        failed_wait = rpc(
            "tools/call",
            {"name": "wait_for_user_message", "arguments": {"after_seq": cursor, "timeout_seconds": 2, "continuous": False}},
        )["result"]["structuredContent"]
        self.assertEqual(failed_wait["user_messages"][-1]["seq"], failed_sent["seq"])
        failed_reply = rpc(
            "tools/call",
            {"name": "send_dwell_reply", "arguments": {
                "reply_to_seq": failed_sent["seq"],
                "text": "这句不应落库。",
                "style": "deep_think",
                "thinking": "__force_render_error__",
                "effort": "low",
                "skin": "microglow",
            }},
        )["result"]
        self.assertTrue(failed_reply["isError"])
        self.assertFalse(any(
            item["text"] == "这句不应落库。"
            for item in self.get("api/nook/chat/sample")["items"]
        ))
        cursor = failed_wait["cursor"]

        provider_calls = len(FakeProvider.requests)
        status, sent = self.request("api/send", {"text": "你通过专属 MCP 看到了吗？"})
        self.assertEqual(status, 202)
        self.assertEqual(sent["mode"], "mcp")
        time.sleep(0.2)
        self.assertEqual(len(FakeProvider.requests), provider_calls)

        waited = rpc(
            "tools/call",
            {
                "name": "wait_for_user_message",
                "arguments": {"after_seq": cursor, "timeout_seconds": 2, "continuous": False},
            },
        )["result"]
        self.assertFalse(waited["isError"])
        user_messages = waited["structuredContent"]["user_messages"]
        self.assertEqual(user_messages[-1]["text"], "你通过专属 MCP 看到了吗？")
        user_seq = user_messages[-1]["seq"]

        reply_args = {
            "reply_to_seq": user_seq,
            "style": "deep_think",
            "thinking": "已确认来自当前 Dwell。",
            "effort": "low",
            "skin": "microglow",
            "text": "看到了，我是通过专属 MCP 回来的。",
        }
        replied = rpc("tools/call", {"name": "send_dwell_reply", "arguments": reply_args})["result"]
        self.assertFalse(replied["structuredContent"]["duplicate"])
        duplicate = rpc("tools/call", {"name": "send_dwell_reply", "arguments": reply_args})["result"]
        self.assertTrue(duplicate["structuredContent"]["duplicate"])
        messages = self.get("api/messages?limit=20")["msgs"]
        matches = [item for item in messages if item["kind"] == "gu" and item["text"] == reply_args["text"]]
        self.assertEqual(len(matches), 1)
        self.assertEqual([item["kind"] for item in messages][-3:], ["me", "think", "gu"])
        self.assertEqual(FakeThinking.calls[-1]["name"], "render_thinking_block")
        self.assertEqual(
            FakeThinking.calls[-1]["arguments"],
            {key: reply_args[key] for key in ("style", "thinking", "effort", "skin")},
        )

        self.request("api/chats", {"action": "switch", "id": claude_chat["id"]})
        self.assertEqual(self.get("api/status")["assistant_mode"], "api")
        provider_calls = len(FakeProvider.requests)
        self.request("api/send", {"text": "Claude 这边仍然独立吗？"})
        deadline = time.time() + 8
        while time.time() < deadline and len(FakeProvider.requests) == provider_calls:
            time.sleep(0.05)
        self.assertGreater(len(FakeProvider.requests), provider_calls)
        claude_messages = self.get("api/messages?limit=20")["msgs"]
        self.assertTrue(any(item["text"] == "Claude 这边仍然独立吗？" for item in claude_messages))
        self.assertFalse(any(item["text"] == reply_args["text"] for item in claude_messages))

        resident_read = rpc(
            "tools/call",
            {"name": "read_dwell_messages", "arguments": {"after_seq": 0, "limit": 100}},
        )["result"]
        self.assertTrue(
            any(item["text"] == reply_args["text"] for item in resident_read["structuredContent"]["messages"])
        )
        self.assertFalse(any(
            item["text"] == reply_args["thinking"]
            or item["role"] == "working_summary"
            for item in resident_read["structuredContent"]["messages"]
        ))
        entered_again = rpc("tools/call", {"name": "enter_dwell", "arguments": {}})["result"]
        self.assertFalse(any(
            item["text"] == reply_args["thinking"]
            or item["role"] == "working_summary"
            for item in entered_again["structuredContent"]["recent_messages"]
        ))

        attachment_cursor = entered_again["structuredContent"]["cursor"]
        status, attachment_sent = self.request(
            "api/send",
            {
                "text": "请读一下这两个附件",
                "attachments": [
                    {
                        "kind": "image",
                        "name": "tiny.png",
                        "media_type": "image/png",
                        "data": "ZmFrZQ==",
                    },
                    {"kind": "text", "name": "notes.md", "text": "# 一条笔记\n\n正文。"},
                ],
            },
        )
        self.assertEqual(status, 202)
        attachment_wait = rpc(
            "tools/call",
            {
                "name": "wait_for_user_message",
                "arguments": {"after_seq": attachment_cursor, "timeout_seconds": 2, "continuous": False},
            },
        )["result"]
        self.assertFalse(attachment_wait["isError"])
        attachment_message = attachment_wait["structuredContent"]["user_messages"][-1]
        self.assertEqual(attachment_message["seq"], attachment_sent["message_seq"])
        self.assertEqual(
            [(item["type"], item["name"]) for item in attachment_message["attachments"]],
            [("image", "tiny.png"), ("file", "notes.md")],
        )
        history_with_attachment = rpc(
            "tools/call",
            {"name": "read_dwell_messages", "arguments": {"after_seq": attachment_cursor, "limit": 10}},
        )["result"]["structuredContent"]["messages"]
        self.assertEqual(history_with_attachment[0]["attachments"], attachment_message["attachments"])
        image_id = attachment_message["attachments"][0]["id"]
        image_read = rpc(
            "tools/call", {"name": "read_attachment", "arguments": {"attachment_id": image_id}},
        )["result"]
        self.assertFalse(image_read["isError"])
        self.assertEqual(image_read["content"][-1]["type"], "image")
        self.assertEqual(image_read["content"][-1]["mimeType"], "image/png")
        file_id = attachment_message["attachments"][1]["id"]
        file_read = rpc(
            "tools/call", {"name": "read_attachment", "arguments": {"attachment_id": file_id}},
        )["result"]
        self.assertFalse(file_read["isError"])
        self.assertEqual(file_read["structuredContent"]["text"], "# 一条笔记\n\n正文。")

        sticker_search = rpc(
            "tools/call",
            {"name": "search_stickers", "arguments": {"query": "抱抱", "limit": 3}},
        )["result"]
        self.assertFalse(sticker_search["isError"])
        sticker_id = sticker_search["structuredContent"]["candidates"][0]["id"]
        sticker_sent = rpc(
            "tools/call",
            {
                "name": "send_sticker",
                "arguments": {
                    "sticker_id": sticker_id,
                    "reply_to_seq": attachment_message["seq"],
                    "client_message_id": "integration-sticker-1",
                },
            },
        )["result"]
        self.assertFalse(sticker_sent["isError"])
        self.assertEqual(sticker_sent["structuredContent"]["reply_to_seq"], attachment_message["seq"])

        proactive = rpc(
            "tools/call",
            {
                "name": "send_dwell_message",
                "arguments": {
                    "text": "我主动来陪你一下。",
                    "client_message_id": "integration-proactive-1",
                    "style": "relational",
                    "thinking": "主动留一句简短的话。",
                    "effort": "low",
                    "skin": "botanical",
                },
            },
        )["result"]
        self.assertFalse(proactive["isError"])
        self.assertTrue(proactive["structuredContent"]["proactive"])
        proactive_retry = rpc(
            "tools/call",
            {
                "name": "send_dwell_message",
                "arguments": {
                    "text": "重试不应再发。",
                    "client_message_id": "integration-proactive-1",
                    "style": "relational",
                    "thinking": "这是网络重试。",
                    "effort": "low",
                    "skin": "botanical",
                },
            },
        )["result"]
        self.assertTrue(proactive_retry["structuredContent"]["duplicate"])
        self.assertEqual(
            proactive_retry["structuredContent"]["assistant_seq"],
            proactive["structuredContent"]["assistant_seq"],
        )

        self.request("api/chats", {"action": "switch", "id": resident_chat["id"]})
        self.assertEqual(self.get("api/status")["assistant_mode"], "mcp")
        resident_messages = self.get("api/messages?limit=20")["msgs"]
        self.assertFalse(any(item["text"] == "Claude 这边仍然独立吗？" for item in resident_messages))

        regeneration_cursor = entered_again["structuredContent"]["cursor"]
        status, regeneration_user = self.request("api/send", {"text": "这条回答需要刷新"})
        self.assertEqual(status, 202)
        first_regeneration_wait = rpc(
            "tools/call",
            {"name": "wait_for_user_message", "arguments": {"after_seq": regeneration_cursor, "timeout_seconds": 2, "continuous": False}},
        )["result"]["structuredContent"]
        self.assertEqual(first_regeneration_wait["user_messages"][-1]["seq"], regeneration_user["message_seq"])
        first_regeneration_reply = dict(reply_args)
        first_regeneration_reply.update({
            "reply_to_seq": regeneration_user["message_seq"],
            "text": "第一次回答",
            "thinking": "先给出第一版回答。",
        })
        first_reply_event_cursor = self.get("api/messages?limit=1")["upto"]
        first_regeneration_result = rpc(
            "tools/call", {"name": "send_dwell_reply", "arguments": first_regeneration_reply},
        )["result"]["structuredContent"]
        first_reply_events = self.get(f"api/poll?since={first_reply_event_cursor}")["events"]
        first_assistant_event = next(event for event in first_reply_events if event["type"] == "assistant")
        self.assertEqual(first_assistant_event["message_seq"], first_regeneration_result["assistant_seq"])

        status, edited_mcp = self.request(
            "api/message-action",
            {"action": "edit", "seq": first_regeneration_result["assistant_seq"], "text": "原地编辑后的回答"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(edited_mcp["message_seq"], first_regeneration_result["assistant_seq"])
        self.assertTrue(any(
            item["seq"] == first_regeneration_result["assistant_seq"] and item["text"] == "原地编辑后的回答"
            for item in self.get("api/messages?limit=40")["msgs"]
        ))

        status, second_user = self.request("api/send", {"text": "第二轮问题必须保留"})
        self.assertEqual(status, 202)
        second_wait = rpc(
            "tools/call",
            {"name": "wait_for_user_message", "arguments": {"after_seq": first_regeneration_result["cursor"], "timeout_seconds": 2, "continuous": False}},
        )["result"]["structuredContent"]
        self.assertEqual(second_wait["user_messages"][-1]["seq"], second_user["message_seq"])
        second_reply_args = dict(reply_args)
        second_reply_args.update({
            "reply_to_seq": second_user["message_seq"],
            "text": "第二轮回答也必须保留",
            "thinking": "这是第二轮自己的思考。",
        })
        second_result = rpc(
            "tools/call", {"name": "send_dwell_reply", "arguments": second_reply_args},
        )["result"]["structuredContent"]
        before_refresh = self.get("api/messages?limit=60")["msgs"]
        first_thinking = next(
            item for item in before_refresh
            if item["kind"] == "think"
            and regeneration_user["message_seq"] < item["seq"] < first_regeneration_result["assistant_seq"]
        )

        status, refresh = self.request(
            "api/message-action",
            {"action": "regenerate", "seq": first_regeneration_result["assistant_seq"]},
        )
        self.assertEqual(status, 202)
        self.assertEqual(refresh["mode"], "mcp")
        self.assertEqual(refresh["reply_to_seq"], regeneration_user["message_seq"])
        self.assertEqual(refresh["assistant_seq"], first_regeneration_result["assistant_seq"])
        queued_messages = self.get("api/messages?limit=60")["msgs"]
        self.assertTrue(any(item["seq"] == second_user["message_seq"] for item in queued_messages))
        self.assertTrue(any(item["seq"] == second_result["assistant_seq"] for item in queued_messages))
        pending = rpc(
            "tools/call",
            {"name": "wait_for_user_message", "arguments": {"after_seq": second_result["cursor"], "timeout_seconds": 2, "continuous": False}},
        )["result"]["structuredContent"]
        self.assertFalse(pending["timed_out"])
        self.assertEqual(pending["regeneration_requests"][0]["reply_to_seq"], regeneration_user["message_seq"])
        self.assertEqual(pending["regeneration_requests"][0]["assistant_seq"], first_regeneration_result["assistant_seq"])
        self.assertEqual(pending["regeneration_requests"][0]["user_message"]["text"], "这条回答需要刷新")

        replacement_args = dict(first_regeneration_reply)
        replacement_args.update({"text": "刷新后的回答", "thinking": "重新回答这条消息。"})
        before_replacement_event = self.get("api/messages?limit=1")["upto"]
        replacement = rpc(
            "tools/call", {"name": "send_dwell_reply", "arguments": replacement_args},
        )["result"]
        self.assertFalse(replacement["isError"])
        self.assertTrue(replacement["structuredContent"]["regenerated"])
        self.assertEqual(replacement["structuredContent"]["assistant_seq"], first_regeneration_result["assistant_seq"])
        self.assertGreaterEqual(replacement["structuredContent"]["cursor"], refresh["request_seq"])
        replacement_events = self.get(f"api/poll?since={before_replacement_event}")["events"]
        replacement_event = next(event for event in replacement_events if event["type"] == "message_regenerated")
        self.assertEqual(replacement_event["message_seq"], first_regeneration_result["assistant_seq"])
        self.assertEqual(replacement_event["thinking"], "重新回答这条消息。")
        refreshed_messages = self.get("api/messages?limit=60")["msgs"]
        self.assertFalse(any(item["kind"] == "mcp_regenerate" for item in refreshed_messages))
        self.assertTrue(any(
            item["seq"] == first_regeneration_result["assistant_seq"] and item["text"] == "刷新后的回答"
            for item in refreshed_messages
        ))
        self.assertFalse(any(item["text"] == "原地编辑后的回答" for item in refreshed_messages))
        self.assertTrue(any(
            item["seq"] == first_thinking["seq"] and item["text"] == "重新回答这条消息。"
            for item in refreshed_messages
        ))
        self.assertTrue(any(
            item["seq"] == second_user["message_seq"] and item["text"] == "第二轮问题必须保留"
            for item in refreshed_messages
        ))
        self.assertTrue(any(
            item["seq"] == second_result["assistant_seq"] and item["text"] == "第二轮回答也必须保留"
            for item in refreshed_messages
        ))
        visible_order = [
            item["seq"] for item in refreshed_messages
            if item["seq"] in {
                regeneration_user["message_seq"], first_regeneration_result["assistant_seq"],
                second_user["message_seq"], second_result["assistant_seq"],
            }
        ]
        self.assertEqual(visible_order, [
            regeneration_user["message_seq"], first_regeneration_result["assistant_seq"],
            second_user["message_seq"], second_result["assistant_seq"],
        ])

        # A real continuous wait must keep the MCP HTTP stream alive before a
        # message arrives, then return the message through one final SSE frame.
        continuous_cursor = rpc(
            "tools/call", {"name": "enter_dwell", "arguments": {}},
        )["result"]["structuredContent"]["cursor"]
        stream_result = {}
        stream_started = threading.Event()
        heartbeat_seen = threading.Event()

        def run_continuous_wait():
            request = urllib.request.Request(
                base_url + secret_path,
                data=json.dumps({
                    "jsonrpc": "2.0",
                    "id": 700,
                    "method": "tools/call",
                    "params": {
                        "name": "wait_for_user_message",
                        "arguments": {
                            "after_seq": continuous_cursor,
                            "continuous": True,
                        },
                    },
                }).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
            )
            with DIRECT_OPENER.open(request, timeout=30) as response:
                stream_started.set()
                stream_result["connected"] = response.readline().decode()
                response.readline()
                stream_result["heartbeat"] = response.readline().decode()
                response.readline()
                heartbeat_seen.set()
                lines = [line.decode() for line in response]
            data_line = next(line for line in lines if line.startswith("data: "))
            stream_result["response"] = json.loads(data_line[6:])

        waiter = threading.Thread(target=run_continuous_wait)
        waiter.start()
        self.assertTrue(stream_started.wait(5))
        self.assertTrue(heartbeat_seen.wait(20))
        status, continuous_user = self.request("api/send", {"text": "连续驻守测试"})
        self.assertEqual(status, 202)
        waiter.join(15)
        self.assertFalse(waiter.is_alive())
        self.assertIn("heartbeat", stream_result["heartbeat"])
        continuous_structured = stream_result["response"]["result"]["structuredContent"]
        self.assertFalse(continuous_structured["timed_out"])
        self.assertEqual(continuous_structured["disconnect_reason"], "message_received")
        self.assertEqual(
            continuous_structured["user_messages"][-1]["seq"],
            continuous_user["message_seq"],
        )

        combined = rpc(
            "tools/call",
            {
                "name": "send_dwell_reply_and_wait",
                "arguments": {
                    "reply_to_seq": continuous_user["message_seq"],
                    "text": "我还在这里。",
                    "style": "relational",
                    "thinking": "已收到新消息，并在发送后继续等待。",
                    "effort": "low",
                    "skin": "botanical",
                    "continuous": False,
                    "timeout_seconds": 1,
                },
            },
        )["result"]
        self.assertFalse(combined["isError"])
        combined_structured = combined["structuredContent"]
        self.assertEqual(
            combined_structured["sent_reply"]["assistant_seq"],
            combined_structured["cursor"],
        )
        self.assertTrue(combined_structured["timed_out"])
        self.assertEqual(combined_structured["disconnect_reason"], "timeout")

        with sqlite3.connect(self.data / "dwell.sqlite3") as conn:
            wait_rows = conn.execute(
                "SELECT request_id,continuous,timed_out,disconnect_reason "
                "FROM mcp_wait_log ORDER BY started_at"
            ).fetchall()
        self.assertTrue(any(row[1] == 1 and row[2] == 0 and row[3] == "message_received" for row in wait_rows))
        self.assertTrue(any(row[1] == 0 and row[2] == 1 and row[3] == "timeout" for row in wait_rows))

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *_args, **_kwargs):
                return None

        rotate = urllib.request.Request(
            base_url + "/api/mcp-link/rotate",
            data=urllib.parse.urlencode({"rotation_csrf": csrf.group(1)}).encode(),
            headers={
                "Cookie": "owner-session=ok",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        with self.assertRaises(urllib.error.HTTPError) as redirected:
            urllib.request.build_opener(
                urllib.request.ProxyHandler({}), NoRedirect()
            ).open(rotate, timeout=30)
        self.assertEqual(redirected.exception.code, 303)
        with self.assertRaises(urllib.error.HTTPError) as revoked:
            rpc("tools/list")
        self.assertEqual(revoked.exception.code, 404)
        self.assertEqual(self.get("api/status")["assistant_mode"], "api")

        with sqlite3.connect(self.data / "dwell.sqlite3") as conn:
            rows = conn.execute("SELECT COUNT(*) FROM mcp_replies").fetchone()[0]
        self.assertEqual(rows, 5)


if __name__ == "__main__":
    unittest.main()
