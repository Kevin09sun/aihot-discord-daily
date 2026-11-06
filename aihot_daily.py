#!/usr/bin/env python3
"""Post the latest AIHOT daily report to a Discord channel, translated to English.

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
import http.client
import ipaddress
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
MAX_TITLE_CHARS = 240
MAX_EMBED_TITLE = 256
MAX_SUMMARY_CHARS = 700
MAX_URL_CHARS = 500
MAX_IMAGE_URL_CHARS = 2000
MAX_PAGE_BYTES = 1_000_000
PAGE_TIMEOUT_SECONDS = 15
MAX_TRANSLATION_INPUT = 2000
MAX_BATCH_CHARS = 4000
MAX_MYMEMORY_CHARS = 150  # MyMemory allows 500 bytes per request
TRANSLATION_PAUSE_SECONDS = 1

HEADER_COLOR = 0x5865F2
FLASH_COLOR = 0xFEE75C
SECTION_STYLES = {
    "模型发布/更新": ("🧠", "Model Releases & Updates", 0x9B59B6),
    "产品发布/更新": ("🚀", "Product Launches & Updates", 0x3498DB),
    "行业动态": ("🏢", "Industry News", 0x2ECC71),
    "论文研究": ("📄", "Research Papers", 0xE67E22),
    "技巧与观点": ("💡", "Tips & Opinions", 0x1ABC9C),
}
DEFAULT_STYLE = ("📌", None, 0x95A5A6)
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
        "summary": clean(raw.get("summary")),
        "source": clean((raw.get("source") or {}).get("name")),
        "url": safe_url((raw.get("links") or {}).get("original")),
        "image": "",
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


def shorten_source(name):
    """Reduce a feed name to its publisher, dropping details such as RSS or web.

    "OpenAI：官网动态（RSS）" becomes "OpenAI"; "X：Jane Doe (@jane)" becomes "Jane Doe on X".
    """
    match = re.match(r"^X\s*[：:]\s*(.+)$", name)
    if match:
        return f"{re.split(r'[（(]', match.group(1))[0].strip()} on X"
    return re.split(r"[：:（(]", name)[0].strip() or name


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
    for item in items:
        item["source"] = shorten_source(item["source"])
        fields += [(item, "title"), (item, "summary"), (item, "source")]

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


def usable_image(url):
    """Reject images Discord can't load, and X profile pictures."""
    if not url.startswith("https://") or len(url) > MAX_IMAGE_URL_CHARS or re.search(r"\s", url):
        return False
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    if not host or host == "localhost" or host.endswith(".local") or is_aihot_host(host):
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    return "/profile_images/" not in url


def parse_preview_image(page, base_url):
    """Return the page's Open Graph or Twitter preview image URL, or ""."""
    found = {}
    for tag in re.findall(rb"<meta\b[^>]*>", page, re.I):
        name = re.search(rb"""(?:property|name)\s*=\s*["']([^"']+)["']""", tag, re.I)
        content = re.search(rb"""content\s*=\s*"([^"]+)"|content\s*=\s*'([^']+)'""", tag, re.I)
        if name and content:
            key = name.group(1).decode("ascii", "ignore").lower()
            value = content.group(1) or content.group(2)
            found.setdefault(key, value.decode("utf-8", "replace"))
    for key in ("og:image", "og:image:secure_url", "twitter:image", "twitter:image:src"):
        if key in found:
            url = urllib.parse.urljoin(base_url, html.unescape(found[key]).strip())
            if usable_image(url):
                return url
    return ""


def open_page(url, read_bytes=0):
    """Return (content type, body). Raises OSError or HTTPException on failure."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=PAGE_TIMEOUT_SECONDS) as resp:
        return resp.headers.get_content_type(), resp.read(read_bytes) if read_bytes else b""


def find_image(url):
    """Find a story's preview image. Images are optional, so failures return ""."""
    try:
        content_type, page = open_page(url, MAX_PAGE_BYTES)
        if "html" not in content_type:
            return ""
        image = parse_preview_image(page, url)
        if not image:
            return ""
        content_type, _ = open_page(image)
        return image if content_type.startswith("image/") else ""
    except (OSError, ValueError, http.client.HTTPException) as err:
        print(f"note: no image for {url[:80]}: {err}", file=sys.stderr)
        return ""


