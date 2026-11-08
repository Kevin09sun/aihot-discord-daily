# AIHOT Discord daily

Posts a concise English digest of the [AIHOT](https://aihot.news) daily report to a Discord channel each day. It runs on GitHub Actions and has no dependencies beyond Python.

Each report is posted at about 00:17 UTC, shortly after AIHOT publishes it at 00:00 UTC. A second run at 02:47 UTC acts as a backup and does nothing if the report is already posted. GitHub can start scheduled runs late.

## Setup

1. In Discord, go to Channel Settings → Integrations → Webhooks and create a webhook.
2. Save its URL as a secret, then paste it when prompted:

   ```
   gh secret set DISCORD_WEBHOOK_URL -R Kevin09sun/aihot-discord-daily
   ```

Translation needs no key or account. It uses Google Translate's free web endpoint, sending the report in batches to keep requests low. That endpoint is unofficial and can refuse requests, so the free MyMemory API (about 5,000 characters a day) takes over when it does. If both refuse, the run fails rather than posting untranslated text, and GitHub emails you about the failed run.

## What gets posted

One short message with:

- The top story, with a one-sentence summary
- Up to 4 images in one grid card, with numbered captions linking to each story. Images are the preview images source pages publish, often screenshots attached to X posts. Logos and profile pictures are skipped.
- One-line headlines grouped by topic, with long headlines cut to their main clause
- Up to 5 quick hits (`MAX_QUICK_HITS`)

All links go to the original source, never to AIHOT.

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
