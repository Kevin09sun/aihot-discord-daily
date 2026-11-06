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


def fake_translator(texts):
    return [f"english {text}" for text in texts]


def no_sleep(_seconds):
    pass


class NormalizeTests(unittest.TestCase):
    def test_uses_original_link_only(self):
        item = a.normalize(report())["sections"][0]["items"][0]
        self.assertEqual(item["url"], "https://example.com/a")
        self.assertNotIn("aihot", item)

    def test_no_link_without_original(self):
        self.assertEqual(a.normalize_item(raw_item(1, original=None))["url"], "")

    def test_rejects_aihot_links(self):
        for url in ("https://aihot.news/items/1", "https://www.aihot.news/x", "https://aihot.virxact.com/x"):
            self.assertEqual(a.safe_url(url), "")
        self.assertEqual(a.safe_url("https://notaihot.news/x"), "https://notaihot.news/x")

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
        digest = a.translate(a.normalize(report()), fake_translator)
        item = digest["sections"][0]["items"][0]
        self.assertEqual(item["title"], "english 标题0")
        self.assertEqual(item["summary"], "english 摘要")
        self.assertEqual(item["source"], "english 来源")
        self.assertEqual(item["url"], "https://example.com/a")
        self.assertEqual(digest["lead"]["summary"], "english 导语")
        self.assertEqual(digest["flashes"][0]["title"], "english 标题f0")

    def test_does_not_change_the_input(self):
        digest = a.normalize(report())
        a.translate(digest, fake_translator)
        self.assertEqual(digest["lead"]["title"], "头条")

    def test_repeated_text_is_translated_once_and_english_is_skipped(self):
        seen = []
        def translator(texts):
            seen.extend(texts)
            return ["english"] * len(texts)
        raw = report(items=3, flashes=0, lead=False)
        raw["sections"][0]["items"][0]["title"] = "Already English"
        a.translate(a.normalize(raw), translator)
        self.assertEqual(seen.count("摘要"), 1)
        self.assertNotIn("Already English", seen)

    def test_known_label_is_kept_but_unknown_label_is_translated(self):
        digest = a.translate(a.normalize(report()), fake_translator)
        self.assertEqual(digest["sections"][0]["label"], "模型发布/更新")
        digest = a.translate(a.normalize(report(label="新版块")), fake_translator)
        self.assertEqual(digest["sections"][0]["label"], "english 新版块")

    def test_translation_failure_stops_the_run(self):
        def translator(_texts):
            raise a.FetchError("blocked")
        with self.assertRaises(a.FetchError):
            a.translate(a.normalize(report()), translator)

    def test_leftover_chinese_is_accepted(self):
        digest = a.translate(a.normalize(report(lead=False)), lambda texts: ["名字"] * len(texts))
        self.assertEqual(digest["sections"][0]["items"][0]["title"], "名字")

    def test_wrong_number_of_translations_stops_the_run(self):
        with self.assertRaises(a.FetchError):
            a.translate(a.normalize(report()), lambda _texts: ["only one"])

    def test_batches_stay_within_the_limit(self):
        batches = a.make_batches(["字" * 40] * 10, limit=100)
        self.assertEqual([len(b) for b in batches], [2, 2, 2, 2, 2])

    def test_report_uses_one_google_request_when_batched(self):
        calls = []
        def google(text):
            calls.append(text)
            return "\n".join(f"line {i}" for i in range(len(text.split("\n"))))
        result = a.translate_texts(["甲", "乙", "丙"], google, None, no_sleep)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result, ["line 0", "line 1", "line 2"])

    def test_falls_back_when_google_refuses(self):
        def google(_text):
            raise a.FetchError("HTTP 429")
        result = a.translate_texts(["甲", "乙"], google, lambda t: f"backup {t}", no_sleep)
        self.assertEqual(result, ["backup 甲", "backup 乙"])

    def test_falls_back_when_line_count_differs(self):
        result = a.translate_texts(["甲", "乙"], lambda _t: "one line", lambda t: f"backup {t}", no_sleep)
        self.assertEqual(result, ["backup 甲", "backup 乙"])

    def test_fails_when_both_translators_refuse(self):
        def refuse(_text):
            raise a.FetchError("refused")
        with self.assertRaises(a.FetchError):
            a.translate_texts(["甲"], refuse, refuse, no_sleep)

    def test_split_for_mymemory_respects_limit_and_keeps_text(self):
        text = "第一句话。" * 50 + "没" * 400
        parts = a.split_for_mymemory(text)
        self.assertTrue(all(len(p) <= a.MAX_MYMEMORY_CHARS for p in parts))
        self.assertTrue(all(p for p in parts))
        self.assertEqual("".join(parts), text)

    def test_parse_mymemory_response(self):
        ok = json.dumps({"responseData": {"translatedText": "Hello &amp; bye"}, "responseStatus": 200})
        self.assertEqual(a.parse_mymemory_response(ok), "Hello & bye")
        for bad in (
            "not json",
            json.dumps({"responseData": {"translatedText": "x"}, "responseStatus": 403}),
            json.dumps({"responseData": {"translatedText": "MYMEMORY WARNING: USED ALL"}, "responseStatus": 200}),
        ):
            with self.assertRaises(a.FetchError):
                a.parse_mymemory_response(bad)

    def test_shorten_source(self):
        cases = {
            "OpenAI：官网动态（RSS · 排除企业/客户案例）": "OpenAI",
            "Meta：Newsroom · AI（RSS）": "Meta",
            "Goodfire Research（网页）": "Goodfire Research",
            "Reflection Blog": "Reflection Blog",
            "X：Tibo (@thsottiaux)": "Tibo on X",
            "X：Mustafa Suleyman（Microsoft AI CEO） (@mustafasuleyman)": "Mustafa Suleyman on X",
            "Xcode：Blog": "Xcode",
        }
        for name, expected in cases.items():
            self.assertEqual(a.shorten_source(name), expected)

    def test_parse_google_response_joins_segments(self):
        raw = json.dumps([[["Hello. ", "你好。"], ["World", "世界"]], None, "zh-CN"])
        self.assertEqual(a.parse_google_response(raw), "Hello. World")

    def test_parse_google_response_rejects_bad_output(self):
        for raw in ("OK", "<html>blocked</html>", "[]", "[null]", json.dumps([[["", "你好"]]])):
            with self.assertRaises(a.FetchError):
                a.parse_google_response(raw)