def attach_images(digest, finder=find_image):
    """Add preview images to the main stories. Returns how many were found."""
    count = 0
    for section in digest["sections"]:
        for item in section["items"]:
            item["image"] = finder(item["url"]) if item["url"] else ""
            count += bool(item["image"])
    return count


def md(text, limit=None):
    """Escape Discord markdown in third-party text so it stays plain text."""
    return MARKDOWN.sub(r"\\\1", clean(text, limit))


def story_embed(item, color, section_title=None):
    """Build one story card. Its title and source line link to the original."""
    title = md(item["title"], MAX_TITLE_CHARS)[:MAX_EMBED_TITLE].rstrip("\\")
    source = md(item["source"], 100)
    lines = []
    if item["summary"]:
        lines.append(md(item["summary"], MAX_SUMMARY_CHARS))
    if item["url"]:
        lines.append(f"🔗 [{source or 'Read the source'}]({item['url']})")
    elif source:
        lines.append(f"*{source}*")
    embed = {"title": title, "color": color}
    if lines:
        embed["description"] = "\n\n".join(lines)
    if item["url"]:
        embed["url"] = item["url"]
    if item["image"]:
        embed["image"] = {"url": item["image"]}
    if section_title:
        embed["author"] = {"name": section_title}
    return embed


def format_flash(item):
    title = md(item["title"], MAX_TITLE_CHARS)
    line = f"• [{title}]({item['url']})" if item["url"] else f"• {title}"
    if item["source"]:
        line += f" — *{md(item['source'], 100)}*"
    return line


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


def section_embeds(title, color, blocks, separator):
    pages = split_blocks(blocks, separator)
    return [
        {
            "title": title if i == 0 else f"{title} (continued)",
            "color": color,
            "description": page,
        }
        for i, page in enumerate(pages)
    ]


def format_date(date):
    day = dt.date.fromisoformat(date)
    return f"{day:%B} {day.day}, {day.year}"


def build_embeds(digest):
    stories = sum(len(s["items"]) for s in digest["sections"])
    counts = f"{stories} {'story' if stories == 1 else 'stories'}"
    hits = len(digest["flashes"])
    if hits:
        counts += f" · {hits} quick {'hit' if hits == 1 else 'hits'}"
    links = {i["title"]: i["url"] for s in digest["sections"] for i in s["items"]}
    summaries = {i["summary"] for s in digest["sections"] for i in s["items"]}
    parts = []
    if digest["lead"]:
        lead_title = md(digest["lead"]["title"], MAX_TITLE_CHARS)
        lead_url = links.get(digest["lead"]["title"])
        parts.append(f"**[{lead_title}]({lead_url})**" if lead_url else f"**{lead_title}**")
        if digest["lead"]["summary"] and digest["lead"]["summary"] not in summaries:
            parts.append(md(digest["lead"]["summary"], MAX_SUMMARY_CHARS * 2))
        parts.append("")
    parts.append(counts)
    embeds = [{
        "title": f"📰 Daily AI Report — {format_date(digest['date'])}",
        "color": HEADER_COLOR,
        "description": "\n".join(parts),
    }]
    for section in digest["sections"]:
        emoji, name, color = SECTION_STYLES.get(section["label"], DEFAULT_STYLE)
        section_title = f"{emoji} {name or section['label']}"
        for index, item in enumerate(section["items"]):
            embeds.append(story_embed(item, color, section_title if index == 0 else None))
    if digest["flashes"]:
        embeds += section_embeds(
            "⚡ Quick Hits", FLASH_COLOR, [format_flash(i) for i in digest["flashes"]], "\n"
        )
    embeds[-1]["footer"] = {
        "text": "Compiled by AIHOT · Machine-translated from Chinese, "
                "so check the linked originals for exact details"
    }
    return embeds


def embed_size(embed):
    return (
        len(embed.get("title", ""))
        + len(embed.get("author", {}).get("name", ""))
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
    images = attach_images(digest)
    print(f"Found images for {images} stories.")
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
