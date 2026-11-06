import json
import tempfile
import unittest
from pathlib import Path

import aihot_daily as a


def raw_item(n, title="标题", summary="摘要", original="https://example.com/a"):
    links = {"aihot": f"https://aihot.news/items/{n}"}
    if original:
        links["original"] = original
    return {"title": f"{title}{n}", "summary": summary, "source": {"name": "来源"}, "links": links}


def report(items=2, flashes=1, label="模型发布/更新", lead=True):
    return {
        "date": "2026-11-06",
        "links": {"aihot": "https://aihot.news/daily/2026-11-06"},
        "lead": {"title": "头条", "leadParagraph": "导语"} if lead else None,
        "sections": [{"label": label, "items": [raw_item(i) for i in range(items)]}],
        "flashes": [raw_item(f"f{i}") for i in range(flashes)],
    }


def fake_complete(inputs):
    """Stand-in model: replaces every value with English text."""
    return json.dumps({"items": [
        {k: (v if k == "id" else f"english {k}") for k, v in item.items()} for item in inputs
    ]})


def no_sleep(_seconds):
    pass


class NormalizeTests(unittest.TestCase):
    def test_prefers_original_link_and_keeps_aihot_link(self):
        item = a.normalize(report())["sections"][0]["items"][0]
        self.assertEqual(item["url"], "https://example.com/a")
        self.assertEqual(item["aihot"], "https://aihot.news/items/0")

    def test_falls_back_to_aihot_link_without_original(self):
        item = a.normalize_item(raw_item(1, original=None))
        self.assertEqual(item["url"], "https://aihot.news/items/1")
        self.assertEqual(item["aihot"], "")

    def test_rejects_non_https_links(self):
        self.assertEqual(a.safe_url("javascript:alert(1)"), "")
        self.assertEqual(a.safe_url("http://example.com"), "")

    def test_null_lead_is_allowed(self):
        self.assertIsNone(a.normalize(report(lead=False))["lead"])

    def test_empty_report_raises(self):
        with self.assertRaises(a.FetchError):
            a.normalize(report(items=0, flashes=0))


class TranslateTests(unittest.TestCase):
    def test_translates_all_text_and_leaves_links_alone(self):
        digest = a.translate(a.normalize(report()), fake_complete, no_sleep)
        item = digest["sections"][0]["items"][0]
        self.assertEqual(item["title"], "english title")
        self.assertEqual(item["source"], "english source")
        self.assertEqual(item["url"], "https://example.com/a")
        self.assertEqual(digest["lead"]["summary"], "english summary")
        self.assertEqual(digest["flashes"][0]["title"], "english title")

    def test_known_label_is_not_sent_but_unknown_label_is(self):
        seen = []
        def complete(inputs):
            seen.extend(i["id"] for i in inputs)
            return fake_complete(inputs)
        a.translate(a.normalize(report()), complete, no_sleep)
        self.assertNotIn("label0", seen)
        digest = a.translate(a.normalize(report(label="新版块")), complete, no_sleep)
        self.assertIn("label0", seen)
        self.assertEqual(digest["sections"][0]["label"], "english title")

    def test_splits_requests_into_chunks(self):
        sizes = []
        def complete(inputs):
            sizes.append(len(inputs))
            return fake_complete(inputs)
        a.translate(a.normalize(report(items=25, flashes=0)), complete, no_sleep)
        self.assertEqual(sum(sizes), 26)
        self.assertLessEqual(max(sizes), a.TRANSLATION_CHUNK)

    def test_retries_bad_output_then_succeeds(self):
        replies = iter(["not json", json.dumps({"items": []})])
        def complete(inputs):
            return next(replies, None) or fake_complete(inputs)
        digest = a.translate(a.normalize(report(items=1, flashes=0, lead=False)), complete, no_sleep)
        self.assertEqual(digest["sections"][0]["items"][0]["title"], "english title")

    def test_raises_when_output_stays_unusable(self):
        with self.assertRaises(a.FetchError):
            a.translate(a.normalize(report()), lambda _: "nope", no_sleep)

    def test_raises_when_a_field_is_dropped(self):
        def complete(inputs):
            return json.dumps({"items": [{"id": i["id"], "title": "x"} for i in inputs]})
        with self.assertRaises(a.FetchError):
            a.translate(a.normalize(report(lead=False)), complete, no_sleep)

    def test_accepts_leftover_chinese_after_retrying(self):
        calls = []
        def complete(inputs):
            calls.append(1)
            return json.dumps({"items": [
                {k: (v if k == "id" else "名字") for k, v in i.items()} for i in inputs
            ]})
        digest = a.translate(a.normalize(report(items=1, flashes=0, lead=False)), complete, no_sleep)
        self.assertEqual(len(calls), a.TRANSLATION_ATTEMPTS)
        self.assertEqual(digest["sections"][0]["items"][0]["title"], "名字")

    def test_fenced_json_is_accepted(self):
        inputs = [{"id": "x", "title": "t"}]
        text = '```json\n{"items":[{"id":"x","title":"Hello"}]}\n```'
        self.assertEqual(a.parse_translation(text, inputs), {"x": {"title": "Hello"}})


