#!/usr/bin/env python3
"""Idea Scout: fetch idea posts, have Claude score them, email a daily digest.

Pipeline: fetch -> analyse -> render -> send.

Sources are pluggable: implement a Fetcher subclass, register it in SOURCES,
and the pipeline picks it up — no pipeline code changes needed.
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser
import requests
import resend
from anthropic import Anthropic
from dotenv import load_dotenv
from jinja2 import Environment, FileSystemLoader

from dashboard import write_dashboard
from prompts import FIX_JSON_PROMPT, SCORING_PROMPT, SYSTEM_PROMPT
from store import append_ideas, load_ideas

MODEL = "claude-haiku-4-5"
MAX_TOKENS = 5000  # headroom for 10 ideas with 3-5 sentence summaries
BODY_LIMIT = 1500          # max chars of post body sent to the model
WORTH_IT_THRESHOLD = 6.0   # minimum total score for "worth your time"
SCORE_WEIGHTS = {"payer": 0.30, "demand": 0.30, "revenue_3mo": 0.25, "buildable": 0.15}
LOCAL_TZ = ZoneInfo("Australia/Melbourne")
DEFAULT_USER_AGENT = "python:idea-scout:1.0 (daily idea digest)"
EMAIL_FROM = "Idea Scout <onboarding@resend.dev>"


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

@dataclass
class Post:
    title: str
    body: str
    url: str
    points: int
    comments: int
    source: str


def clean_text(raw: str) -> str:
    """Drop HTML tags/entities and collapse whitespace to a single line."""
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def truncate(text: str, limit: int = BODY_LIMIT) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class Fetcher(ABC):
    """One idea source. Subclass, implement fetch(), register in SOURCES."""

    def __init__(self, name: str, **options):
        self.name = name
        self.options = options

    @abstractmethod
    def fetch(self) -> list[Post]:
        ...


class HackerNewsFetcher(Fetcher):
    """Show HN / Ask HN stories via the public Algolia API (no auth)."""

    API = "https://hn.algolia.com/api/v1/search_by_date"

    def fetch(self) -> list[Post]:
        cutoff = int(time.time()) - int(self.options.get("window_hours", 24)) * 3600
        params = {
            "tags": "(" + ",".join(self.options.get("tags", ["show_hn", "ask_hn"])) + ")",
            "numericFilters": f"created_at_i>={cutoff},points>={self.options.get('min_points', 5)}",
            "hitsPerPage": 100,
        }
        resp = requests.get(self.API, params=params, timeout=30)
        resp.raise_for_status()
        posts = []
        for hit in resp.json().get("hits", []):
            discussion = f"https://news.ycombinator.com/item?id={hit.get('objectID', '')}"
            posts.append(
                Post(
                    title=clean_text(hit.get("title") or "(untitled)"),
                    body=truncate(clean_text(hit.get("story_text") or "")),
                    url=hit.get("url") or discussion,
                    points=int(hit.get("points") or 0),
                    comments=int(hit.get("num_comments") or 0),
                    source=self.name,
                )
            )
        return posts


class RedditRssFetcher(Fetcher):
    """Public subreddit RSS feed via feedparser — no credentials required."""

    _BOILERPLATE = re.compile(r"\s*submitted by\s+/u/\S+.*$", re.IGNORECASE)

    def fetch(self) -> list[Post]:
        user_agent = os.environ.get("REDDIT_USER_AGENT") or DEFAULT_USER_AGENT
        resp = requests.get(
            self.options["feed_url"], headers={"User-Agent": user_agent}, timeout=30
        )
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        if feed.bozo and not feed.entries:
            raise RuntimeError(f"unparseable feed: {feed.bozo_exception}")

        cutoff = datetime.now(timezone.utc) - timedelta(
            hours=self.options.get("window_hours", 24)
        )
        posts = []
        for entry in feed.entries:
            published = entry.get("published_parsed") or entry.get("updated_parsed")
            if published and datetime(*published[:6], tzinfo=timezone.utc) < cutoff:
                continue
            body = self._BOILERPLATE.sub("", clean_text(entry.get("summary", "")))
            posts.append(
                Post(
                    title=clean_text(entry.get("title", "(untitled)")),
                    body=truncate(body),
                    url=entry.get("link", self.options["feed_url"]),
                    points=0,    # the public RSS feed exposes no vote counts
                    comments=0,  # ...or comment counts
                    source=self.name,
                )
            )
        return posts


class RedditPrawFetcher(Fetcher):
    """Stub: authenticated Reddit API via PRAW.

    To enable:
      1. Create a "script" app at https://www.reddit.com/prefs/apps
      2. Set REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET and REDDIT_USER_AGENT
      3. Un-comment praw in requirements.txt
      4. Replace fetch() with the sketch below and set enabled=True in SOURCES
    """

    def fetch(self) -> list[Post]:
        # import praw
        # reddit = praw.Reddit(
        #     client_id=os.environ["REDDIT_CLIENT_ID"],
        #     client_secret=os.environ["REDDIT_CLIENT_SECRET"],
        #     user_agent=os.environ["REDDIT_USER_AGENT"],
        # )
        # cutoff = time.time() - self.options.get("window_hours", 24) * 3600
        # subreddits = "+".join(self.options.get("subreddits", ["Startup_Ideas"]))
        # return [
        #     Post(
        #         title=submission.title,
        #         body=truncate(clean_text(submission.selftext)),
        #         url="https://www.reddit.com" + submission.permalink,
        #         points=int(submission.score),
        #         comments=int(submission.num_comments),
        #         source=self.name,
        #     )
        #     for submission in reddit.subreddit(subreddits).new(limit=100)
        #     if submission.created_utc >= cutoff
        # ]
        raise NotImplementedError("PRAW source is stubbed — see the class docstring.")


@dataclass
class SourceConfig:
    fetcher: type[Fetcher]
    name: str
    enabled: bool = True
    options: dict = field(default_factory=dict)


SOURCES = [
    SourceConfig(
        HackerNewsFetcher,
        "Hacker News",
        options={"tags": ["show_hn", "ask_hn"], "min_points": 5, "window_hours": 24},
    ),
    SourceConfig(
        RedditRssFetcher,
        "r/Startup_Ideas",
        options={"feed_url": "https://www.reddit.com/r/Startup_Ideas/.rss", "window_hours": 24},
    ),
    SourceConfig(
        RedditPrawFetcher,
        "Reddit API",
        enabled=False,  # flip on once PRAW credentials exist
        options={"subreddits": ["Startup_Ideas"], "window_hours": 24},
    ),
]


def fetch_all() -> list[Post]:
    posts: list[Post] = []
    enabled = [cfg for cfg in SOURCES if cfg.enabled]
    failures = 0
    for cfg in enabled:
        try:
            fetched = cfg.fetcher(cfg.name, **cfg.options).fetch()
        except Exception as exc:  # one broken source must not kill the run
            failures += 1
            print(f"[fetch] {cfg.name} FAILED: {exc}", file=sys.stderr)
            continue
        print(f"[fetch] {cfg.name}: {len(fetched)} posts")
        posts.extend(fetched)
    if enabled and failures == len(enabled):
        raise RuntimeError("every enabled source failed")
    return posts


# ---------------------------------------------------------------------------
# Analyse
# ---------------------------------------------------------------------------

def _extract_json(text: str) -> dict:
    """json.loads after stripping markdown fences, with a brace-slice fallback."""
    cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", text.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(cleaned[start : end + 1])


def _to_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalise(result: dict, posts: list[Post]) -> dict:
    """Recompute totals from the weights, repair echoed metadata, rank descending."""
    by_url = {p.url: p for p in posts}
    by_title = {p.title.lower(): p for p in posts}
    ideas = []
    for raw in result.get("ideas", []):
        try:
            scores = {k: max(1, min(10, int(raw["scores"][k]))) for k in SCORE_WEIGHTS}
        except (KeyError, TypeError, ValueError):
            print(f"[analyse] dropped malformed idea: {str(raw)[:120]}", file=sys.stderr)
            continue
        # informational only — displayed in the email, never part of the total
        scores["difficulty"] = max(1, min(10, _to_int(raw["scores"].get("difficulty"), 5)))
        idea = {
            "title": str(raw.get("title") or "(untitled)").strip(),
            "url": str(raw.get("url") or ""),
            "points": _to_int(raw.get("points")),
            "comments": _to_int(raw.get("comments")),
            "source": str(raw.get("source") or ""),
            "scores": scores,
            "total": round(sum(scores[k] * w for k, w in SCORE_WEIGHTS.items()), 1),
            "summary": str(raw.get("summary") or "").strip(),
            "verdict": str(raw.get("verdict") or "").strip(),
        }
        match = by_url.get(idea["url"]) or by_title.get(idea["title"].lower())
        if match:  # trust fetched data over the model's echo
            idea.update(
                url=match.url, points=match.points, comments=match.comments, source=match.source
            )
        ideas.append(idea)
    ideas.sort(key=lambda idea: idea["total"], reverse=True)
    return {
        "ideas": ideas,
        "skipped_summary": str(result.get("skipped_summary") or "").strip(),
    }


def analyse(posts: list[Post]) -> dict:
    # .strip(): a key pasted into a secret store with a trailing newline makes
    # an illegal HTTP header, which surfaces as a bare "Connection error".
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"].strip())
    messages = [
        {
            "role": "user",
            "content": SCORING_PROMPT + "\n"
            + json.dumps([asdict(p) for p in posts], ensure_ascii=False),
        }
    ]
    response = client.messages.create(
        model=MODEL, max_tokens=MAX_TOKENS, system=SYSTEM_PROMPT, messages=messages
    )
    text = "".join(block.text for block in response.content if block.type == "text")
    if response.stop_reason == "max_tokens":
        print("[analyse] warning: response hit the max_tokens cap", file=sys.stderr)
    try:
        result = _extract_json(text)
    except json.JSONDecodeError:
        print("[analyse] invalid JSON — asking the model to fix it and retrying once")
        messages += [
            {"role": "assistant", "content": text or "(empty response)"},
            {"role": "user", "content": FIX_JSON_PROMPT},
        ]
        response = client.messages.create(
            model=MODEL, max_tokens=MAX_TOKENS, system=SYSTEM_PROMPT, messages=messages
        )
        text = "".join(block.text for block in response.content if block.type == "text")
        result = _extract_json(text)  # a second failure is fatal: exit non-zero
    return _normalise(result, posts)


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def render_email(result: dict, posts_scanned: int) -> tuple[str, str]:
    now = datetime.now(LOCAL_TZ)
    ideas = result["ideas"]
    worth_count = sum(1 for idea in ideas if idea["total"] >= WORTH_IT_THRESHOLD)
    top_pick = ideas[0] if ideas and ideas[0]["total"] >= WORTH_IT_THRESHOLD else None
    runners_up = ideas[1:3] if top_pick else ideas[:3]

    env = Environment(
        loader=FileSystemLoader(Path(__file__).resolve().parent),
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    body = env.get_template("template.html").render(
        date_str=f"{now:%A} {now.day} {now:%B %Y}",  # e.g. "Sunday 26 July 2026"
        posts_scanned=posts_scanned,
        worth_count=worth_count,
        top_pick=top_pick,
        runners_up=runners_up,
        skipped_summary=result["skipped_summary"],
    )
    if top_pick:
        subject = f"Idea Scout · {top_pick['title']} ({top_pick['total']:.1f}/10)"
    else:
        subject = "Idea Scout · Nothing worth your time today"
    return subject, body


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------

def send_email(subject: str, body: str) -> None:
    resend.api_key = os.environ["RESEND_API_KEY"].strip()
    recipients = [addr.strip() for addr in os.environ["EMAIL_TO"].split(",") if addr.strip()]
    sent = resend.Emails.send(
        {"from": EMAIL_FROM, "to": recipients, "subject": subject, "html": body}
    )
    email_id = sent.get("id", "?") if isinstance(sent, dict) else getattr(sent, "id", "?")
    print(f"[send] delivered to {', '.join(recipients)} (id: {email_id})")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles may default to cp1252
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    load_dotenv()
    dry_run = "--dry-run" in sys.argv[1:]
    required = ["ANTHROPIC_API_KEY"] if dry_run else ["ANTHROPIC_API_KEY", "RESEND_API_KEY", "EMAIL_TO"]
    missing = [name for name in required if not os.environ.get(name, "").strip()]
    if missing:
        sys.exit(f"Missing environment variables: {', '.join(missing)}")

    posts = fetch_all()
    print(f"[fetch] {len(posts)} posts total")

    if posts:
        result = analyse(posts)
        print(f"[analyse] {len(result['ideas'])} ideas ranked")
    else:
        result = {"ideas": [], "skipped_summary": "No new posts were found in the last 24 hours."}

    subject, body = render_email(result, len(posts))

    if dry_run:
        Path("preview.html").write_text(body, encoding="utf-8")
        print(f"[dry-run] subject: {subject}")
        print("[dry-run] wrote preview.html — nothing was sent or logged")
        return

    send_email(subject, body)

    # Persist the day's ideas and refresh the static dashboard; the workflow
    # commits both files back to the repo after this script exits.
    stamp = datetime.now(LOCAL_TZ)
    written = append_ideas(result["ideas"], f"{stamp:%Y-%m-%d}")
    records = load_ideas()
    dashboard_path = write_dashboard(
        records, updated=f"{stamp:%A} {stamp.day} {stamp:%B %Y}, {stamp:%H:%M %Z}"
    )
    print(f"[store] logged {written} ideas ({len(records)} rows total); refreshed {dashboard_path.name}")


if __name__ == "__main__":
    main()