class ImageTests(unittest.TestCase):
    def test_finds_og_image_in_either_attribute_order(self):
        for tag in (
            b'<meta property="og:image" content="https://cdn.example.com/a.png">',
            b"<meta content='https://cdn.example.com/a.png' property='og:image' />",
        ):
            self.assertEqual(
                a.parse_preview_image(tag, "https://example.com/post"),
                "https://cdn.example.com/a.png",
            )

    def test_falls_back_to_twitter_image_and_resolves_relative_urls(self):
        page = b'<meta name="twitter:image" content="/img/a.png?x=1&amp;y=2">'
        self.assertEqual(
            a.parse_preview_image(page, "https://example.com/post"),
            "https://example.com/img/a.png?x=1&y=2",
        )

    def test_page_without_image_returns_nothing(self):
        self.assertEqual(a.parse_preview_image(b"<html></html>", "https://example.com"), "")

    def test_rejects_unusable_images(self):
        for url in (
            "https://localhost/a.png",
            "http://example.com/a.png",
            "https://127.0.0.1/a.png",
            "https://pbs.twimg.com/profile_images/1/a_400x400.jpg",
            "https://aihot.news/a.png",
            "https://example.com/a b.png",
        ):
            self.assertFalse(a.usable_image(url), url)
        self.assertTrue(a.usable_image("https://pbs.twimg.com/media/abc?format=webp"))

    def test_network_failure_means_no_image(self):
        self.assertEqual(a.find_image("https://nonexistent.invalid/"), "")

    def test_attach_images_covers_stories_only(self):
        digest = a.normalize(report(items=2, flashes=1))
        count = a.attach_images(digest, lambda url: "https://cdn.example.com/i.png")
        self.assertEqual(count, 2)
        self.assertEqual(digest["sections"][0]["items"][0]["image"], "https://cdn.example.com/i.png")
        self.assertEqual(digest["flashes"][0]["image"], "")


