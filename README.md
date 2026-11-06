# AIHOT Discord daily

Posts the [AIHOT](https://aihot.news) daily report to a Discord channel each day, translated into English. It runs on GitHub Actions and has no dependencies beyond Python.

Each report is posted at about 00:17 UTC, shortly after AIHOT publishes it at 00:00 UTC. A second run at 02:47 UTC acts as a backup and does nothing if the report is already posted. GitHub can start scheduled runs late.

## Setup

1. In Discord, go to Channel Settings → Integrations → Webhooks and create a webhook.
2. Save its URL as a secret, then paste it when prompted:

   ```
   gh secret set DISCORD_WEBHOOK_URL -R Kevin09sun/aihot-discord-daily
   ```

Translation uses Google Translate's free web endpoint, so it needs no key or account. The endpoint is unofficial and could change or start blocking requests. If that happens, the run fails rather than posting untranslated text, and GitHub emails you about the failed run.

## Images and links

Each main story is its own card, showing the preview image its source page publishes. A story goes without an image if the page has none, blocks the request, or only offers an X profile picture. Quick hits stay as a text list. All links go to the original source, never to AIHOT.

## Running it manually

Under Actions → Daily AI report → Run workflow:

- **dry_run**, on by default, prints the report in the log and posts nothing.
- **force** posts again even if that report has already been posted.

## Configuration

- **Time:** change the `cron` lines in `.github/workflows/daily.yml`. Times are in UTC.
- **Duplicates:** the last posted date is stored in `state/last_posted.txt`.

## Tests

```
python -m unittest -v
```

## Usage terms

AIHOT's [public use rules](https://aihot.news/terms) allow free personal, non-commercial use. They require written permission for commercial or public redistribution, so this setup is intended for a private server only.
