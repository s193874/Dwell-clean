#!/usr/bin/env python3
"""Collect news material and optionally write one dwell daily report.

Production scheduling is owned by ``deploy/dwell-news-daily.service`` and
``deploy/dwell-news-daily.timer``.  The command remains independently runnable
for collection, dry-run, and repair work.
"""

from __future__ import annotations

import argparse
import html
import ipaddress
import os
import re
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable, Iterable


if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.provider import OpenAIProvider, ProviderConfig, ProviderError


ROOT = Path(__file__).resolve().parents[1]
CN_TZ = timezone(timedelta(hours=8))
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DEFAULT_TIMEOUT = 15.0


class NewsError(RuntimeError):
    """A user-facing error that should not print a traceback by default."""


@dataclass(frozen=True)
class Story:
    title: str
    summary: str
    link: str
    source: str


@dataclass(frozen=True)
class Section:
    name: str
    query: str
    style: str
    feeds: tuple[str, ...]
    story_count: int | None = None


def google_news(query: str) -> str:
    params = {
        "q": f"{query} when:1d",
        "hl": "zh-CN",
        "gl": "CN",
        "ceid": "CN:zh-Hans",
    }
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(params)


SECTIONS = (
    Section(
        "科技与 AI",
        "人工智能 OR 大模型",
        "偏重真正的技术进展和行业动向；纯粹的参数堆砌合并成一句带过。",
        (google_news("人工智能 OR 大模型"), "https://www.ithome.com/rss/"),
        2,
    ),
    Section(
        "ChatGPT 与其他 AI",
        "ChatGPT OR OpenAI OR DeepSeek OR Kimi OR GLM OR 智谱 OR Gemini OR Claude OR Anthropic",
        "重点关注 ChatGPT、DeepSeek、Kimi、GLM 等模型和产品的真正变化；"
        "也可以挑当天值得看的其他 AI 动态，不让单一厂商长期占满版面。",
        (
            google_news(
                "ChatGPT OR OpenAI OR DeepSeek OR Kimi OR GLM OR 智谱 OR Gemini OR Claude OR Anthropic"
            ),
        ),
        10,
    ),
    Section(
        "社会",
        "社会 民生",
        "把事件说清楚，必要时补一句它为什么值得关注，不把热搜词当成事实本身。",
        (google_news("社会 民生"),),
        2,
    ),
    Section(
        "娱乐",
        "明星 OR 娱乐圈",
        "可以轻一点、活一点，但不编造未被来源支持的细节。",
        (google_news("明星 OR 娱乐圈"),),
        2,
    ),
)


def today_cn() -> str:
    return datetime.now(CN_TZ).date().isoformat()


def validate_date(value: str) -> str:
    if not DATE_RE.fullmatch(value):
        raise NewsError("日期必须是 YYYY-MM-DD")
    try:
        Date.fromisoformat(value)
    except ValueError as exc:
        raise NewsError("日期不是有效的日历日期") from exc
    return value


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child(element: ET.Element, names: Iterable[str]) -> ET.Element | None:
    wanted = {name.lower() for name in names}
    for child in list(element):
        if _local_name(child.tag) in wanted:
            return child
    return None


def _clean(value: str | None) -> str:
    text = html.unescape(value or "")
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _link(element: ET.Element) -> str:
    for child in list(element):
        if _local_name(child.tag) != "link":
            continue
        href = (child.attrib.get("href") or "").strip()
        if href:
            return href
        value = (child.text or "").strip()
        if value:
            return value
    return ""


def parse_feed(payload: bytes, source: str = "") -> list[Story]:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise NewsError(f"RSS/XML 解析失败：{exc}") from exc

    parsed: list[Story] = []
    for item in root.iter():
        if _local_name(item.tag) not in {"item", "entry"}:
            continue
        title_node = _child(item, ("title",))
        summary_node = _child(item, ("description", "summary", "content"))
        title = _clean(title_node.text if title_node is not None else "")
        summary = _clean(summary_node.text if summary_node is not None else "")
        link = _link(item)
        if not title:
            continue
        parsed.append(Story(title=title, summary=summary, link=link, source=source))
    return parsed


