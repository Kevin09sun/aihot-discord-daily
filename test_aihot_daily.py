import json
import tempfile
import unittest
from pathlib import Path

import aihot_daily as a


def raw_item(n, title="标题", original="https://example.com/a"):
    links = {"aihot": f"https://aihot.news/items/{n}"}
    if original:
        links["original"] = f"{original}{n}"
    return {"title": f"{title}{n}", "summary": "摘要", "source": {"name": "来源"}, "links": links}


def report(items=2, flashes=1, label="模型发布/更新", lead=True):
    return {
        "date": "2026-11-06",
        "links": {"aihot": "https://aihot.news/daily/2026-11-06"},
        "lead": {"title": "标题0", "leadParagraph": "导语"} if lead else None,
        "sections": [{"label": label, "items": [raw_item(i) for i in range(items)]}],
        "flashes": [raw_item(f"f{i}") for i in range(flashes)],
    }


def fake_translator(texts):
    return [f"english {text}" for text in texts]


def no_sleep(_seconds):
    pass


def english(**kwargs):
    return a.translate(a.normalize(report(**kwargs)), fake_translator)


class NormalizeTests(unittest.TestCase):
    def test_uses_original_link_only(self):
        item = a.normalize(report())["sections"][0]["items"][0]
        self.assertEqual(item, {"title": "标题0", "url": "https://example.com/a0"})

    def test_no_link_without_original(self):
        self.assertEqual(a.normalize_item(raw_item(1, original=None))["url"], "")

    def test_rejects_aihot_and_non_https_links(self):
        for url in (
            "https://aihot.news/items/1",
            "https://www.aihot.news/x",
            "https://aihot.virxact.com/x",
            "javascript:alert(1)",
            "http://example.com",
        ):
            self.assertEqual(a.safe_url(url), "")
        self.assertEqual(a.safe_url("https://notaihot.news/x"), "https://notaihot.news/x")

    def test_quick_hits_are_capped(self):
        self.assertEqual(len(a.normalize(report(flashes=9))["flashes"]), a.MAX_QUICK_HITS)

    def test_null_lead_is_allowed(self):
        self.assertIsNone(a.normalize(report(lead=False))["lead"])

    def test_empty_report_raises(self):
        with self.assertRaises(a.FetchError):
            a.normalize(report(items=0, flashes=0))


