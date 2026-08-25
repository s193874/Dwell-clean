"""Shared reading helpers used by the HTTP UI and resident MCP."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


SLUG_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}")
PAGE_CHARS = 1200
DEFAULT_NOTEBOOK_PROMPT = (
    "读到每章最后一页时，更新本书长期记事本：优先补充已有条目，记录人物关系、"
    "关键事件、重要线索和未解问题；标题要便于检索，摘要要能独立说明这一条在讲什么。"
)


def load_book(book_dir: Path, slug: str) -> dict[str, Any] | None:
    if not SLUG_RE.fullmatch(slug or ""):
        return None
    path = book_dir / f"{slug}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def chapter_parts(book: dict[str, Any], index: int) -> tuple[str, str] | None:
    chapters = book.get("chapters") or []
    if index < 0 or index >= len(chapters):
        return None
    chapter = chapters[index]
    if isinstance(chapter, dict):
        return (
            str(chapter.get("title") or f"第 {index + 1} 节"),
            str(chapter.get("text") or ""),
        )
    return f"第 {index + 1} 节", str(chapter)


def paginate(text: str, page_chars: int = PAGE_CHARS) -> list[str]:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n|\n", text) if part.strip()]
    if not paragraphs:
        return [""]
    pages: list[str] = []
    current: list[str] = []
    size = 0
    for paragraph in paragraphs:
        chunks = [
            paragraph[start : start + page_chars]
            for start in range(0, len(paragraph), page_chars)
        ] or [""]
        for chunk in chunks:
            extra = len(chunk) + (2 if current else 0)
            if current and size + extra > page_chars:
                pages.append("\n\n".join(current))
                current = []
                size = 0
            current.append(chunk)
            size += len(chunk) + (2 if len(current) > 1 else 0)
    if current:
        pages.append("\n\n".join(current))
    return pages or [""]


def page_context(book_dir: Path, slug: str, chapter: int, page: int) -> dict[str, Any] | None:
    book = load_book(book_dir, slug)
    if not book:
        return None
    parts = chapter_parts(book, chapter)
    if not parts:
        return None
    chapter_title, text = parts
    pages = paginate(text)
    page = max(0, min(int(page), len(pages) - 1))
    previous_page_text = pages[page - 1] if page > 0 else ""
    if page == 0 and chapter > 0:
        previous = chapter_parts(book, chapter - 1)
        if previous:
            previous_pages = paginate(previous[1])
            previous_page_text = previous_pages[-1]
    return {
        "slug": slug,
        "book_title": str(book.get("title") or slug),
        "chapter": chapter,
        "chapter_title": chapter_title,
        "page": page,
        "page_total": len(pages),
        "page_text": pages[page],
        "previous_page_text": previous_page_text,
    }
