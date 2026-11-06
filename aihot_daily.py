#!/usr/bin/env python3
"""Post the latest AIHOT daily report to a Discord channel, translated to English.

Uses only the Python standard library. Translation uses Google Translate's free
web endpoint, which needs no key but is unofficial and could change.

Environment:
  DISCORD_WEBHOOK_URL  target webhook (not needed with --dry-run)
"""
import argparse
import copy
import datetime as dt
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
USER_AGENT = "aihot-discord-daily/1.0 (private, non-commercial)"
STATE_FILE = Path(__file__).parent / "state" / "last_posted.txt"

# Discord allows 4096 per description, 6000 per message and 10 embeds per
# message. Stay a little under the character limits.
MAX_DESCRIPTION = 4000
MAX_MESSAGE_CHARS = 5800
MAX_EMBEDS = 10
MAX_TITLE_CHARS = 300
MAX_SUMMARY_CHARS = 700
MAX_URL_CHARS = 500
MAX_TRANSLATION_INPUT = 2000
TRANSLATION_PAUSE_SECONDS = 0.2

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
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if limit and len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def safe_url(url):
    url = str(url or "").strip()
    if not url.startswith("https://") or len(url) > MAX_URL_CHARS or re.search(r"\s", url):
        return ""
    return url.replace("(", "%28").replace(")", "%29")


def normalize_item(raw):
    aihot = safe_url((raw.get("links") or {}).get("aihot"))
    original = safe_url((raw.get("links") or {}).get("original"))
    return {
        "title": clean(raw.get("title")),
        "summary": clean(raw.get("summary")),
        "source": clean((raw.get("source") or {}).get("name")),
        "url": original or aihot,
        "aihot": aihot if original else "",
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
        "url": safe_url((report.get("links") or {}).get("aihot")),
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


def google_translate(text):
    raw = request("Translation", TRANSLATE_URL, form={"q": text[:MAX_TRANSLATION_INPUT]})
    return parse_google_response(raw)


def translate(digest, translator=google_translate, sleep=time.sleep):
    """Return an English copy of `digest`, using `translator(text) -> english`.

    Text without Chinese is left as is, and repeated text is translated once.
    """
    digest = copy.deepcopy(digest)
    cache = {}

    def english(text):
        text = clean(text)
        if not CJK.search(text):
            return text
        if text not in cache:
            cache[text] = clean(translator(text))
            sleep(TRANSLATION_PAUSE_SECONDS)
            if CJK.search(cache[text]):
                print(f"warning: Chinese left after translating: {text[:60]}", file=sys.stderr)
        return cache[text]

    if digest["lead"]:
        for field in ("title", "summary"):
            digest["lead"][field] = english(digest["lead"][field])
    items = list(digest["flashes"])
    for section in digest["sections"]:
        if section["label"] not in SECTION_STYLES:
            section["label"] = english(section["label"])
        items.extend(section["items"])
    for item in items:
        item["title"] = english(item["title"])
        item["summary"] = english(item["summary"])
        item["source"] = english(shorten_source(item["source"]))
    return digest


def md(text, limit=None):
    """Escape Discord markdown in third-party text so it stays plain text."""
    return MARKDOWN.sub(r"\\\1", clean(text, limit))


def format_item(item):
    title = md(item["title"], MAX_TITLE_CHARS)
    lines = [f"**[{title}]({item['url']})**" if item["url"] else f"**{title}**"]
    if item["summary"]:
        lines.append(md(item["summary"], MAX_SUMMARY_CHARS))
    meta = []
    if item["source"]:
        meta.append(f"*{md(item['source'], 100)}*")
    if item["aihot"]:
        meta.append(f"[AIHOT]({item['aihot']})")
    if meta:
        lines.append(" · ".join(meta))
    return "\n".join(lines)


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
    parts = []
    if digest["lead"]:
        parts.append(f"**{md(digest['lead']['title'], MAX_TITLE_CHARS)}**")
        if digest["lead"]["summary"]:
            parts.append(md(digest["lead"]["summary"], MAX_SUMMARY_CHARS * 2))
        parts.append("")
    parts.append(counts)
    header = {
        "title": f"📰 Daily AI Report — {format_date(digest['date'])}",
        "color": HEADER_COLOR,
        "description": "\n".join(parts),
    }
    if digest["url"]:
        header["url"] = digest["url"]

    embeds = [header]
    for section in digest["sections"]:
        emoji, name, color = SECTION_STYLES.get(section["label"], DEFAULT_STYLE)
        title = f"{emoji} {name or section['label']}"
        embeds += section_embeds(
            title, color, [format_item(i) for i in section["items"]], "\n\n"
        )
    if digest["flashes"]:
        embeds += section_embeds(
            "⚡ Quick Hits", FLASH_COLOR, [format_flash(i) for i in digest["flashes"]], "\n"
        )
    embeds[-1]["footer"] = {
        "text": "From AIHOT (aihot.news) · Machine-translated from Chinese, "
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
