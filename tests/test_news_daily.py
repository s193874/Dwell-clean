import tempfile
import unittest
from pathlib import Path

from tools.news_daily import (
    NewsError,
    SECTIONS,
    Section,
    Story,
    build_report,
    collect_sections,
    parse_feed,
    write_report,
    write_section,
)


class NewsDailyTest(unittest.TestCase):
    def test_ai_product_section_covers_chatgpt_and_other_models(self):
        section = next(item for item in SECTIONS if item.name == "ChatGPT 与其他 AI")
        for term in ("ChatGPT", "DeepSeek", "Kimi", "GLM", "智谱"):
            self.assertIn(term, section.query)
        self.assertNotIn("关于 Claude", {item.name for item in SECTIONS})

    def test_daily_report_focuses_story_counts_on_ai_products(self):
        counts = {item.name: item.story_count for item in SECTIONS}
        self.assertEqual(
            counts,
            {
                "科技与 AI": 2,
                "ChatGPT 与其他 AI": 10,
                "社会": 2,
                "娱乐": 2,
            },
        )

    def test_inline_source_is_normalized_without_model_retry(self):
        class RepairingProvider:
            def __init__(self):
                self.calls = 0

            def complete(self, messages, tools=None, max_tokens=None):
                self.calls += 1
                return {
                    "content": "### 标题\n摘要写在段落里。"
                    "来源：[示例站](https://example.test/story)"
                }

        provider = RepairingProvider()
        section = Section("娱乐", "娱乐", "轻一点", ("unused",))
        text = write_section(
            provider,
            section,
            [Story("标题", "摘要", "https://example.test/story", "示例站")],
        )
        self.assertEqual(provider.calls, 1)
        self.assertEqual(
            text,
            "### 标题\n- 摘要写在段落里。｜来源：[示例站](https://example.test/story)",
        )

    def test_ascii_separator_is_normalized(self):
        class Provider:
            def complete(self, messages, tools=None, max_tokens=None):
                return {
                    "content": "### 标题\n* 摘要 | [example.test](https://example.test/story)"
                }

        text = write_section(
            Provider(),
            Section("科技与 AI", "技术", "清楚", ("unused",)),
            [Story("标题", "摘要", "https://example.test/story", "example.test")],
        )
        self.assertEqual(
            text,
            "### 标题\n- 摘要｜来源：[example.test](https://example.test/story)",
        )

    def test_malformed_section_is_not_published_after_failed_repair(self):
        class BrokenProvider:
            def complete(self, messages, tools=None, max_tokens=None):
                return {"content": "### 标题\n来源仍然写在段落里"}

        with self.assertRaises(NewsError):
            write_section(
                BrokenProvider(),
                Section("娱乐", "娱乐", "轻一点", ("unused",)),
                [Story("标题", "摘要", "https://example.test/story", "示例站")],
            )

    def test_multiple_source_links_are_accepted_with_explicit_separator(self):
        class Provider:
            def complete(self, messages, tools=None, max_tokens=None):
                return {
                    "content": "### 标题\n- 摘要｜来源："
                    "[甲站](https://one.example/story)、[乙站](https://two.example/story)"
                }

        text = write_section(
            Provider(),
            Section("娱乐", "娱乐", "轻一点", ("unused",)),
            [Story("标题", "摘要", "https://one.example/story", "甲站")],
        )
        self.assertIn("、[乙站](https://two.example/story)", text)

    def test_step_reasoning_models_are_not_cut_off_before_final_content(self):
        class RecordingProvider:
            def __init__(self):
                self.max_tokens = "not-called"

            def complete(self, messages, tools=None, max_tokens=None):
                self.max_tokens = max_tokens
                return {
                    "content": "### 一条稿件\n\n"
                    "- 摘要｜来源：[example.test](https://example.test/story)"
                }

        provider = RecordingProvider()
        section = Section("科技与 AI", "技术", "清楚", ("unused",))
        text = write_section(
            provider,
            section,
            [Story("标题", "摘要", "https://example.test/story", "example.test")],
        )
        self.assertIn("一条稿件", text)
        self.assertIsNone(provider.max_tokens)

    def test_parse_rss_and_strip_markup(self):
        payload = """<?xml version="1.0"?><rss><channel>
          <item><title>  一条新闻  </title><description>&lt;p&gt;摘要&lt;/p&gt;&lt;br&gt;第二句</description>
          <link>https://example.test/story</link></item>
        </channel></rss>""".encode()
        stories = parse_feed(payload, "example.test")
        self.assertEqual(stories, [Story("一条新闻", "摘要 第二句", "https://example.test/story", "example.test")])

    def test_collect_sections_keeps_other_sources_when_one_fails(self):
        section = Section("测试", "测试", "简洁", ("good", "bad"))

        def fetcher(url, timeout):
            if url == "bad":
                from tools.news_daily import NewsError

                raise NewsError("down")
            return [Story("同一条", "摘要", "https://example.test/a", "example.test")]

        sections, errors = collect_sections((section,), fetcher)
        self.assertEqual(len(sections["测试"]), 1)
        self.assertEqual(len(errors), 1)

    def test_build_and_atomically_write_dated_report(self):
        section = Section("科技与 AI", "技术", "清楚", ("unused",))
        stories = {"科技与 AI": [Story("标题", "摘要", "链接", "来源")]}
        report = build_report(
            "2026-08-22",
            (section,),
            stories,
            lambda current, items: "### 标题\n\n- 摘要｜来源",
        )
        self.assertIn("## 科技与 AI", report)
        with tempfile.TemporaryDirectory() as directory:
            target = write_report("2026-08-22", report, Path(directory))
            self.assertEqual(target.name, "2026-08-22.md")
            self.assertEqual(target.read_text(encoding="utf-8"), report)

    def test_invalid_date_is_rejected(self):
        section = Section("测试", "测试", "简洁", ())
        with self.assertRaises(Exception):
            build_report("2026-02-30", (section,), {}, lambda current, items: "稿件")


if __name__ == "__main__":
    unittest.main()
