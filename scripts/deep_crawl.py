"""
Deep crawl @<X_USERNAME>'s X profile to surface ALL past reposts (not just the
most-recent few). For each repost lacking a github URL in its body, follow the
status URL and inspect OP self-replies for a github URL at the end.

Hard safety bounds (株予測 circuit-breaker style):
  - Wall-clock cap: DEEP_TIME_LIMIT_S (default 1800s)
  - Profile scroll cap: MAX_PROFILE_SCROLLS (default 50)
  - Status pages visited cap: MAX_STATUS_VISITS (default 200)
  - Consecutive failures cap: 3 → abort
  - Idempotent: re-running picks up where it left off via manifest.jsonl

Output: append-only JSONL at $KNOWLEDGE_DIR/manifest.jsonl
  Each line: {"tweet_id","is_repost","gh_urls":[...],"source":"profile|reply",
              "tweet_text","ctx","fetched_at","author_handle"}
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import re
import sys
import time

from scrapling.fetchers import StealthyFetcher

X_USERNAME = os.environ["X_USERNAME"]
X_AUTH_TOKEN = os.environ["X_AUTH_TOKEN"]
X_CT0 = os.environ["X_CT0"]
KNOWLEDGE_DIR = pathlib.Path(os.environ.get("KNOWLEDGE_DIR", ".knowledge"))
MANIFEST = KNOWLEDGE_DIR / "manifest.jsonl"
AUDIT = KNOWLEDGE_DIR / "audit" / f"deep_crawl_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

DEEP_TIME_LIMIT_S = int(os.environ.get("DEEP_TIME_LIMIT_S", "1800"))
MAX_PROFILE_SCROLLS = int(os.environ.get("MAX_PROFILE_SCROLLS", "50"))
MAX_STATUS_VISITS = int(os.environ.get("MAX_STATUS_VISITS", "200"))
SCROLL_DELAY_MS = int(os.environ.get("SCROLL_DELAY_MS", "1500"))
FAIL_THRESHOLD = 3
DRY_RUN = os.environ.get("DRY_RUN") == "1"

COOKIES = [
    {"name": "auth_token", "value": X_AUTH_TOKEN, "domain": ".x.com", "path": "/",
     "httpOnly": True, "secure": True, "sameSite": "None"},
    {"name": "ct0", "value": X_CT0, "domain": ".x.com", "path": "/",
     "httpOnly": False, "secure": True, "sameSite": "Lax"},
]

GH_RE = re.compile(r"https?://github\.com/[\w.-]+/[\w.-]+", re.IGNORECASE)
REPOST_RE = re.compile(r"repost|retweet|リポスト|リツイート", re.IGNORECASE)
STATUS_RE = re.compile(r"/status/(\d+)")
T_START = time.time()

KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
AUDIT.parent.mkdir(parents=True, exist_ok=True)
_audit_fh = AUDIT.open("a", encoding="utf-8")


def log(msg: str) -> None:
    line = f"[{dt.datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    _audit_fh.write(line + "\n")
    _audit_fh.flush()


def time_left() -> float:
    return DEEP_TIME_LIMIT_S - (time.time() - T_START)


def load_seen() -> dict[str, dict]:
    seen: dict[str, dict] = {}
    if MANIFEST.exists():
        for line in MANIFEST.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                seen[row["tweet_id"]] = row
            except Exception:
                pass
    return seen


def append_manifest(row: dict) -> None:
    if DRY_RUN:
        log(f"[DRY] would write {row['tweet_id']} (gh={len(row['gh_urls'])})")
        return
    with MANIFEST.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def normalize_gh(u: str) -> str | None:
    m = re.search(r"github\.com/([\w.-]+)/([\w.-]+)", u, re.IGNORECASE)
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    repo = re.sub(r"\.git$", "", repo, flags=re.IGNORECASE)
    repo = re.sub(r"[.,;:!?\)\]\}]+$", "", repo)
    if owner.lower() in {"i", "search", "settings", "explore", "home", "notifications", "messages"}:
        return None
    return f"https://github.com/{owner}/{repo}"


def make_scroll_action(rounds: int):
    """Scrapling calls page_action synchronously with a sync Playwright Page."""
    def action(page):
        log(f"  scrolling {rounds} times…")
        for i in range(rounds):
            if time_left() < 60:
                log(f"  scroll abort at iter {i} — time budget low")
                return
            try:
                page.evaluate("window.scrollBy(0, 2400)")
                page.wait_for_timeout(SCROLL_DELAY_MS)
            except Exception as e:
                log(f"  scroll iter {i} error: {e}")
                return
    return action


_AUTHOR_FROM_STATUS = re.compile(r"^/([\w.-]+)/status/(\d+)")


def parse_articles(page, source: str) -> list[dict]:
    articles = page.css('article[data-testid="tweet"]')
    out: list[dict] = []
    for a in articles:
        # Prefer extracting (author, tweet_id) from the same /<handle>/status/<id> href
        # because that's the canonical author of the tweet (not the reposter chrome).
        author = ""
        tweet_id = None
        for h in a.css('a[href*="/status/"]::attr(href)'):
            m = _AUTHOR_FROM_STATUS.match(str(h))
            if m:
                author, tweet_id = m.group(1), m.group(2)
                break
        if not tweet_id:
            for h in a.css('a[href*="/status/"]::attr(href)'):
                m = STATUS_RE.search(str(h))
                if m:
                    tweet_id = m.group(1)
                    break
        if not tweet_id:
            continue

        ctx = (a.css('[data-testid="socialContext"]::text').get() or "").strip()
        is_repost = bool(REPOST_RE.search(ctx))

        text_parts = a.css('[data-testid="tweetText"] *::text').getall() or []
        tweet_text = "".join(text_parts).strip() or (a.css('[data-testid="tweetText"]::text').get() or "").strip()

        haystack_pieces = []
        for el in a.css("a"):
            for sel in ("::attr(href)", "::attr(aria-label)", "::text"):
                v = el.css(sel).get()
                if v:
                    haystack_pieces.append(str(v))
        haystack = " ".join(haystack_pieces)
        gh = sorted({normalize_gh(u) for u in GH_RE.findall(haystack) if normalize_gh(u)})
        gh = [u for u in gh if u]

        out.append({
            "tweet_id": tweet_id,
            "is_repost": is_repost,
            "gh_urls": gh,
            "source": source,
            "tweet_text": tweet_text[:600],
            "ctx": ctx,
            "author_handle": author,
            "fetched_at": dt.datetime.now(dt.UTC).isoformat(),
        })
    return out


def fetch_profile_deep(scrolls: int):
    log(f"FETCH profile https://x.com/{X_USERNAME}  (scrolls={scrolls})")
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
        disable_resources=True,
        page_action=make_scroll_action(scrolls),
    )
    if page.status >= 400:
        raise RuntimeError(f"profile status={page.status}")
    return parse_articles(page, "profile")


def fetch_status_thread(tweet_id: str, owner_handle: str):
    url = f"https://x.com/{owner_handle or X_USERNAME}/status/{tweet_id}"
    log(f"FETCH status {url}")
    page = StealthyFetcher.fetch(
        url,
        headless=True,
        network_idle=True,
        wait_selector='article[data-testid="tweet"]',
        wait_selector_state="visible",
        timeout=45000,
        cookies=COOKIES,
        locale="ja-JP",
        google_search=False,
        disable_resources=True,
        page_action=make_scroll_action(3),
    )
    if page.status >= 400:
        raise RuntimeError(f"status {tweet_id} returned {page.status}")
    # Don't filter by OP author — collect ALL github URLs from the thread.
    # The user said the OP usually self-replies with the link, but a co-thread
    # link is also worth ingesting. Better recall, minor false-positive risk.
    return parse_articles(page, "reply")


def main() -> int:
    log(f"=== deep_crawl start (limit {DEEP_TIME_LIMIT_S}s, scrolls<={MAX_PROFILE_SCROLLS}, status<={MAX_STATUS_VISITS}) ===")
    seen = load_seen()
    log(f"manifest preload: {len(seen)} tweets already seen")
    fails = 0

    try:
        items = fetch_profile_deep(MAX_PROFILE_SCROLLS)
    except Exception as e:
        log(f"FATAL profile fetch: {e}")
        return 2

    log(f"profile parsed: {len(items)} articles")
    new_or_updated = 0
    for it in items:
        prev = seen.get(it["tweet_id"])
        if prev and prev.get("gh_urls"):
            continue
        if not prev:
            append_manifest(it)
            seen[it["tweet_id"]] = it
            new_or_updated += 1

    log(f"new from profile: {new_or_updated}")

    candidates = [it for it in items if it["is_repost"] and not it["gh_urls"]]
    log(f"reposts without inline github: {len(candidates)} → will visit status pages (cap {MAX_STATUS_VISITS})")
    visits = 0
    enriched = 0
    for it in candidates:
        if visits >= MAX_STATUS_VISITS or time_left() < 90:
            log(f"status visit budget exhausted (visits={visits}, time_left={int(time_left())}s)")
            break
        if fails >= FAIL_THRESHOLD:
            log(f"abort: {fails} consecutive failures")
            break

        owner = it.get("author_handle") or X_USERNAME
        try:
            self_replies = fetch_status_thread(it["tweet_id"], owner)
            visits += 1
            fails = 0
            gh_in_replies = sorted({u for r in self_replies for u in r["gh_urls"]})
            if gh_in_replies:
                row = dict(it)
                row["gh_urls"] = gh_in_replies
                row["source"] = "reply"
                row["fetched_at"] = dt.datetime.now(dt.UTC).isoformat()
                append_manifest(row)
                enriched += 1
                log(f"  + {it['tweet_id']} enriched with {len(gh_in_replies)} gh url(s) from OP self-reply")
            else:
                log(f"  - {it['tweet_id']} no gh in self-replies")
        except Exception as e:
            fails += 1
            log(f"  ! {it['tweet_id']} status fetch failed ({fails}/{FAIL_THRESHOLD}): {e}")
            time.sleep(5)

    log(f"=== deep_crawl done: profile_new={new_or_updated} reply_enriched={enriched} status_visits={visits} elapsed={int(time.time()-T_START)}s ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
