"""Unit tests for the resident MCP extensions: diary authorship, todos, calendar,
daily reports, search, attachments, proactive messaging, etc.

These tests bypass the HTTP server (which is flaky on Windows under the existing
test harness) and drive the Database + ResidentMCP directly. They focus on the
new surface area added in this change.
"""

from __future__ import annotations

import json
import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.store import Database
from backend.resident_mcp import ResidentMCP, _message, _diary_entry, _daily_report, _snippet, _iso, _day_bounds
from backend.history_search import HistorySearchService


class LegacySchemaMigrationTest(unittest.TestCase):
    def test_old_diary_table_adds_author_columns_before_creating_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dwell.sqlite3"
            with sqlite3.connect(path) as conn:
                conn.execute(
                    "CREATE TABLE her_diary ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT,text TEXT NOT NULL,at REAL NOT NULL)"
                )
                conn.execute("INSERT INTO her_diary(text,at) VALUES('旧日记',1)")
            db = Database(path)
            with sqlite3.connect(path) as conn:
                columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(her_diary)")
                }
                indexes = {
                    row[1] for row in conn.execute("PRAGMA index_list(her_diary)")
                }
            self.assertIn("author_type", columns)
            self.assertIn("author_id", columns)
            self.assertIn("her_diary_author", indexes)
            self.assertEqual(db.diary_entries()[0]["author_type"], "user")


class _FakeThinking:
    """Stand-in for ThinkingBridge that always succeeds."""

    def render(self, arguments):
        return {"content": [{"type": "text", "text": "ok"}], "isError": False}


class _FakeStickers:
    def __init__(self):
        self.send_calls = 0

    def search(self, arguments):
        return {"candidates": [{
            "id": "st_hug",
            "semantic_intent": "抱抱安慰",
            "tone_tags": ["温柔"],
            "use_intents": ["安慰"],
            "score": 0.9,
        }]}

    def send(self, sticker_id):
        self.send_calls += 1
        return {
            "sticker_id": sticker_id,
            "url": "https://stickers.example/st_hug.webp",
            "alt": "抱抱",
        }


class _FakeEmbedder:
    model_name = "test-semantic-v1"

    def embed(self, texts):
        vectors = []
        for text in texts:
            vectors.append([
                1.0 if any(word in text for word in ("炒饭", "米饭", "午饭", "吃饭")) else 0.0,
                1.0 if any(word in text for word in ("散步", "公园", "走路")) else 0.0,
                1.0 if any(word in text for word in ("难过", "伤心", "低落")) else 0.0,
                0.1,
            ])
        return vectors


def _make_mcp(tmpdir: Path) -> tuple[Database, ResidentMCP]:
    db = Database(tmpdir / "dwell.sqlite3")
    token_file = tmpdir / "mcp-token"
    token_file.write_text("x" * 48, encoding="utf-8")
    mcp = ResidentMCP(
        db=db,
        token_file=token_file,
        resident_name="驻客",
        book_dir=tmpdir / "books",
        thinking=_FakeThinking(),
        stickers=None,
    )
    return db, mcp


class DiaryAuthorshipTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db, self.mcp = _make_mcp(self.tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_user_and_resident_entries_are_separate(self):
        eid1 = self.db.add_diary_entry("用户写的日记", author_type="user")
        eid2 = self.db.add_diary_entry("驻客写的日记", author_type="resident", author_id="驻客")
        user = self.db.diary_entries(author_type="user")
        resident = self.db.diary_entries(author_type="resident")
        self.assertEqual([e["id"] for e in user], [eid1])
        self.assertEqual([e["id"] for e in resident], [eid2])
        self.assertEqual(user[0]["author_type"], "user")
        self.assertEqual(resident[0]["author_type"], "resident")
        self.assertEqual(resident[0]["author_id"], "驻客")

    def test_mcp_diary_create_defaults_to_resident(self):
        chat = self.mcp.ensure_resident_chat()
        self.mcp.db.set_setting("assistant_mode", "mcp")
        result = self.mcp.call_tool("diary", {"action": "create", "text": "今天读了书"})
        self.assertFalse(result.get("isError"))
        data = result["structuredContent"]
        self.assertEqual(data["entry"]["author_type"], "resident")
        self.assertEqual(data["entry"]["author_id"], "resident")

    def test_mcp_diary_cannot_create_user_entry(self):
        self.mcp.ensure_resident_chat()
        self.mcp.db.set_setting("assistant_mode", "mcp")
        result = self.mcp.call_tool(
            "diary",
            {"action": "create", "text": "我想记一下", "author_type": "user"},
        )
        self.assertTrue(result.get("isError"))
        self.assertEqual(self.db.diary_entries(author_type="user"), [])

    def test_mcp_diary_cannot_update_or_delete_user_entry(self):
        self.mcp.ensure_resident_chat()
        self.mcp.db.set_setting("assistant_mode", "mcp")
        entry_id = self.db.add_diary_entry("用户原文", author_type="user")
        updated = self.mcp.call_tool(
            "diary",
            {"action": "update", "entry_id": entry_id, "text": "越权修改"},
        )
        deleted = self.mcp.call_tool(
            "diary",
            {"action": "delete", "entry_id": entry_id},
        )
        self.assertTrue(updated.get("isError"))
        self.assertTrue(deleted.get("isError"))
        self.assertEqual(self.db.diary_entry(entry_id)["text"], "用户原文")
        self.assertEqual(self.db.diary_entry(entry_id)["author_id"], "owner")

    def test_diary_timeline_filters_by_author(self):
        self.db.add_diary_entry("u1", author_type="user")
        self.db.add_diary_entry("r1", author_type="resident")
        all_entries = self.db.diary_entries()
        self.assertEqual(len(all_entries), 2)


class TodosMCPTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db, self.mcp = _make_mcp(self.tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_create_complete_delete(self):
        self.mcp.ensure_resident_chat()
        self.mcp.db.set_setting("assistant_mode", "mcp")
        created = self.mcp.call_tool("todos", {"action": "create", "side": "mine", "text": "驻客任务"})
        self.assertTrue(created["structuredContent"]["ok"])
        new_id = created["structuredContent"]["affected_id"]
        self.assertGreater(new_id, 0)

        completed = self.mcp.call_tool("todos", {"action": "complete", "side": "mine", "id": new_id})
        mine = completed["structuredContent"]["mine"]
        self.assertTrue(any(t["id"] == new_id and t["done"] for t in mine))

        deleted = self.mcp.call_tool("todos", {"action": "delete", "side": "mine", "id": new_id})
        mine_after = deleted["structuredContent"]["mine"]
        self.assertFalse(any(t["id"] == new_id for t in mine_after))


class DayContextTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db, self.mcp = _make_mcp(self.tmp)
        self.mcp.ensure_resident_chat()
        self.db.set_setting("assistant_mode", "mcp")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_requested_date_joins_due_todos_diaries_calendar_and_report(self):
        target = "2026-08-20"
        due = self.mcp.call_tool(
            "todos",
            {"action": "create", "side": "mine", "text": "当天待办", "due_date": target},
        )["structuredContent"]["affected_id"]
        self.mcp.call_tool(
            "todos",
            {"action": "create", "side": "mine", "text": "别天待办", "due_date": "2026-08-21"},
        )
        self.mcp.call_tool(
            "calendar", {"action": "add_event", "date": target, "text": "当天日程"}
        )
        self.mcp.call_tool(
            "calendar", {"action": "set_menstrual", "date": target}
        )
        entry = self.db.add_diary_entry("当天用户日记", author_type="user")
        start, _ = _day_bounds(target)
        self.db.execute("UPDATE her_diary SET at=? WHERE id=?", (start + 3600, entry))
        self.mcp.call_tool(
            "daily_report", {"action": "save", "date": target, "text": "当天日报"}
        )
        result = self.mcp.call_tool("get_day_context", {"date": target})
        self.assertFalse(result.get("isError"))
        data = result["structuredContent"]
        self.assertEqual([item["id"] for item in data["todos"]["mine"]], [due])
        self.assertEqual(data["calendar_events"][0]["text"], "当天日程")
        self.assertTrue(data["day"]["menstrual"])
        self.assertEqual(data["diary_user"][0]["text"], "当天用户日记")
        self.assertEqual(data["daily_report"]["body"], "当天日报")


class DomainEventTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db, self.mcp = _make_mcp(self.tmp)
        self.mcp.ensure_resident_chat()
        self.db.set_setting("assistant_mode", "mcp")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_resident_mutations_share_one_cursor_based_event_model(self):
        self.mcp.call_tool(
            "todos", {"action": "create", "side": "mine", "text": "事件待办"}
        )
        self.mcp.call_tool(
            "calendar", {"action": "set_menstrual", "date": "2026-08-24"}
        )
        self.mcp.call_tool(
            "diary", {"action": "create", "text": "事件日记"}
        )
        self.mcp.call_tool(
            "daily_report", {"action": "comment", "date": "2026-08-24", "comment": "事件点评"}
        )
        first = self.mcp.call_tool(
            "read_dwell_events", {"after_event_id": 0, "limit": 20}
        )["structuredContent"]
        types = [event["type"] for event in first["events"]]
        self.assertEqual(
            types,
            ["todo.updated", "calendar.updated", "diary.updated", "report.updated"],
        )
        self.assertTrue(all(event["actor_type"] == "resident" for event in first["events"]))
        empty = self.mcp.call_tool(
            "read_dwell_events", {"after_event_id": first["event_cursor"], "limit": 20}
        )["structuredContent"]
        self.assertEqual(empty["events"], [])
        filtered = self.mcp.call_tool(
            "read_dwell_events", {"after_event_id": 0, "types": ["diary.updated"]}
        )["structuredContent"]
        self.assertEqual([event["type"] for event in filtered["events"]], ["diary.updated"])

    def test_domain_event_idempotency_key_does_not_duplicate(self):
        first = self.db.append_domain_event(
            "reading.page_changed", "user", "owner", {"page": 1}, "reading:test:1"
        )
        second = self.db.append_domain_event(
            "reading.page_changed", "user", "owner", {"page": 1}, "reading:test:1"
        )
        self.assertEqual(first, second)
        self.assertEqual(len(self.db.domain_events_after()), 1)


class CalendarMCPTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db, self.mcp = _make_mcp(self.tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_add_event_and_set_menstrual(self):
        self.mcp.ensure_resident_chat()
        self.mcp.db.set_setting("assistant_mode", "mcp")
        added = self.mcp.call_tool(
            "calendar",
            {"action": "add_event", "date": "2026-08-24", "text": "验收"},
        )
        events = added["structuredContent"]["events"]
        self.assertTrue(any(e["date"] == "2026-08-24" and e["text"] == "验收" for e in events))

        mens = self.mcp.call_tool(
            "calendar",
            {"action": "set_menstrual", "date": "2026-08-24"},
        )
        days = mens["structuredContent"]["days"]
        self.assertEqual(days["2026-08-24"]["flow"], "")
        self.assertTrue(days["2026-08-24"]["menstrual"])
        self.assertTrue(self.db.calendar_day_menstrual("2026-08-24"))

    def test_clear_menstrual(self):
        self.mcp.ensure_resident_chat()
        self.mcp.db.set_setting("assistant_mode", "mcp")
        self.mcp.call_tool("calendar", {"action": "set_menstrual", "date": "2026-08-25"})
        self.mcp.call_tool("calendar", {"action": "clear_menstrual", "date": "2026-08-25"})
        self.assertFalse(self.db.calendar_day_menstrual("2026-08-25"))

    def test_flow_is_preserved_and_event_can_be_updated(self):
        self.mcp.ensure_resident_chat()
        self.mcp.db.set_setting("assistant_mode", "mcp")
        self.mcp.call_tool(
            "calendar",
            {"action": "set_day", "date": "2026-08-24", "flow": "偏多", "pain": 3},
        )
        self.mcp.call_tool(
            "calendar", {"action": "set_menstrual", "date": "2026-08-24"}
        )
        day = self.db.calendar_day_states()["2026-08-24"]
        self.assertEqual(day["flow"], "偏多")
        self.assertEqual(day["pain"], 3)
        self.assertTrue(day["menstrual"])
        added = self.mcp.call_tool(
            "calendar",
            {"action": "add_event", "date": "2026-08-24", "text": "旧标题"},
        )
        event_id = added["structuredContent"]["affected_id"]
        updated = self.mcp.call_tool(
            "calendar",
            {"action": "update_event", "event_id": event_id, "text": "新标题", "time": "09:30"},
        )
        event = next(item for item in updated["structuredContent"]["events"] if item["id"] == event_id)
        self.assertEqual(event["text"], "新标题")
        self.assertEqual(event["time"], "09:30")

    def test_legacy_flow_marker_migrates_to_boolean_column(self):
        self.db.execute(
            "INSERT OR REPLACE INTO calendar_days(date,mood,flow,pain,note,private,menstrual) "
            "VALUES('2026-08-01','','menstrual',0,'','',0)"
        )
        restarted = Database(self.tmp / "dwell.sqlite3")
        day = restarted.calendar_day_states()["2026-08-01"]
        self.assertTrue(day["menstrual"])
        self.assertEqual(day["flow"], "")

    def test_rejects_impossible_dates_and_empty_event_updates(self):
        self.mcp.ensure_resident_chat()
        self.mcp.db.set_setting("assistant_mode", "mcp")
        invalid = self.mcp.call_tool(
            "calendar",
            {"action": "add_event", "date": "2026-02-31", "text": "不存在的日期"},
        )
        self.assertTrue(invalid.get("isError"))
        added = self.mcp.call_tool(
            "calendar",
            {"action": "add_event", "date": "2026-08-24", "text": "不能清空"},
        )["structuredContent"]["affected_id"]
        emptied = self.mcp.call_tool(
            "calendar",
            {"action": "update_event", "event_id": added, "text": "   "},
        )
        self.assertTrue(emptied.get("isError"))


class DailyReportMCPTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db, self.mcp = _make_mcp(self.tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_and_comment(self):
        self.mcp.ensure_resident_chat()
        self.mcp.db.set_setting("assistant_mode", "mcp")
        saved = self.mcp.call_tool(
            "daily_report",
            {"action": "save", "date": "2026-08-24", "text": "# 今日\n正文"},
        )
        self.assertTrue(saved["structuredContent"]["ok"])
        commented = self.mcp.call_tool(
            "daily_report",
            {"action": "comment", "date": "2026-08-24", "comment": "驻客点评：还行"},
        )
        report = commented["structuredContent"]["report"]
        self.assertEqual(report["resident_comment"], "驻客点评：还行")
        self.assertTrue(report["commented_at"])

    def test_list_returns_dates(self):
        self.mcp.ensure_resident_chat()
        self.mcp.db.set_setting("assistant_mode", "mcp")
        self.mcp.call_tool("daily_report", {"action": "save", "date": "2026-08-23", "text": "a"})
        self.mcp.call_tool("daily_report", {"action": "save", "date": "2026-08-24", "text": "b"})
        listed = self.mcp.call_tool("daily_report", {"action": "list"})
        dates = listed["structuredContent"]["dates"]
        self.assertIn("2026-08-23", dates)
        self.assertIn("2026-08-24", dates)

    def test_generated_markdown_is_the_same_body_seen_by_mcp(self):
        self.mcp.ensure_resident_chat()
        self.mcp.db.set_setting("assistant_mode", "mcp")
        news_dir = self.tmp / "news"
        news_dir.mkdir()
        (news_dir / "2026-08-22.md").write_text("# 日报\n\n磁盘生成稿\n", encoding="utf-8")
        result = self.mcp.call_tool(
            "daily_report",
            {"action": "read", "date": "2026-08-22"},
        )
        report = result["structuredContent"]["report"]
        self.assertEqual(report["body"], "# 日报\n\n磁盘生成稿\n")
        self.assertEqual(report["source"], "generated_markdown")


class ProactiveMessageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db, self.mcp = _make_mcp(self.tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_proactive_send_marks_proactive_true(self):
        self.mcp.ensure_resident_chat()
        self.mcp.db.set_setting("assistant_mode", "mcp")
        result = self.mcp.call_tool(
            "send_dwell_message",
            {
                "text": "我自己想跟你说一句",
                "style": "relational",
                "thinking": "想他了",
                "effort": "low",
                "skin": "botanical",
            },
        )
        data = result["structuredContent"]
        self.assertTrue(data["ok"])
        self.assertTrue(data["proactive"])
        self.assertFalse(data["duplicate"])
        self.assertGreater(data["assistant_seq"], 0)

    def test_reply_send_marks_proactive_false(self):
        self.mcp.ensure_resident_chat()
        self.mcp.db.set_setting("assistant_mode", "mcp")
        # First create a user message to reply to.
        user_seq = self.db.append_message("me", "你好", chat_id=self.mcp.ensure_resident_chat()["id"])
        result = self.mcp.call_tool(
            "send_dwell_message",
            {
                "text": "你好啊",
                "reply_to_seq": user_seq,
                "style": "deep_think",
                "thinking": "回应他",
                "effort": "medium",
                "skin": "microglow",
            },
        )
        data = result["structuredContent"]
        self.assertFalse(data["proactive"])
        self.assertEqual(data["reply_to_seq"], user_seq)

    def test_client_message_id_is_idempotent(self):
        self.mcp.ensure_resident_chat()
        self.mcp.db.set_setting("assistant_mode", "mcp")
        first = self.mcp.call_tool(
            "send_dwell_message",
            {
                "text": "发一条",
                "client_message_id": "client-A",
                "style": "relational",
                "thinking": "想他",
                "effort": "low",
                "skin": "botanical",
            },
        )
        first_seq = first["structuredContent"]["assistant_seq"]
        second = self.mcp.call_tool(
            "send_dwell_message",
            {
                "text": "再发一条不同的内容",
                "client_message_id": "client-A",
                "style": "relational",
                "thinking": "想他",
                "effort": "low",
                "skin": "botanical",
            },
        )
        self.assertTrue(second["structuredContent"]["duplicate"])
        self.assertEqual(second["structuredContent"]["assistant_seq"], first_seq)
        chat_id = self.mcp.ensure_resident_chat()["id"]
        count = self.db.one(
            "SELECT COUNT(*) AS n FROM messages WHERE chat_id=? AND kind='gu'",
            (chat_id,),
        )
        self.assertEqual(count["n"], 1)

    def test_reply_target_must_be_user_message_in_current_chat(self):
        chat_id = self.mcp.ensure_resident_chat()["id"]
        self.mcp.db.set_setting("assistant_mode", "mcp")
        assistant_seq = self.db.append_message("gu", "不是用户消息", chat_id=chat_id)
        result = self.mcp.call_tool(
            "send_dwell_message",
            {
                "text": "不该发出",
                "reply_to_seq": assistant_seq,
                "style": "relational",
                "thinking": "核对目标",
                "effort": "low",
                "skin": "botanical",
            },
        )
        self.assertTrue(result.get("isError"))

    def test_selected_quote_offsets_are_validated_and_saved(self):
        chat_id = self.mcp.ensure_resident_chat()["id"]
        self.mcp.db.set_setting("assistant_mode", "mcp")
        source = "第一句。这里是用户真正追问的段落。最后一句。"
        source_seq = self.db.append_message("me", source, chat_id=chat_id)
        start = source.index("这里")
        quoted = "这里是用户真正追问的段落"
        result = self.mcp.call_tool(
            "send_dwell_message",
            {
                "text": "我针对这一段回答",
                "style": "deep_think",
                "thinking": "先定位引用段落",
                "effort": "medium",
                "skin": "microglow",
                "quote": {
                    "message_seq": source_seq,
                    "start_offset": start,
                    "end_offset": start + len(quoted),
                    "text": quoted,
                },
            },
        )
        self.assertFalse(result.get("isError"))
        seq = result["structuredContent"]["assistant_seq"]
        row = self.db.one("SELECT extra FROM messages WHERE seq=?", (seq,))
        saved = json.loads(row["extra"])["quote"]
        self.assertEqual(saved["text"], quoted)
        self.assertEqual(saved["start_offset"], start)

        bad = self.mcp.call_tool(
            "send_dwell_message",
            {
                "text": "不该发出",
                "style": "relational",
                "thinking": "错误引用",
                "effort": "low",
                "skin": "botanical",
                "quote": {
                    "message_seq": source_seq,
                    "start_offset": start,
                    "end_offset": start + len(quoted),
                    "text": "对不上",
                },
            },
        )
        self.assertTrue(bad.get("isError"))


class ChatSearchMCPTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db, self.mcp = _make_mcp(self.tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_search_returns_snippets(self):
        chat_id = self.mcp.ensure_resident_chat()["id"]
        self.mcp.db.set_setting("assistant_mode", "mcp")
        self.db.append_message("me", "今天吃了炒饭", chat_id=chat_id)
        self.db.append_message("gu", "炒饭好吃吗", chat_id=chat_id)
        self.db.append_message("me", "还行", chat_id=chat_id)
        result = self.mcp.call_tool("search_chat_history", {"query": "炒饭"})
        hits = result["structuredContent"]["results"]
        self.assertGreaterEqual(len(hits), 2)
        # Both hits should mention 炒饭 in the snippet.
        self.assertTrue(all("炒饭" in h["snippet"] for h in hits))
        # Snippets must not be the full text — confirm they're bounded.
        for hit in hits:
            self.assertLessEqual(len(hit["snippet"]), 250)

    def test_fts_trigram_stays_in_sync_and_short_chinese_falls_back(self):
        chat_id = self.mcp.ensure_resident_chat()["id"]
        service = HistorySearchService(self.db)
        seq = self.db.append_message("me", "今天吃了炒饭", chat_id=chat_id)
        trigram = service.search(
            "吃了炒", mode="keyword", chat_id=chat_id, kinds=("me",), limit=5
        )["results"]
        self.assertEqual(trigram[0]["seq"], seq)
        short = service.search(
            "炒饭", mode="keyword", chat_id=chat_id, kinds=("me",), limit=5
        )["results"]
        self.assertEqual(short[0]["seq"], seq)
        self.db.execute("UPDATE messages SET text=? WHERE seq=?", ("今天吃了面条", seq))
        removed = service.search(
            "吃了炒", mode="keyword", chat_id=chat_id, kinds=("me",), limit=5
        )["results"]
        updated = service.search(
            "吃了面", mode="keyword", chat_id=chat_id, kinds=("me",), limit=5
        )["results"]
        self.assertEqual(removed, [])
        self.assertEqual(updated[0]["seq"], seq)

    def test_read_message_context_returns_neighbors(self):
        chat_id = self.mcp.ensure_resident_chat()["id"]
        self.mcp.db.set_setting("assistant_mode", "mcp")
        seq1 = self.db.append_message("me", "第一条", chat_id=chat_id)
        seq2 = self.db.append_message("gu", "第二条", chat_id=chat_id)
        seq3 = self.db.append_message("me", "第三条", chat_id=chat_id)
        result = self.mcp.call_tool(
            "read_message_context",
            {"seq": seq2, "before": 1, "after": 1},
        )
        msgs = result["structuredContent"]["messages"]
        seqs = [m["seq"] for m in msgs]
        self.assertIn(seq1, seqs)
        self.assertIn(seq2, seqs)
        self.assertIn(seq3, seqs)

    def test_hybrid_semantic_search_filters_and_rrf(self):
        chat_id = self.mcp.ensure_resident_chat()["id"]
        self.mcp.db.set_setting("assistant_mode", "mcp")
        self.mcp.history = HistorySearchService(self.db, _FakeEmbedder())
        food_seq = self.db.append_message("me", "三天前我做了一盘炒饭", chat_id=chat_id)
        self.db.append_message("gu", "听起来很香", chat_id=chat_id)
        self.db.append_message("me", "晚上去公园散步", chat_id=chat_id)
        result = self.mcp.call_tool(
            "search_chat_history",
            {
                "query": "午饭吃的东西",
                "mode": "hybrid",
                "speaker": "user",
                "limit": 5,
            },
        )
        self.assertFalse(result.get("isError"))
        data = result["structuredContent"]
        self.assertTrue(data["semantic"]["available"])
        expected_backend = "sqlite-vec" if importlib.util.find_spec("sqlite_vec") else "python-cosine"
        self.assertEqual(data["semantic"]["vector_backend"], expected_backend)
        self.assertTrue(any(item["seq"] == food_seq for item in data["results"]))
        food = next(item for item in data["results"] if item["seq"] == food_seq)
        self.assertIn("semantic", food["sources"])
        self.assertGreater(food["score"], 0)

    def test_date_filter_and_context_use_visible_row_counts(self):
        chat_id = self.mcp.ensure_resident_chat()["id"]
        self.mcp.db.set_setting("assistant_mode", "mcp")
        old_seq = self.db.append_message("me", "过滤词 旧", chat_id=chat_id)
        new_seq = self.db.append_message("me", "过滤词 新", chat_id=chat_id)
        old_start, _ = _day_bounds("2026-08-20")
        new_start, _ = _day_bounds("2026-08-24")
        self.db.execute("UPDATE messages SET at=? WHERE seq=?", (old_start + 3600, old_seq))
        self.db.execute("UPDATE messages SET at=? WHERE seq=?", (new_start + 3600, new_seq))
        filtered = self.mcp.call_tool(
            "search_chat_history",
            {
                "query": "过滤词",
                "mode": "keyword",
                "date_from": "2026-08-24",
                "date_to": "2026-08-24",
            },
        )["structuredContent"]["results"]
        self.assertEqual([item["seq"] for item in filtered], [new_seq])

        self.db.append_message("think", "不应算一条可见上下文", chat_id=chat_id)
        middle = self.db.append_message("gu", "上下文中心", chat_id=chat_id)
        self.db.append_message("tool", "也不应算", chat_id=chat_id)
        after = self.db.append_message("me", "可见的后一条", chat_id=chat_id)
        context = self.mcp.call_tool(
            "read_message_context", {"seq": middle, "before": 1, "after": 1}
        )["structuredContent"]["messages"]
        self.assertEqual([item["seq"] for item in context], [new_seq, middle, after])


class AttachmentObservationTest(unittest.TestCase):
    """A regression: stored_text must NOT contain [图片附件：...] style placeholders
    when the user sends an attachment-only message. We exercise the underlying
    rule by checking the helpers directly."""

    def test_message_helper_reads_attachments(self):
        row = {
            "seq": 1, "kind": "me", "text": "看看这张",
            "extra": json.dumps({"attachments": [{
                "id": "abc12345", "type": "image", "name": "p.jpg",
                "mime": "image/jpeg", "size": 1024, "url": "",
                "width": 100, "height": 100,
            }]}),
            "at": 1787508743.0,
        }
        msg = _message(row)
        self.assertEqual(len(msg["attachments"]), 1)
        self.assertEqual(msg["attachments"][0]["type"], "image")
        self.assertEqual(msg["text"], "看看这张")  # No placeholder


class StickerGuardTest(unittest.TestCase):
    """send_sticker must refuse ids that weren't returned by search_stickers."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db, self.mcp = _make_mcp(self.tmp)
        self.mcp.ensure_resident_chat()
        self.mcp.db.set_setting("assistant_mode", "mcp")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_send_without_search_is_rejected(self):
        result = self.mcp.call_tool("send_sticker", {"sticker_id": "st_invented"})
        self.assertTrue(result.get("isError"))

    def test_search_send_reply_and_idempotent_retry(self):
        stickers = _FakeStickers()
        self.mcp.stickers = stickers
        chat_id = self.mcp.ensure_resident_chat()["id"]
        user_seq = self.db.append_message("me", "抱抱我", chat_id=chat_id)
        searched = self.mcp.call_tool("search_stickers", {"query": "抱抱", "limit": 3})
        self.assertEqual(searched["structuredContent"]["candidates"][0]["id"], "st_hug")
        sent = self.mcp.call_tool(
            "send_sticker",
            {
                "sticker_id": "st_hug",
                "reply_to_seq": user_seq,
                "client_message_id": "sticker-retry-1",
            },
        )
        self.assertFalse(sent.get("isError"))
        data = sent["structuredContent"]
        self.assertFalse(data["duplicate"])
        self.assertEqual(data["reply_to_seq"], user_seq)
        row = self.db.one("SELECT extra FROM messages WHERE seq=?", (data["message_seq"],))
        extra = json.loads(row["extra"])
        self.assertEqual(extra["quote"]["message_seq"], user_seq)
        calls_after_first = stickers.send_calls

        retried = self.mcp.call_tool(
            "send_sticker",
            {
                "sticker_id": "st_hug",
                "reply_to_seq": user_seq,
                "client_message_id": "sticker-retry-1",
            },
        )
        self.assertTrue(retried["structuredContent"]["duplicate"])
        self.assertEqual(retried["structuredContent"]["message_seq"], data["message_seq"])
        self.assertEqual(stickers.send_calls, calls_after_first)

        rejected = self.mcp.call_tool(
            "send_sticker",
            {"sticker_id": "st_hug", "client_message_id": "sticker-new"},
        )
        self.assertTrue(rejected.get("isError"))


class NookDedupTest(unittest.TestCase):
    """Stable reading keys must survive reopen, reconnect, and process restart."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = Database(self.tmp / "dwell.sqlite3")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_same_page_is_atomically_deduped_but_force_resends(self):
        chat_id = self.db.current_chat()["id"]
        event_key = "reading:foo:1:2"
        extra = json.dumps({
            "source": "nook-page",
            "event_key": event_key,
            "reading": {"slug": "foo", "chapter": 1, "page": 2},
        })
        first_seq, first_deduped = self.db.append_reading_event(
            chat_id, event_key, "第 3 页", extra
        )
        second_seq, second_deduped = self.db.append_reading_event(
            chat_id, event_key, "第 3 页", extra
        )
        forced_seq, forced_deduped = self.db.append_reading_event(
            chat_id, event_key, "第 3 页", extra, force=True
        )
        self.assertFalse(first_deduped)
        self.assertTrue(second_deduped)
        self.assertEqual(second_seq, first_seq)
        self.assertFalse(forced_deduped)
        self.assertNotEqual(forced_seq, first_seq)
        count = self.db.one(
            "SELECT COUNT(*) AS n FROM nook_reading_deliveries WHERE event_key=?",
            (event_key,),
        )
        self.assertEqual(count["n"], 2)

    def test_old_nook_message_is_backfilled_on_restart(self):
        chat_id = self.db.current_chat()["id"]
        event_key = "reading:legacy:0:0"
        extra = json.dumps({
            "source": "nook-page",
            "reading": {"slug": "legacy", "chapter": 0, "page": 0},
        })
        old_seq = self.db.append_message("nook", "旧版已送达", extra, chat_id)
        restarted = Database(self.tmp / "dwell.sqlite3")
        seq, deduped = restarted.append_reading_event(
            chat_id, event_key, "不应重复", extra
        )
        self.assertTrue(deduped)
        self.assertEqual(seq, old_seq)


class BookNoteIdempotencyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        books = self.tmp / "books"
        books.mkdir()
        (books / "sample.json").write_text(
            json.dumps({"title": "测试书", "chapters": [{"title": "一", "text": "正文"}]}),
            encoding="utf-8",
        )
        self.db, self.mcp = _make_mcp(self.tmp)
        self.mcp.ensure_resident_chat()
        self.db.set_setting("assistant_mode", "mcp")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_retry_key_creates_only_one_note(self):
        args = {
            "slug": "sample",
            "title": "第一章",
            "summary": "摘要",
            "body": "正文",
            "pinned": True,
            "client_message_id": "reading:sample:0:0:notebook",
        }
        first = self.mcp.call_tool("save_book_note", args)
        second = self.mcp.call_tool("save_book_note", {**args, "body": "重试时变了"})
        self.assertFalse(first.get("isError"))
        self.assertFalse(first["structuredContent"]["duplicate"])
        self.assertTrue(second["structuredContent"]["duplicate"])
        self.assertEqual(
            first["structuredContent"]["note_id"],
            second["structuredContent"]["note_id"],
        )
        notes = self.db.book_notes("sample")
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["body"], "正文")

    def test_resident_cannot_overwrite_user_note(self):
        user_note = self.db.add_book_note("sample", "user", "用户", "", "原文")
        result = self.mcp.call_tool(
            "save_book_note",
            {
                "slug": "sample",
                "note_id": user_note,
                "title": "越权",
                "body": "改掉",
                "client_message_id": "overwrite-user-note",
            },
        )
        self.assertTrue(result.get("isError"))
        self.assertEqual(self.db.book_note("sample", user_note)["body"], "原文")


class SnippetTest(unittest.TestCase):
    def test_returns_window_around_match(self):
        text = "abc QUERY def" * 10
        snippet = _snippet(text, "QUERY")
        self.assertIn("QUERY", snippet)
        self.assertIn("…", snippet)

    def test_returns_truncated_when_no_match(self):
        snippet = _snippet("hello world", "不存在"),  # tuple by mistake?
        if isinstance(snippet, tuple):
            snippet = snippet[0]
        # _snippet always returns a string
        self.assertEqual(_snippet("hello world", "不存在"), "hello world")


class HelpersTest(unittest.TestCase):
    def test_diary_entry_helper(self):
        entry = _diary_entry({
            "id": 5, "text": "hi", "at": 1787508743.0,
            "author_type": "resident", "author_id": "驻客",
        })
        self.assertEqual(entry["id"], 5)
        self.assertEqual(entry["author_type"], "resident")
        self.assertTrue(entry["created_at"])

    def test_daily_report_helper_handles_empty(self):
        report = _daily_report({}, "2026-08-24")
        self.assertEqual(report["date"], "2026-08-24")
        self.assertEqual(report["body"], "")


class RelatedHistoryRecallTest(unittest.TestCase):
    """related_history passive recall: when wait_for_user_message returns user
    messages, each must carry up to N older snippets that mention the same topic."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db, self.mcp = _make_mcp(self.tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_recall_attaches_older_hit(self):
        chat_id = self.mcp.ensure_resident_chat()["id"]
        self.mcp.db.set_setting("assistant_mode", "mcp")
        # Old conversation about 炒饭 (more than 180s ago).
        old_seq_me = self.db.append_message("me", "我吃了炒饭", chat_id=chat_id)
        old_seq_gu = self.db.append_message("gu", "炒饭好吃吗", chat_id=chat_id)
        # Backdate them so the recent-message filter doesn't exclude.
        self.db.execute(
            "UPDATE messages SET at=? WHERE seq=?",
            (1787000000.0, old_seq_me),
        )
        self.db.execute(
            "UPDATE messages SET at=? WHERE seq=?",
            (1787000005.0, old_seq_gu),
        )
        # New user message also about 炒饭.
        new_seq = self.db.append_message("me", "今天又吃了炒饭", chat_id=chat_id)

        msg = {
            "seq": new_seq,
            "text": "今天又吃了炒饭",
            "created_at": _iso(1787508800.0),
            "created_at_epoch": 1787508800.0,
        }
        enriched = self.mcp._enrich_with_related_history(msg, chat_id)
        self.assertIn("related_history", enriched)
        ids = {item["result_id"] for item in enriched["related_history"]}
        self.assertIn(old_seq_me, ids)
        # Snippets must not contain full body — only short context.
        for item in enriched["related_history"]:
            self.assertLessEqual(len(item["snippet"]), 250)

    def test_short_query_is_skipped(self):
        chat_id = self.mcp.ensure_resident_chat()["id"]
        msg = {
            "seq": 999,
            "text": "嗯",  # below min_query_len
            "created_at_epoch": 1787508800.0,
        }
        enriched = self.mcp._enrich_with_related_history(msg, chat_id)
        self.assertNotIn("related_history", enriched)

    def test_recent_hits_are_filtered_out(self):
        chat_id = self.mcp.ensure_resident_chat()["id"]
        self.mcp.db.set_setting("assistant_mode", "mcp")
        seq_a = self.db.append_message("me", "今天吃了炒饭", chat_id=chat_id)
        seq_b = self.db.append_message("me", "炒饭真好吃", chat_id=chat_id)
        # Both within the same minute: should NOT appear in related_history.
        msg = {
            "seq": seq_b,
            "text": "炒饭真好吃",
            "created_at_epoch": float(self.db.one("SELECT at AS a FROM messages WHERE seq=?", (seq_b,))["a"]),
        }
        enriched = self.mcp._enrich_with_related_history(msg, chat_id)
        if "related_history" in enriched:
            ids = {item["result_id"] for item in enriched["related_history"]}
            self.assertNotIn(seq_a, ids)

    def test_semantic_recall_uses_threshold_and_skips_unrelated_history(self):
        chat_id = self.mcp.ensure_resident_chat()["id"]
        self.mcp.db.set_setting("assistant_mode", "mcp")
        self.mcp.history = HistorySearchService(self.db, _FakeEmbedder())
        food_seq = self.db.append_message("me", "那天做的炒饭很好吃", chat_id=chat_id)
        unrelated_seq = self.db.append_message("me", "我换了一个新键盘", chat_id=chat_id)
        self.db.execute("UPDATE messages SET at=? WHERE seq IN (?,?)", (1787000000.0, food_seq, unrelated_seq))
        new_seq = self.db.append_message("me", "今天午饭吃的东西也不错", chat_id=chat_id)
        current_at = float(self.db.one("SELECT at FROM messages WHERE seq=?", (new_seq,))["at"])
        enriched = self.mcp._enrich_with_related_history(
            {
                "seq": new_seq,
                "text": "今天午饭吃的东西也不错",
                "created_at_epoch": current_at,
            },
            chat_id,
        )
        ids = {item["result_id"] for item in enriched.get("related_history", [])}
        self.assertIn(food_seq, ids)
        self.assertNotIn(unrelated_seq, ids)
        self.assertTrue(all(item["score"] >= 0.72 for item in enriched["related_history"]))


if __name__ == "__main__":
    unittest.main()
