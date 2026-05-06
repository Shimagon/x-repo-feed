"""Poll @<X_USERNAME>'s X profile via Scrapling and forward GitHub-bearing reposts to Gmail."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import smtplib
import sys
from email.mime.text import MIMEText

from scrapling.fetchers import StealthyFetcher

X_USERNAME = os.environ["X_USERNAME"]
X_AUTH_TOKEN = os.environ["X_AUTH_TOKEN"]
X_CT0 = os.environ["X_CT0"]
GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
FORWARD_TO = os.environ.get("FORWARD_TO") or GMAIL_USER
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
DRY_RUN = os.environ.get("DRY_RUN") == "1"
DEBUG = os.environ.get("DEBUG") == "1"

COOKIES = [
    {"name": "auth_token", "value": X_AUTH_TOKEN, "domain": ".x.com", "path": "/",
     "httpOnly": True, "secure": True, "sameSite": "None"},
    {"name": "ct0", "value": X_CT0, "domain": ".x.com", "path": "/",
     "httpOnly": False, "secure": True, "sameSite": "Lax"},
]

GH_RE = re.compile(r"https?://github\.com/[\w.-]+/[\w.-]+", re.IGNORECASE)
REPOST_RE = re.compile(r"repost|retweet|リポスト|リツイート", re.IGNORECASE)
STATUS_RE = re.compile(r"/status/(\d+)")


def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"processed_ids": []}


def save_state(state: dict) -> None:
    state["processed_ids"] = state.get("processed_ids", [])[-300:]
    state["last_run_at"] = dt.datetime.now(dt.UTC).isoformat()
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
        f.write("\n")


def fetch_tweets() -> list[dict]:
    print(f"Visiting https://x.com/{X_USERNAME}")
    page = StealthyFetcher.fetch(
        f"https://x.com/{X_USERNAME}",
        headless=True,
        network_idle=True,
        wait_selector='article[data-testid="tweet"]',
        wait_selector_state="visible",
        timeout=60000,
        cookies=COOKIES,
        locale="ja-JP",
        google_search=False,
        disable_resources=True,  # block fonts/images for speed; tweet text is text content
    )

    if page.status >= 400:
        raise SystemExit(f"X returned status {page.status}")

    articles = page.css('article[data-testid="tweet"]')
    out: list[dict] = []
    for a in articles:
        # tweet ID — first /status/<id> href inside the article
        status_links = a.css('a[href*="/status/"]::attr(href)') or []
        status_links = list(status_links)
        tweet_id = None
        for h in status_links:
            m = STATUS_RE.search(str(h))
            if m:
                tweet_id = m.group(1)
                break
        if not tweet_id:
            continue

        ctx_text = (a.css('[data-testid="socialContext"]::text').get() or "").strip()
        is_repost = bool(REPOST_RE.search(ctx_text))

        tweet_text_parts = a.css('[data-testid="tweetText"] *::text').getall() or []
        tweet_text = "".join(tweet_text_parts).strip()
        if not tweet_text:
            tweet_text = (a.css('[data-testid="tweetText"]::text').get() or "").strip()

        anchors = a.css("a")
        haystack_pieces: list[str] = []
        for el in anchors:
            for sel in ("::attr(href)", "::attr(aria-label)", "::text"):
                v = el.css(sel).get()
                if v:
                    haystack_pieces.append(str(v))
        haystack = " ".join(haystack_pieces)
        gh_matches = GH_RE.findall(haystack)

        out.append({
            "id": tweet_id,
            "isRepost": is_repost,
            "ctxText": ctx_text,
            "tweetText": tweet_text,
            "githubMatches": gh_matches,
        })
    return out


def normalize_gh_url(u: str) -> str | None:
    m = re.search(r"github\.com/([\w.-]+)/([\w.-]+)", u, re.IGNORECASE)
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    repo = re.sub(r"\.git$", "", repo, flags=re.IGNORECASE)
    repo = re.sub(r"[.,;:]+$", "", repo)
    if owner.lower() in {"i", "search", "settings", "explore"}:
        return None  # X internal paths
    return f"https://github.com/{owner}/{repo}"


def send_one(tweet: dict, repos: list[str]) -> None:
    slug = repos[0].replace("https://github.com/", "")
    extra = f" (+{len(repos) - 1})" if len(repos) > 1 else ""
    subject = f"[repo-feed] {slug}{extra}"
    body = "\n".join([
        f"Reposted by @{X_USERNAME}",
        f"Tweet: https://x.com/i/status/{tweet['id']}",
        "",
        "Repos:",
        *[f"  {r}" for r in repos],
        "",
        "--- tweet text ---",
        tweet["tweetText"] or "(no text)",
    ])

    if DRY_RUN:
        print(f"[DRY] would send: {subject}")
        return

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = FORWARD_TO

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)
    print(f"Sent: {subject}")


def main() -> None:
    state = load_state()
    seen = set(state.get("processed_ids", []))
    tweets = fetch_tweets()
    print(f"Fetched {len(tweets)} tweets from profile")
    repost_count = sum(1 for t in tweets if t["isRepost"])
    gh_count = sum(1 for t in tweets if t["githubMatches"])
    print(f"  reposts detected: {repost_count}, with github URL: {gh_count}")
    if DEBUG:
        for t in tweets:
            print(f'  tweet {t["id"]} repost={t["isRepost"]} ctx="{t["ctxText"]}" gh={len(t["githubMatches"])}')

    candidates = [t for t in tweets if t["isRepost"] and t["githubMatches"] and t["id"] not in seen]
    print(f"New GitHub-containing reposts: {len(candidates)}")

    for t in candidates:
        seen_repos: dict[str, None] = {}
        for u in t["githubMatches"]:
            n = normalize_gh_url(u)
            if n and n not in seen_repos:
                seen_repos[n] = None
        repos = list(seen_repos)
        if not repos:
            continue
        try:
            send_one(t, repos)
            state["processed_ids"].append(t["id"])
        except Exception as e:
            print(f"Failed {t['id']}: {e}", file=sys.stderr)

    save_state(state)
    print("Done.")


if __name__ == "__main__":
    main()