class FormatTests(unittest.TestCase):
    def english(self, **kwargs):
        return a.translate(a.normalize(report(**kwargs)), fake_translator)

    def test_header_has_title_and_counts_and_no_link(self):
        header = a.build_embeds(self.english())[0]
        self.assertEqual(header["title"], "📰 Daily AI Report — November 6, 2026")
        self.assertIn("2 stories · 1 quick hit", header["description"])
        self.assertNotIn("url", header)

    def test_lead_links_to_matching_story_source(self):
        digest = self.english()
        digest["lead"]["title"] = digest["sections"][0]["items"][0]["title"]
        self.assertIn("(https://example.com/a)", a.build_embeds(digest)[0]["description"])

    def test_header_skips_lead_paragraph_repeated_by_a_story(self):
        digest = self.english()
        digest["lead"]["summary"] = "unique lead"
        self.assertIn("unique lead", a.build_embeds(digest)[0]["description"])
        digest["lead"]["summary"] = digest["sections"][0]["items"][0]["summary"]
        self.assertNotIn(digest["lead"]["summary"], a.build_embeds(digest)[0]["description"])

    def test_zero_width_characters_are_removed(self):
        self.assertEqual(a.clean("GPT-6 Cyber \u200b\u200bmodel"), "GPT-6 Cyber model")

    def test_story_card_links_to_source_and_shows_image(self):
        digest = self.english()
        digest["sections"][0]["items"][0]["image"] = "https://cdn.example.com/i.png"
        card = a.build_embeds(digest)[1]
        self.assertEqual(card["url"], "https://example.com/a")
        self.assertEqual(card["image"], {"url": "https://cdn.example.com/i.png"})
        self.assertIn("🔗 [english 来源](https://example.com/a)", card["description"])

    def test_section_name_appears_once_per_section(self):
        embeds = a.build_embeds(self.english())
        self.assertEqual(embeds[1]["author"], {"name": "🧠 Model Releases & Updates"})
        self.assertNotIn("author", embeds[2])

    def test_story_without_source_link_or_image(self):
        raw = report(items=1, flashes=0)
        del raw["sections"][0]["items"][0]["links"]["original"]
        card = a.build_embeds(a.translate(a.normalize(raw), fake_translator))[1]
        self.assertNotIn("url", card)
        self.assertNotIn("image", card)
        self.assertIn("*english 来源*", card["description"])

    def test_payload_never_links_to_aihot(self):
        digest = self.english(items=3, flashes=3)
        payload = json.dumps(a.build_messages(a.build_embeds(digest)), ensure_ascii=False)
        self.assertNotIn("aihot.news", payload)
        self.assertNotIn("[AIHOT]", payload)

    def test_last_embed_credits_source_and_discloses_translation(self):
        footer = a.build_embeds(self.english())[-1]["footer"]["text"]
        self.assertIn("AIHOT", footer)
        self.assertIn("Machine-translated", footer)

    def test_markdown_links_in_text_are_neutralized(self):
        item = {"title": "t", "summary": "[x](https://evil.example) **hi**", "source": "",
                "url": "https://example.com", "image": ""}
        text = a.story_embed(item, 0)["description"]
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
                item["title"] = "_" * 400
                item["summary"] = "word " * 200
        messages = a.build_messages(a.build_embeds(digest))
        self.assertGreater(len(messages), 1)
        for message in messages:
            self.assertLessEqual(len(message["embeds"]), 10)
            self.assertLessEqual(sum(a.embed_size(e) for e in message["embeds"]), 6000)
            for embed in message["embeds"]:
                self.assertLessEqual(len(embed.get("description", "")), 4096)
                self.assertLessEqual(len(embed["title"]), 256)
        titles = [e["title"] for m in messages for e in m["embeds"]]
        self.assertIn("⚡ Quick Hits (continued)", titles)


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