def _open_url(request: urllib.request.Request, timeout: float):
    host = urllib.parse.urlsplit(request.full_url).hostname or ""
    direct = host.lower() == "localhost"
    if not direct:
        try:
            direct = ipaddress.ip_address(host).is_loopback
        except ValueError:
            pass
    if direct:
        return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
            request, timeout=timeout
        )
    return urllib.request.urlopen(request, timeout=timeout)


def fetch_feed(url: str, timeout: float = DEFAULT_TIMEOUT) -> list[Story]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml",
            "User-Agent": "dwell-news-daily/1.0",
        },
    )
    try:
        with _open_url(request, timeout) as response:
            payload = response.read(2 * 1024 * 1024 + 1)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise NewsError(f"新闻源访问失败：{urlparse_host(url)}：{exc}") from exc
    if len(payload) > 2 * 1024 * 1024:
        raise NewsError(f"新闻源返回过大：{urlparse_host(url)}")
    return parse_feed(payload, urlparse_host(url))


def urlparse_host(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc or url


def dedupe(stories: Iterable[Story], limit: int = 12) -> list[Story]:
    result: list[Story] = []
    seen: set[str] = set()
    for story in stories:
        key = story.link or story.title.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(story)
        if len(result) >= limit:
            break
    return result


def collect_sections(
    sections: Iterable[Section] = SECTIONS,
    fetcher: Callable[[str, float], list[Story]] = fetch_feed,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[dict[str, list[Story]], list[str]]:
    collected: dict[str, list[Story]] = {}
    errors: list[str] = []
    for section in sections:
        stories: list[Story] = []
        for url in section.feeds:
            try:
                stories.extend(fetcher(url, timeout))
            except NewsError as exc:
                errors.append(f"{section.name}: {exc}")
        collected[section.name] = dedupe(stories)
    return collected, errors


def _db_setting(db_path: Path, key: str) -> str:
    if not db_path.exists():
        return ""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return ""
    return str(row[0]) if row else ""


def provider_config() -> ProviderConfig:
    data_dir = Path(os.environ.get("DWELL_DATA_DIR", ROOT / "data")).resolve()
    base = os.environ.get("DWELL_API_BASE") or _db_setting(data_dir / "dwell.sqlite3", "api_base")
    token = os.environ.get("DWELL_API_TOKEN") or _db_setting(data_dir / "dwell.sqlite3", "api_token")
    model = os.environ.get("DWELL_MODEL") or _db_setting(data_dir / "dwell.sqlite3", "model")
    if not base or not token or not model:
        missing = [name for name, value in (("DWELL_API_BASE", base), ("DWELL_API_TOKEN", token), ("DWELL_MODEL", model)) if not value]
        raise NewsError("模型配置不完整，缺少：" + "、".join(missing))
    return ProviderConfig(base=base, token=token, model=model, effort="medium")


def _material(stories: list[Story]) -> str:
    lines: list[str] = []
    for story in stories:
        lines.append(f"- 标题：{story.title}")
        if story.summary:
            lines.append(f"  摘要：{story.summary}")
        if story.link:
            lines.append(f"  来源：{story.link}")
    return "\n".join(lines)


def _strip_fence(text: str) -> str:
    value = text.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        value = "\n".join(lines).strip()
    return value


def _section_format_ok(text: str, expected_stories: int | None = None) -> bool:
    """Require one heading and one linked summary/source line per story."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    index = 0
    stories = 0
    while index < len(lines):
        if not lines[index].startswith("### ") or not lines[index][4:].strip():
            return False
        index += 1
        if index >= len(lines):
            return False
        match = re.fullmatch(r"-\s+(.+?)｜来源：(.+)", lines[index])
        if not match or not match.group(1).strip() or not _source_links_ok(match.group(2)):
            return False
        index += 1
        stories += 1
    if expected_stories is not None:
        return stories == expected_stories
    return 1 <= stories <= 4


def _source_links_ok(value: str) -> bool:
    """Allow one or more safe Markdown links separated only by punctuation."""
    link_re = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
    cursor = 0
    links = 0
    for match in link_re.finditer(value):
        separator = value[cursor : match.start()]
        if (links == 0 and separator.strip()) or (
            links > 0 and not re.fullmatch(r"\s*[、,，]\s*", separator)
        ):
            return False
        parsed_source = urllib.parse.urlsplit(match.group(2))
        if parsed_source.scheme not in {"http", "https"} or not parsed_source.netloc:
            return False
        cursor = match.end()
        links += 1
    return links > 0 and not value[cursor:].strip()


def _normalize_section_format(text: str, expected_stories: int | None = None) -> str:
    """Normalize common model formatting drift without changing story facts."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or not lines[0].startswith("### "):
        return text.strip()

    normalized: list[str] = []
    index = 0
    while index < len(lines):
        heading = lines[index]
        if not heading.startswith("### ") or not heading[4:].strip():
            return text.strip()
        index += 1
        body_lines: list[str] = []
        while index < len(lines) and not lines[index].startswith("### "):
            if lines[index].startswith("#"):
                return text.strip()
            body_lines.append(re.sub(r"^[-*]\s+", "", lines[index]))
            index += 1
        body = " ".join(part for part in body_lines if part).strip()
        if not body:
            return text.strip()

        separated = re.fullmatch(r"(.+?)\s*[｜|]\s*(.+)", body)
        if separated:
            summary, source = separated.groups()
            source = re.sub(r"^来源\s*[:：]?\s*", "", source).strip()
        else:
            inline = re.fullmatch(r"(.+?)\s*来源\s*[:：]\s*(.+)", body)
            if not inline:
                inline = re.fullmatch(r"(.+?)\s*[（(]\s*来源\s*[:：]\s*(.+)[）)]", body)
            if not inline:
                return text.strip()
            summary, source = inline.groups()
        summary = summary.strip()
        source = source.strip()
        if not summary or not source:
            return text.strip()
        normalized.extend((heading, f"- {summary}｜来源：{source}"))

    result = "\n".join(normalized)
    return result if _section_format_ok(result, expected_stories) else text.strip()


def write_section(provider: OpenAIProvider, section: Section, stories: list[Story]) -> str:
    selection_rule = (
        f"恰好挑 {section.story_count} 条"
        if section.story_count is not None
        else "挑真正值得说的 1 到 4 条"
    )
    length_limit = max(400, (section.story_count or 4) * 100)
    prompt = (
        "你在给一个人编一份只给她看的中文日报。\n\n"
        f"版块：{section.name}\n"
        f"这一版的写法：{section.style}\n\n"
        "下面是今天抓到的原始素材（标题、摘要、来源）：\n"
        f"{_material(stories)}\n\n"
        f"要求：{selection_rule}；每条写成人话，不复制标题；"
        f"不要凭素材之外的内容补细节；有来源就保留来源；总长度控制在 {length_limit} 字以内。"
        "只返回 Markdown 正文，不要返回代码围栏、解释或版块标题。"
        "每条必须严格占两行：第一行 `### 标题`，"
        "第二行 `- 摘要｜来源：[站点名](素材中的原始URL)`；"
        "同一条有多个来源时，用 `、` 分隔多个完整 Markdown 链接；"
        "链接必须逐字使用素材里的 http/https URL，不能编造或省略；"
        "不要把来源写进摘要句尾，不要在两行之间插入正文段落。"
    )
    try:
        message = provider.complete(
            [
                {
                    "role": "system",
                    "content": "你是严谨、简洁的中文日报编辑，只根据给定素材写稿。",
                },
                {"role": "user", "content": prompt},
            ]
        )
    except ProviderError as exc:
        raise NewsError(f"写稿模型调用失败：{exc}") from exc
    text = _normalize_section_format(
        _strip_fence(str(message.get("content") or "")), section.story_count
    )
    if not text:
        raise NewsError(f"版块“{section.name}”没有得到稿件")
    if not _section_format_ok(text, section.story_count):
        repair_prompt = (
            "只根据下面原始素材筛选并整理草稿，不编造或改写事实。"
            f"{selection_rule}。"
            "每条严格使用两行：第一行 `### 标题`，"
            "第二行 `- 摘要｜来源：[站点名](素材中的原始URL)`。"
            "同一条有多个来源时，用 `、` 分隔多个完整 Markdown 链接。"
            "链接必须逐字取自原始素材，不能编造。"
            "不要输出版块标题、解释、代码围栏或额外段落。"
            "\n\n原始素材：\n" + _material(stories) + "\n\n草稿：\n" + text
        )
        try:
            repaired = provider.complete(
                [
                    {
                        "role": "system",
                        "content": "你只根据原始素材筛选并整理格式，不能编造稿件事实。",
                    },
                    {"role": "user", "content": repair_prompt},
                ]
            )
        except ProviderError as exc:
            raise NewsError(f"版块“{section.name}”格式整理失败：{exc}") from exc
        text = _normalize_section_format(
            _strip_fence(str(repaired.get("content") or "")), section.story_count
        )
        if not _section_format_ok(text, section.story_count):
            raise NewsError(
                f"版块“{section.name}”没有返回指定条数及统一的标题、摘要和来源格式"
            )
    return text


def build_report(
    report_date: str,
    sections: Iterable[Section],
    stories_by_section: dict[str, list[Story]],
    writer: Callable[[Section, list[Story]], str],
) -> str:
    report_date = validate_date(report_date)
    lines = ["# 日报", "", report_date, ""]
    written = 0
    for section in sections:
        stories = stories_by_section.get(section.name) or []
        if not stories:
            continue
        text = writer(section, stories).strip()
        if not text:
            continue
        lines.extend([f"## {section.name}", "", text, ""])
        written += 1
    if not written:
        raise NewsError("没有可写成日报的新闻素材")
    return "\n".join(lines).rstrip() + "\n"


def write_report(report_date: str, text: str, news_dir: Path) -> Path:
    report_date = validate_date(report_date)
    news_dir.mkdir(parents=True, exist_ok=True)
    target = news_dir / f"{report_date}.md"
    fd, temporary = tempfile.mkstemp(prefix=f".{report_date}.", suffix=".tmp", dir=news_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        try:
            Path(temporary).unlink()
        except FileNotFoundError:
            pass
    return target


def _print_material(stories_by_section: dict[str, list[Story]]) -> None:
    for section in SECTIONS:
        print(f"## {section.name}")
        stories = stories_by_section.get(section.name) or []
        if not stories:
            print("（没有抓到素材）")
            continue
        for story in stories:
            print(f"- {story.title}｜{story.source}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="抓取素材并生成一份 Dwell 日报")
    parser.add_argument("--date", default=today_cn(), help="日报日期，默认中国时区今天")
    parser.add_argument("--news-dir", type=Path, help="输出目录，默认 DWELL_DATA_DIR/news")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--collect-only", action="store_true", help="只抓取并打印素材，不调用模型")
    parser.add_argument("--dry-run", action="store_true", help="调用模型但不写文件")
    args = parser.parse_args(argv)

    try:
        report_date = validate_date(args.date)
        stories, errors = collect_sections(timeout=max(1.0, args.timeout))
        if args.collect_only:
            _print_material(stories)
            if errors:
                print("\n抓取警告：", file=sys.stderr)
                for error in errors:
                    print("- " + error, file=sys.stderr)
            return 0 if any(stories.values()) else 2

        config = provider_config()
        provider = OpenAIProvider(config, timeout=max(30.0, args.timeout * 8))
        report = build_report(
            report_date,
            SECTIONS,
            stories,
            lambda section, items: write_section(provider, section, items),
        )
        if args.dry_run:
            print(report, end="")
        else:
            data_dir = Path(os.environ.get("DWELL_DATA_DIR", ROOT / "data")).resolve()
            target = write_report(report_date, report, args.news_dir or data_dir / "news")
            print(f"已生成 {target}")
        if errors:
            print(f"抓取警告：{len(errors)} 个来源失败", file=sys.stderr)
        return 0
    except NewsError as exc:
        print(f"日报未生成：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