class FormatTests(unittest.TestCase):
    def english(self, **kwargs):
        return a.translate(a.normalize(report(**kwargs)), fake_complete, no_sleep)

    def test_header_and_english_section_title(self):
        embeds = a.build_embeds(self.english())
        self.assertEqual(embeds[0]["title"], "📰 Daily AI Report — November 6, 2026")
        self.assertEqual(embeds[0]["url"], "https://aihot.news/daily/2026-11-06")
        self.assertIn("2 stories · 1 quick hit", embeds[0]["description"])
        self.assertEqual(embeds[1]["title"], "🧠 Model Releases & Updates")

    def test_item_keeps_source_and_both_links(self):
        text = a.build_embeds(self.english())[1]["description"]
        self.assertIn("(https://example.com/a)", text)
        self.assertIn("[AIHOT](https://aihot.news/items/0)", text)
        self.assertIn("*english source*", text)

    def test_last_embed_credits_source_and_discloses_translation(self):
        footer = a.build_embeds(self.english())[-1]["footer"]["text"]
        self.assertIn("AIHOT", footer)
        self.assertIn("Machine-translated", footer)

    def test_markdown_links_in_text_are_neutralized(self):
        item = {"title": "[x](https://evil.example)", "summary": "**hi**", "source": "",
                "url": "https://example.com", "aihot": ""}
        text = a.format_item(item)
        self.assertNotIn("[x](", text)
        self.assertIn("\\[x\\]", text)
        self.assertIn("\\*\\*hi\\*\\*", text)

    def test_messages_disable_all_mentions(self):
        for message in a.build_messages(a.build_embeds(self.english())):
            self.assertEqual(message["allowed_mentions"], {"parse": []})

    def test_large_report_respects_discord_limits(self):
        digest = self.english(items=120, flashes=120)
        for section in digest["sections"]:
            for item in section["items"]:
                item["summary"] = "word " * 200
        messages = a.build_messages(a.build_embeds(digest))
        self.assertGreater(len(messages), 1)
        for message in messages:
            self.assertLessEqual(len(message["embeds"]), 10)
            self.assertLessEqual(sum(a.embed_size(e) for e in message["embeds"]), 6000)
            for embed in message["embeds"]:
                self.assertLessEqual(len(embed["description"]), 4096)
                self.assertLessEqual(len(embed["title"]), 256)
        titles = [e["title"] for m in messages for e in m["embeds"]]
        self.assertIn("🧠 Model Releases & Updates (continued)", titles)


class StateTests(unittest.TestCase):
    def test_dedupe_rule(self):
        self.assertFalse(a.already_posted("2026-11-06", ""))
        self.assertFalse(a.already_posted("2026-11-06", "2026-11-05"))
        self.assertTrue(a.already_posted("2026-11-06", "2026-11-06"))
        self.assertTrue(a.already_posted("2026-11-05", "2026-11-06"))

    def test_state_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state" / "last.txt"
            self.assertEqual(a.read_last_posted(path), "")
            a.write_last_posted("2026-11-06", path)
            self.assertEqual(a.read_last_posted(path), "2026-11-06")


if __name__ == "__main__":
    unittest.main()