class TranslateTests(unittest.TestCase):
    def test_translates_used_text_and_leaves_links_alone(self):
        digest = english()
        item = digest["sections"][0]["items"][0]
        self.assertEqual(item["title"], "english 标题0")
        self.assertEqual(item["url"], "https://example.com/a0")
        self.assertEqual(digest["lead"]["summary"], "english 导语")
        self.assertEqual(digest["flashes"][0]["title"], "english 标题f0")

    def test_does_not_change_the_input(self):
        digest = a.normalize(report())
        a.translate(digest, fake_translator)
        self.assertEqual(digest["lead"]["title"], "标题0")

    def test_repeated_text_is_sent_once_and_unused_text_is_not_sent(self):
        seen = []
        def translator(texts):
            seen.extend(texts)
            return ["english"] * len(texts)
        raw = report(items=3, flashes=0)
        raw["sections"][0]["items"][1]["title"] = "Already English"
        a.translate(a.normalize(raw), translator)
        self.assertEqual(seen.count("标题0"), 1)
        self.assertNotIn("Already English", seen)
        self.assertNotIn("摘要", seen)
        self.assertNotIn("来源", seen)

    def test_known_label_is_kept_but_unknown_label_is_translated(self):
        self.assertEqual(english()["sections"][0]["label"], "模型发布/更新")
        self.assertEqual(english(label="新版块")["sections"][0]["label"], "english 新版块")

    def test_translation_failure_stops_the_run(self):
        def translator(_texts):
            raise a.FetchError("blocked")
        with self.assertRaises(a.FetchError):
            a.translate(a.normalize(report()), translator)

    def test_wrong_number_of_translations_stops_the_run(self):
        with self.assertRaises(a.FetchError):
            a.translate(a.normalize(report()), lambda _texts: ["only one"])

    def test_leftover_chinese_is_accepted(self):
        digest = a.translate(a.normalize(report(lead=False)), lambda texts: ["名字"] * len(texts))
        self.assertEqual(digest["sections"][0]["items"][0]["title"], "名字")

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
        self.assertTrue(all(0 < len(p) <= a.MAX_MYMEMORY_CHARS for p in parts))
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

    def test_rejects_logos(self):
        self.assertFalse(a.usable_image("https://metr.org/assets/images/logo/og-image-logo.png"))

    def test_top_story_image_comes_first(self):
        raw = report(items=3, flashes=0)
        raw["lead"]["title"] = "标题2"
        digest = a.normalize(raw)
        count = a.attach_gallery(digest, lambda url: f"https://cdn.example.com/{url[-2:]}.png")
        self.assertEqual(count, 3)
        self.assertEqual([g["title"] for g in digest["gallery"]], ["标题2", "标题0", "标题1"])

    def test_quick_hits_fill_the_gallery_after_stories(self):
        digest = a.normalize(report(items=1, flashes=5))
        a.attach_gallery(digest, lambda url: f"https://cdn.example.com/{url[-3:]}.png")
        self.assertEqual(len(digest["gallery"]), a.MAX_GALLERY_IMAGES)
        self.assertEqual(digest["gallery"][1]["title"], "标题f0")

    def test_stories_without_images_are_skipped(self):
        digest = a.normalize(report(items=3))
        a.attach_gallery(
            digest, lambda url: "https://cdn.example.com/i.png" if url.endswith("a1") else ""
        )
        self.assertEqual([g["title"] for g in digest["gallery"]], ["标题1"])

    def test_duplicate_images_are_skipped(self):
        digest = a.normalize(report(items=3))
        a.attach_gallery(digest, lambda _url: "https://cdn.example.com/same.png")
        self.assertEqual(len(digest["gallery"]), 1)

    def test_no_images_anywhere(self):
        digest = a.normalize(report(items=2))
        self.assertEqual(a.attach_gallery(digest, lambda _url: ""), 0)
        self.assertEqual(digest["gallery"], [])

    def test_tries_are_limited(self):
        tried = []
        def finder(url):
            tried.append(url)
            return ""
        a.attach_gallery(a.normalize(report(items=20)), finder)
        self.assertEqual(len(tried), a.MAX_IMAGE_TRIES)


