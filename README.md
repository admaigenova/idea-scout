# Idea Scout

A daily agent that scans Hacker News and Reddit for startup ideas, scores them
against one specific builder's skills and goals with Claude, and emails a
ranked digest every evening.

```
fetch (HN Algolia + Reddit RSS) -> analyse (claude-haiku-4-5) -> render (Jinja2) -> send (Resend)
```

## How it works

- **Fetch** — sources are pluggable. Each one implements the `Fetcher`
  interface in [scout.py](scout.py) and is registered in the `SOURCES` config
  list; the pipeline never changes when you add one.
  - Hacker News: "Show HN" + "Ask HN" from the last 24 h with ≥ 5 points, via
    the public Algolia API (no auth).
  - r/Startup_Ideas: public RSS feed via feedparser, with a proper User-Agent.
  - Reddit API (PRAW): stubbed and disabled (`enabled=False`) until
    credentials exist.
- **Analyse** — a single `claude-haiku-4-5` call scores every post 1–10 on
  four weighted dimensions: payer (30%), demand (30%), revenue in 3 months
  (25%), buildable (15%) — plus an unweighted difficulty-to-develop rating
  that is shown in the email but never affects the ranking. The rubric lives in [prompts.py](prompts.py) as
  `SCORING_PROMPT`. The reply must be pure JSON; the parser strips markdown
  fences, retries once asking the model to fix broken JSON, recomputes the
  weighted totals locally and re-ranks.
- **Render** — [template.html](template.html) is an email-safe newsletter
  (inline CSS, table layout, 640 px, single column) populated with Jinja2.
- **Send** — Resend, from `onboarding@resend.dev`. The subject carries the
  top pick and score; on a day where nothing scores ≥ 6.0 you still get a
  "Nothing worth your time today" email, so you know it ran.

## Run locally

Requires Python 3.11+ (on Windows: `winget install Python.Python.3.11`).

```bash
python -m venv .venv
```

Activate it (`source .venv/bin/activate` on macOS/Linux,
`.venv\Scripts\Activate.ps1` on Windows), then:

```bash
pip install -r requirements.txt
```

```bash
cp .env.example .env   # then fill in your keys
```

Dry run — fetches and scores for real, but writes `preview.html` instead of
sending (needs only `ANTHROPIC_API_KEY`):

```bash
python scout.py --dry-run
```

Real run (also needs `RESEND_API_KEY` and `EMAIL_TO`):

```bash
python scout.py
```

## Deploy (GitHub Actions)

1. Push this repo to GitHub.
2. In the repo: **Settings → Secrets and variables → Actions → New repository
   secret**, add:

   | Secret | Required | Purpose |
   | --- | --- | --- |
   | `ANTHROPIC_API_KEY` | yes | scoring |
   | `RESEND_API_KEY` | yes | email delivery |
   | `EMAIL_TO` | yes | recipient address |
   | `REDDIT_CLIENT_ID` | optional | only when the PRAW source is enabled |
   | `REDDIT_CLIENT_SECRET` | optional | only when the PRAW source is enabled |
   | `REDDIT_USER_AGENT` | optional | only when the PRAW source is enabled |

3. [.github/workflows/daily.yml](.github/workflows/daily.yml) runs every day
   at 12:00 UTC — 10 pm Melbourne during AEST. Melbourne shifts to AEDT
   (UTC+11) October–April, when the email lands at 11 pm; edit the cron if
   that matters to you.
4. Manual run: **Actions → Idea Scout daily → Run workflow**.
5. If `scout.py` exits non-zero the run is marked failed — visible in the
   Actions tab (turn on notifications for failed workflows in your GitHub
   settings if you want an alert).

## Data log & dashboard

Every real (non-dry-run) run appends the full ranked list — up to 10 ideas,
more than the email shows — to [data/ideas.csv](data/ideas.csv) and regenerates
[docs/index.html](docs/index.html): **The Scout Report**, a self-contained
dashboard with decision-focused KPIs (worth pursuing, quick wins, repeat
signals, best score), two trend charts, and the top 10 prospects ranked by
Overall score, Payer, Demand, Revenue 3 mo, Buildable, or Easiest-to-build,
filterable to the last 7/30 days. Ideas seen on multiple days are deduplicated
and marked "seen N×". The workflow commits both files back after each run.

Ways to view the dashboard:

- `git pull`, then double-click `docs/index.html` — works offline, no server.
- Or enable GitHub Pages (**Settings → Pages → Deploy from a branch →
  `main` / `docs`**) for a permanent URL. Note: on a free GitHub plan, Pages
  requires the repository to be public.

The CSV is a flat, analysis-ready table (one row per idea per day) — point
Power BI or Excel at it whenever you want deeper slicing.

## Resend notes

The default `onboarding@resend.dev` sender can only deliver **to the email
address you signed up to Resend with**. To send to anything else, verify a
domain in Resend and change `EMAIL_FROM` in [scout.py](scout.py).

## Adding a source

1. Subclass `Fetcher` in [scout.py](scout.py) and implement
   `fetch() -> list[Post]`.
2. Append a `SourceConfig(YourFetcher, "Display name", options={...})` entry
   to `SOURCES`.

That's it — fetching, scoring, rendering and sending pick it up automatically.

## Enabling the Reddit API (PRAW) source

1. Create a "script" app at <https://www.reddit.com/prefs/apps>.
2. Set `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USER_AGENT`
   (locally in `.env`, and as Actions secrets for the workflow).
3. Un-comment `praw` in [requirements.txt](requirements.txt).
4. In [scout.py](scout.py), replace the body of `RedditPrawFetcher.fetch()`
   with the commented sketch and flip its `SOURCES` entry to `enabled=True`.
