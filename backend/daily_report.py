"""Shared daily-report service for the web API and resident MCP."""

from __future__ import annotations

import re
from datetime import date as calendar_date
from pathlib import Path
from typing import Any

from .store import Database


DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


class DailyReportService:
    """Present one report contract over generated Markdown and DB metadata.

    Scheduled reports historically live in ``news/<date>.md`` while comments
    and resident-authored drafts live in SQLite.  Both the browser and MCP must
    resolve those stores in exactly the same way: a non-empty DB body is the
    editable override, otherwise the generated Markdown file is the body.
    """

    def __init__(self, db: Database, news_dir: Path):
        self.db = db
        self.news_dir = news_dir

    @staticmethod
    def valid_date(value: str) -> bool:
        raw = str(value or "")
        if not DATE_RE.fullmatch(raw):
            return False
        try:
            calendar_date.fromisoformat(raw)
        except ValueError:
            return False
        return True

    def dates(self) -> list[str]:
        disk_dates: list[str] = []
        try:
            disk_dates = [
                path.stem
                for path in self.news_dir.glob("*.md")
                if self.valid_date(path.stem)
            ]
        except OSError:
            pass
        return sorted(set(disk_dates) | set(self.db.daily_report_dates()), reverse=True)

    def read(self, date: str) -> dict[str, Any] | None:
        if not self.valid_date(date):
            return None
        row = self.db.daily_report(date) or {}
        body = str(row.get("body") or "")
        source = "database" if body else ""
        if not body:
            path = self.news_dir / f"{date}.md"
            try:
                body = path.read_text(encoding="utf-8") if path.is_file() else ""
            except OSError:
                body = ""
            if body:
                source = "generated_markdown"
        if not body and not row:
            return None
        return {
            "date": date,
            "body": body,
            "resident_comment": str(row.get("resident_comment") or ""),
            "commented_at": float(row.get("commented_at") or 0),
            "updated_at": float(row.get("updated_at") or 0),
            "source": source,
        }

    def save(self, date: str, body: str) -> dict[str, Any]:
        self.db.upsert_daily_report(date, body)
        return self.read(date) or {
            "date": date,
            "body": body,
            "resident_comment": "",
            "commented_at": 0.0,
            "updated_at": 0.0,
            "source": "database",
        }

    def comment(self, date: str, comment: str) -> dict[str, Any]:
        self.db.update_daily_report_comment(date, comment)
        return self.read(date) or {
            "date": date,
            "body": "",
            "resident_comment": comment,
            "commented_at": 0.0,
            "updated_at": 0.0,
            "source": "",
        }