class FormatTests(unittest.TestCase):
    def test_short_headline_keeps_main_clause(self):
        self.assertEqual(
            a.short_headline("Thinking Machines Lab releases Inkling 2 Small: 594B parameter MoE model, with weights next week"),
            "Thinking Machines Lab releases Inkling 2 Small",
        )

    def test_short_headline_keeps_short_or_leading_colon_titles(self):
        short = "OpenAI releases GPT-6 Cyber network security model"
        self.assertEqual(a.short_headline(short), short)
        title = "a16z: Vulnerability exploitation used to be a bottleneck, but AI has reduced attack costs"
        self.assertEqual(a.short_headline(title), "a16z: Vulnerability exploitation used to be a bottleneck")

    def test_short_headline_has_a_length_limit(self):
        self.assertLessEqual(len(a.short_headline("word " * 100)), a.MAX_HEADLINE_CHARS)

    def test_first_sentence(self):
        self.assertEqual(a.first_sentence("Stocks fell. Then they rose."), "Stocks fell.")
        self.assertEqual(a.first_sentence("The U.S. market fell 5%. More."), "The U.S. market fell 5%.")
        self.assertEqual(a.first_sentence("No full stop here"), "No full stop here")
        self.assertLessEqual(len(a.first_sentence("word " * 200)), a.MAX_SUMMARY_CHARS)

    def test_header_shows_top_story_with_link_and_one_sentence(self):
        digest = english()
        digest["lead"]["summary"] = "First sentence. Second sentence."
        header = a.build_embeds(digest)[0]
        self.assertEqual(header["title"], "📰 Daily AI Report — November 6, 2026")
        self.assertIn("[english 标题0](https://example.com/a0)", header["description"])
        self.assertIn("First sentence.", header["description"])
        self.assertNotIn("Second sentence.", header["description"])
        self.assertNotIn("url", header)

    def gallery_digest(self, count):
        digest = english(items=4, flashes=1)
        digest["gallery"] = [
            {**item, "image": f"https://cdn.example.com/{n}.png"}
            for n, item in enumerate(digest["sections"][0]["items"][:count])
        ]
        return digest

    def test_gallery_is_one_card_with_shared_url_and_captions(self):
        embeds = a.build_embeds(self.gallery_digest(3))
        card, second, third = embeds[1], embeds[2], embeds[3]
        self.assertEqual(card["title"], "📸 In pictures")
        self.assertEqual(card["image"], {"url": "https://cdn.example.com/0.png"})
        self.assertIn("1. [english 标题0](https://example.com/a0)", card["description"])
        self.assertIn("3. [english 标题2](https://example.com/a2)", card["description"])
        self.assertEqual(second, {"url": card["url"], "image": {"url": "https://cdn.example.com/1.png"}})
        self.assertEqual(third["image"], {"url": "https://cdn.example.com/2.png"})
        self.assertEqual(embeds[4]["title"], "🗞️ Headlines")
        self.assertNotIn("image", embeds[0])

    def test_no_gallery_without_images(self):
        embeds = a.build_embeds(english())
        self.assertNotIn("📸 In pictures", [e.get("title") for e in embeds])

    def test_headlines_still_list_pictured_stories(self):
        embeds = a.build_embeds(self.gallery_digest(2))
        self.assertIn("english 标题1", embeds[-1]["description"])

    def test_headlines_are_grouped_and_linked(self):
        text = a.build_embeds(english(items=2, flashes=1))[1]["description"]
        self.assertIn("**🧠 Models**", text)
        self.assertIn("• [english 标题1](https://example.com/a1)", text)
        self.assertIn("**⚡ Also**", text)
        self.assertIn("• [english 标题f0](https://example.com/af0)", text)

    def test_headline_without_source_has_no_link(self):
        self.assertEqual(a.headline_line({"title": "Plain", "url": ""}), "• Plain")

    def test_section_emptied_by_top_story_is_dropped(self):
        embeds = a.build_embeds(english(items=1, flashes=1))
        self.assertNotIn("Models", embeds[1]["description"])

    def test_report_with_only_a_top_story_still_has_a_footer(self):
        embeds = a.build_embeds(english(items=1, flashes=0))
        self.assertEqual(len(embeds), 1)
        self.assertIn("footer", embeds[0])

    def test_report_without_lead(self):
        embeds = a.build_embeds(english(lead=False))
        self.assertNotIn("description", embeds[0])
        self.assertIn("english 标题0", embeds[1]["description"])

    def test_payload_never_links_to_aihot(self):
        digest = english(items=3, flashes=3)
        payload = json.dumps(a.build_messages(a.build_embeds(digest)), ensure_ascii=False)
        self.assertNotIn("aihot.news", payload)
        self.assertNotIn("[AIHOT]", payload)

    def test_last_embed_credits_source_and_discloses_translation(self):
        footer = a.build_embeds(english())[-1]["footer"]["text"]
        self.assertIn("AIHOT", footer)
        self.assertIn("Machine-translated", footer)

    def test_markdown_links_in_text_are_neutralized(self):
        line = a.headline_line({"title": "[x](https://evil.example) **hi**", "url": "https://example.com"})
        self.assertNotIn("[x](", line)
        self.assertIn("\\[x\\]", line)
        self.assertIn("\\*\\*hi\\*\\*", line)

    def test_zero_width_characters_are_removed(self):
        self.assertEqual(a.clean("GPT-6 Cyber \u200b\u200bmodel"), "GPT-6 Cyber model")

    def test_messages_disable_all_mentions(self):
        for message in a.build_messages(a.build_embeds(english())):
            self.assertEqual(message["allowed_mentions"], {"parse": []})

    def test_typical_report_fits_one_message(self):
        digest = english(items=12, flashes=10)
        digest["gallery"] = [
            {**item, "image": f"https://cdn.example.com/{n}.png"}
            for n, item in enumerate(digest["sections"][0]["items"][:4])
        ]
        self.assertEqual(len(a.build_messages(a.build_embeds(digest))), 1)

    def test_huge_report_respects_discord_limits(self):
        digest = english(items=400, flashes=5)
        messages = a.build_messages(a.build_embeds(digest))
        self.assertGreater(len(messages), 1)
        for message in messages:
            self.assertLessEqual(len(message["embeds"]), 10)
            self.assertLessEqual(sum(a.embed_size(e) for e in message["embeds"]), 6000)
            for embed in message["embeds"]:
                self.assertLessEqual(len(embed.get("description", "")), 4096)
                self.assertLessEqual(len(embed["title"]), 256)


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
