#!/usr/bin/env python3
"""Post a concise English digest of the latest AIHOT daily report to Discord.

Uses only the Python standard library. Translation uses Google Translate's free
web endpoint, which needs no key but is unofficial and could change, and falls
back to the free MyMemory API if Google refuses.

Environment:
  DISCORD_WEBHOOK_URL  target webhook (not needed with --dry-run)
"""
import argparse
import copy
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPORT_URL = "https://aihot.news/api/v1/dailies/latest"
TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single?client=gtx&sl=zh-CN&tl=en&dt=t"
MYMEMORY_URL = "https://api.mymemory.translated.net/get"
USER_AGENT = "aihot-discord-daily/1.0 (private, non-commercial)"
STATE_FILE = Path(__file__).parent / "state" / "last_posted.txt"

# Discord allows 4096 per description, 6000 per message and 10 embeds per
# message. Stay a little under the character limits.
MAX_DESCRIPTION = 4000
MAX_MESSAGE_CHARS = 5800
MAX_EMBEDS = 10
MAX_HEADLINE_CHARS = 110
MAX_SUMMARY_CHARS = 220
MAX_QUICK_HITS = 5
MAX_URL_CHARS = 500
MAX_TRANSLATION_INPUT = 2000
MAX_BATCH_CHARS = 4000
MAX_MYMEMORY_CHARS = 150  # MyMemory allows 500 bytes per request
TRANSLATION_PAUSE_SECONDS = 1

EMBED_COLOR = 0x5865F2
SECTION_STYLES = {
    "模型发布/更新": ("🧠", "Models"),
    "产品发布/更新": ("🚀", "Products"),
    "行业动态": ("🏢", "Industry"),
    "论文研究": ("📄", "Research"),
    "技巧与观点": ("💡", "Tips & Opinions"),
}
DEFAULT_STYLE = ("📌", None)
AIHOT_HOSTS = ("aihot.news", "aihot.virxact.com")

CJK =re.compile(r"[㐀-鿿豈-﫿]")
MARKDOWN = re.compile(r"([\\`*_~|\[\]<>#])")


class FetchError(RuntimeError):
    pass


def request(label, url, body=None, form=None, attempts=4, sleep=time.sleep):
    """Send a request, retrying rate limits, server errors and network failures.

    Errors name `label` rather than `url`, which may be the secret webhook.
    """
    data = None
    all_headers = {"User-Agent": USER_AGENT}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        all_headers["Content-Type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        all_headers["Content-Type"] = "application/x-www-form-urlencoded"
    problem = "no attempts made"
    for attempt in range(attempts):
        req = urllib.request.Request(url, data=data, headers=all_headers)
        delay = 2 ** (attempt + 1)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.read()
        except urllib.error.HTTPError as err:
            detail = err.read()[:300].decode("utf-8", "replace")
            problem = f"HTTP {err.code}: {detail}"
            if err.code != 429 and err.code < 500:
                raise FetchError(f"{label} failed with {problem}") from None
            try:
                delay = max(delay, min(float(err.headers.get("Retry-After", 0)), 120))
            except ValueError:
                pass
        except (urllib.error.URLError, TimeoutError) as err:
            problem = f"network error: {getattr(err, 'reason', err)}"
        if attempt < attempts - 1:
            sleep(delay)
    raise FetchError(f"{label} failed after {attempts} attempts, last {problem}")


def fetch_report():
    return json.loads(request("AIHOT report", REPORT_URL))["report"]


def clean(text, limit=None):
    text = re.sub(r"[\u200b-\u200d\ufeff]", "", str(text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    if limit and len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def is_aihot_host(host):
    return any(host == h or host.endswith("." + h) for h in AIHOT_HOSTS)


def safe_url(url):
    """Return an https link to a story's source, or "" if it can't be used."""
    url = str(url or "").strip()
    if not url.startswith("https://") or len(url) > MAX_URL_CHARS or re.search(r"\s", url):
        return ""
    if is_aihot_host((urllib.parse.urlsplit(url).hostname or "").lower()):
        return ""
    return url.replace("(", "%28").replace(")", "%29")


def normalize_item(raw):
    return {
        "title": clean(raw.get("title")),
        "url": safe_url((raw.get("links") or {}).get("original")),
    }


def normalize(report):
    """Reduce an API report to the fields used here."""
    lead = report.get("lead") or None
    sections = []
    for section in report.get("sections") or []:
        items = [normalize_item(i) for i in section.get("items") or [] if i.get("title")]
        if items:
            sections.append({"label": clean(section.get("label")), "items": items})
    flashes = [normalize_item(i) for i in report.get("flashes") or [] if i.get("title")]
    flashes = flashes[:MAX_QUICK_HITS]
    if not sections and not flashes:
        raise FetchError(f"AIHOT report {report.get('date')} has no items")
    return {
        "date": report["date"],
        "lead": lead and {
            "title": clean(lead.get("title")),
            "summary": clean(lead.get("leadParagraph")),
        },
        "sections": sections,
        "flashes": flashes,
    }


def parse_google_response(raw):
    """Join the translated segments of a Google Translate response."""
    try:
        text = "".join(segment[0] for segment in json.loads(raw)[0] if segment[0])
    except (ValueError, TypeError, IndexError, KeyError) as err:
        raise FetchError(f"unexpected translation response: {err}") from None
    if not text.strip():
        raise FetchError("translation response was empty")
    return text


def make_batches(texts, limit=MAX_BATCH_CHARS):
    """Group texts so one request can carry many of them."""
    batches, current, size = [], [], 0
    for text in texts:
        if current and size + len(text) + 1 > limit:
            batches.append(current)
            current, size = [], 0
        current.append(text)
        size += len(text) + 1
    if current:
        batches.append(current)
    return batches


def google_translate(text):
    raw = request("Google Translate", TRANSLATE_URL, form={"q": text}, attempts=3)
    return parse_google_response(raw)


def split_for_mymemory(text, limit=MAX_MYMEMORY_CHARS):
    """Split long text at sentence ends, or mid-sentence if there are none nearby."""
    parts, current = [], ""
    for sentence in re.findall(r"[^。！？；]+[。！？；]*", text):
        while len(sentence) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(sentence[:limit])
            sentence = sentence[limit:]
        if len(current) + len(sentence) > limit:
            parts.append(current)
            current = ""
        current += sentence
    if current:
        parts.append(current)
    return parts


def parse_mymemory_response(raw):
    try:
        data = json.loads(raw)
        text = data["responseData"]["translatedText"]
        status = str(data.get("responseStatus"))
    except (ValueError, TypeError, KeyError) as err:
        raise FetchError(f"unexpected MyMemory response: {err}") from None
    if status != "200" or not text or not text.strip() or "MYMEMORY WARNING" in text:
        raise FetchError(f"MyMemory refused the request: {status} {str(text)[:120]}")
    return html.unescape(text)


def mymemory_translate(text, sleep=time.sleep):
    """Translate with MyMemory, whose free tier allows about 5,000 characters a day."""
    translated = []
    for part in split_for_mymemory(text):
        query = urllib.parse.urlencode({"q": part, "langpair": "zh-CN|en"})
        raw = request("MyMemory", f"{MYMEMORY_URL}?{query}", attempts=2)
        translated.append(clean(parse_mymemory_response(raw)))
        sleep(TRANSLATION_PAUSE_SECONDS)
    return " ".join(translated)


def translate_texts(texts, google=google_translate, fallback=mymemory_translate,
                    sleep=time.sleep):
    """Translate a list of texts, in order, in as few requests as possible.

    Each text is one line of a batch. If Google returns a different number of
    lines, or refuses, the affected texts go through the fallback one at a time.
    """
    results = {}
    inputs = [text[:MAX_TRANSLATION_INPUT] for text in texts]
    try:
        for batch in make_batches(list(dict.fromkeys(inputs))):
            lines = google("\n".join(batch)).strip().split("\n")
            if len(lines) == len(batch):
                results.update(zip(batch, lines))
            else:
                print(f"note: batch returned {len(lines)} of {len(batch)} lines", file=sys.stderr)
            sleep(TRANSLATION_PAUSE_SECONDS)
    except FetchError as err:
        print(f"note: Google Translate unavailable, using MyMemory: {err}", file=sys.stderr)
    for text in dict.fromkeys(inputs):
        if text not in results:
            results[text] = fallback(text)
    return [results[text] for text in inputs]


def translate(digest, translator=translate_texts):
    """Return an English copy of `digest`, using `translator(texts) -> english texts`.

    Text without Chinese is left as is, and repeated text is translated once.
    """
    digest = copy.deepcopy(digest)
    fields = []
    if digest["lead"]:
        fields += [(digest["lead"], "title"), (digest["lead"], "summary")]
    items = list(digest["flashes"])
    for section in digest["sections"]:
        if section["label"] not in SECTION_STYLES:
            fields.append((section, "label"))
        items.extend(section["items"])
    fields += [(item, "title") for item in items]

    wanted = list(dict.fromkeys(
        clean(entry[key]) for entry, key in fields if CJK.search(clean(entry[key]))
    ))
    translated = translator(wanted) if wanted else []
    if len(translated) != len(wanted):
        raise FetchError(f"translated {len(translated)} of {len(wanted)} texts")
    english = {text: clean(result) for text, result in zip(wanted, translated)}
    for text, result in english.items():
        if CJK.search(result):
            print(f"warning: Chinese left after translating: {text[:60]}", file=sys.stderr)
    for entry, key in fields:
        text = clean(entry[key])
        entry[key] = english.get(text, text)
    return digest


def md(text, limit=None):
    """Escape Discord markdown in third-party text so it stays plain text."""
    return MARKDOWN.sub(r"\\\1", clean(text, limit))


def short_headline(title):
    """Keep a long headline's main clause, cutting at its first ": " or ", "."""
    title = clean(title)
    if len(title) > 70:
        match = re.search(r": |, ", title[25:])
        if match:
            title = title[: 25 + match.start()]
    return clean(title, MAX_HEADLINE_CHARS)


def first_sentence(text):
    """Return text's first sentence, without stopping at abbreviations like U.S."""
    match = re.match(r".+?[a-z0-9)\"'%][.!?](?=\s|$)", clean(text))
    return clean(match.group(0) if match else text, MAX_SUMMARY_CHARS)


def headline_line(item):
    title = md(short_headline(item["title"]))
    return f"• [{title}]({item['url']})" if item["url"] else f"• {title}"


def split_blocks(blocks, separator):
    """Join text blocks into descriptions that each fit one embed."""
    pages, current = [], ""
    for block in blocks:
        candidate = f"{current}{separator}{block}" if current else block
        if len(candidate) > MAX_DESCRIPTION and current:
            pages.append(current)
            current = block
        else:
            current = candidate
    if current:
        pages.append(current)
    return pages


def format_date(date):
    day = dt.date.fromisoformat(date)
    return f"{day:%B} {day.day}, {day.year}"


def build_embeds(digest):
    """Build the digest's embeds: one card, split only when it gets too long.

    Every story appears once: the top story only in the header, and a story
    whose title was already listed is skipped in later sections and quick hits.
    """
    links = {i["title"]: i["url"] for s in digest["sections"] for i in s["items"]}
    shown = set()
    lines = []
    if digest["lead"]:
        lead = digest["lead"]
        shown.add(lead["title"])
        title = md(short_headline(lead["title"]))
        url = links.get(lead["title"])
        lines.append(f"**[{title}]({url})**" if url else f"**{title}**")
        if lead["summary"]:
            lines.append(md(first_sentence(lead["summary"])))
    for section in digest["sections"]:
        items = []
        for item in section["items"]:
            if item["title"] not in shown:
                shown.add(item["title"])
                items.append(item)
        if items:
            emoji, name = SECTION_STYLES.get(section["label"], DEFAULT_STYLE)
            if lines:
                lines.append("")
            lines.append(f"**{emoji} {name or md(section['label'])}**")
            lines += [headline_line(i) for i in items]
    flashes = [i for i in digest["flashes"] if i["title"] not in shown]
    if flashes:
        if lines:
            lines.append("")
        lines.append("**⚡ Also**")
        lines += [headline_line(i) for i in flashes]

    pages = split_blocks(lines, "\n")
    embeds = [{
        "title": f"📰 Daily AI Report — {format_date(digest['date'])}",
        "color": EMBED_COLOR,
        "description": pages[0].strip(),
    }]
    embeds += [{"color": EMBED_COLOR, "description": page.strip()} for page in pages[1:]]
    embeds[-1]["footer"] = {
        "text": "Machine-translated from Chinese, "
                "so check the linked originals for exact details"
    }
    return embeds


def embed_size(embed):
    return (
        len(embed.get("title", ""))
        + len(embed.get("description", ""))
        + len(embed.get("footer", {}).get("text", ""))
    )


def build_messages(embeds):
    """Group embeds into webhook payloads that respect Discord's limits."""
    groups, current, size = [], [], 0
    for embed in embeds:
        length = embed_size(embed)
        if current and (len(current) >= MAX_EMBEDS or size + length > MAX_MESSAGE_CHARS):
            groups.append(current)
            current, size = [], 0
        current.append(embed)
        size += length
    if current:
        groups.append(current)
    return [
        # Empty parse list: text from feeds can never ping @everyone or anyone else.
        {"username": "Daily AI Report", "allowed_mentions": {"parse": []}, "embeds": group}
        for group in groups
    ]


def post_messages(webhook_url, messages, sleep=time.sleep):
    for index, message in enumerate(messages):
        request("Discord post", f"{webhook_url}?wait=true", message)
        if index < len(messages) - 1:
            sleep(1)


def read_last_posted(path=STATE_FILE):
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def write_last_posted(date, path=STATE_FILE):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(date + "\n", encoding="utf-8")


def already_posted(date, last_posted):
    return bool(last_posted) and date <= last_posted


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="print the Discord payloads; post nothing, save nothing")
    parser.add_argument("--force", action="store_true",
                        help="post even if this report was already posted")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not args.dry_run and not webhook.startswith(
        ("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/")
    ):
        parser.error("DISCORD_WEBHOOK_URL is missing or is not a Discord webhook URL")

    report = fetch_report()
    last_posted = read_last_posted()
    if already_posted(report["date"], last_posted) and not (args.force or args.dry_run):
        print(f"Report {report['date']} was already posted; nothing to do.")
        return 0

    digest = translate(normalize(report))
    messages = build_messages(build_embeds(digest))

    if args.dry_run:
        print(json.dumps(messages, ensure_ascii=False, indent=2))
        print(f"Dry run: built {len(messages)} message(s) for {report['date']}; nothing posted.")
        return 0

    post_messages(webhook, messages)
    write_last_posted(report["date"])
    print(f"Posted report {report['date']} in {len(messages)} message(s).")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except FetchError as err:
        print(f"error: {err}", file=sys.stderr)
        sys.exit(1)
