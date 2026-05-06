"""
Read manifest.jsonl produced by deep_crawl.py and:
  - For each unique GitHub URL: shallow-clone into $KNOWLEDGE_DIR/repos/<owner_repo>/repo/
                                emit summary.md (lang, stars, pushed_at, README first para)
                                emit source.md (the X tweet text + URL)
  - For each unique non-github URL in tweet text: scrape with Scrapling get + main_content_only
                                save to $KNOWLEDGE_DIR/articles/<slug>/source.md + summary.md

Hard safety bounds:
  - Wall-clock cap: INGEST_TIME_LIMIT_S (default 1800)
  - Repos per run cap: MAX_REPOS_PER_RUN (default 200)
  - Articles per run cap: MAX_ARTICLES_PER_RUN (default 200)
  - Skip if target dir already exists (idempotent)
  - shallow clone --depth=1 only
  - Per-clone timeout 90s
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

KNOWLEDGE_DIR = pathlib.Path(os.environ.get("KNOWLEDGE_DIR", ".knowledge"))
MANIFEST = KNOWLEDGE_DIR / "manifest.jsonl"
REPOS_DIR = KNOWLEDGE_DIR / "repos"
ARTICLES_DIR = KNOWLEDGE_DIR / "articles"
AUDIT = KNOWLEDGE_DIR / "audit" / f"ingest_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

INGEST_TIME_LIMIT_S = int(os.environ.get("INGEST_TIME_LIMIT_S", "1800"))
MAX_REPOS_PER_RUN = int(os.environ.get("MAX_REPOS_PER_RUN", "200"))
MAX_ARTICLES_PER_RUN = int(os.environ.get("MAX_ARTICLES_PER_RUN", "200"))
CLONE_TIMEOUT_S = int(os.environ.get("CLONE_TIMEOUT_S", "90"))
SCRAPE_TIMEOUT_S = int(os.environ.get("SCRAPE_TIMEOUT_S", "45"))
DRY_RUN = os.environ.get("DRY_RUN") == "1"

T_START = time.time()
URL_RE = re.compile(r"https?://[^\s)]+", re.IGNORECASE)
SKIP_HOSTS = {
    "github.com", "x.com", "twitter.com", "t.co", "youtube.com", "youtu.be",
    "google.com", "image.twimg.com", "pbs.twimg.com",
}

REPOS_DIR.mkdir(parents=True, exist_ok=True)
ARTICLES_DIR.mkdir(parents=True, exist_ok=True)
AUDIT.parent.mkdir(parents=True, exist_ok=True)
_audit_fh = AUDIT.open("a", encoding="utf-8")


def log(msg: str) -> None:
    line = f"[{dt.datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    _audit_fh.write(line + "\n")
    _audit_fh.flush()


def time_left() -> float:
    return INGEST_TIME_LIMIT_S - (time.time() - T_START)


def slug_from_url(u: str) -> str:
    p = urllib.parse.urlparse(u)
    s = (p.netloc + p.path).strip("/").replace("/", "_")
    s = re.sub(r"[^A-Za-z0-9._-]", "_", s)
    return s[:120] or "article"


def gh_slug(u: str) -> str | None:
    m = re.match(r"https?://github\.com/([\w.-]+)/([\w.-]+)", u, re.IGNORECASE)
    if not m:
        return None
    return f"{m.group(1).lower()}-{m.group(2).lower()}"


def fetch_gh_meta(owner_repo: str) -> dict:
    """Public GitHub REST, no auth (60 req/hr is plenty)."""
    out: dict = {}
    try:
        with urllib.request.urlopen(f"https://api.github.com/repos/{owner_repo}", timeout=20) as r:
            data = json.loads(r.read())
            for k in ("description", "language", "stargazers_count", "pushed_at", "homepage", "full_name", "default_branch", "topics", "license"):
                out[k] = data.get(k)
    except Exception as e:
        out["_meta_error"] = str(e)
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{owner_repo}/readme",
            headers={"Accept": "application/vnd.github.raw"},
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            text = r.read().decode("utf-8", errors="replace")
            paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
            for p in paragraphs:
                if not re.match(r"^[<\!\[#\|`>]", p):
                    p = re.sub(r"\s+", " ", p)
                    out["readme_para"] = p[:600]
                    break
    except Exception as e:
        out["_readme_error"] = str(e)
    return out


def write_repo_summary(target_dir: pathlib.Path, owner_repo: str, meta: dict, source_tweets: list[dict]) -> None:
    summary_path = target_dir / "summary.md"
    src_path = target_dir / "source.md"
    badges = []
    if meta.get("language"): badges.append(f"`{meta['language']}`")
    if meta.get("stargazers_count") is not None: badges.append(f"⭐ {meta['stargazers_count']}")
    if meta.get("pushed_at"): badges.append(f"updated {meta['pushed_at'][:10]}")
    if meta.get("homepage"): badges.append(f"home: {meta['homepage']}")

    desc = meta.get("description") or "(no description)"
    para = meta.get("readme_para") or "(no readme paragraph extracted)"
    topics = ", ".join(meta.get("topics") or []) or "—"

    summary = "\n".join([
        f"# {owner_repo}",
        "",
        " · ".join(badges) if badges else "",
        "",
        f"**Description**: {desc}",
        "",
        f"**Topics**: {topics}",
        "",
        "## README first paragraph",
        "",
        para,
        "",
        f"_Discovered via @{source_tweets[0].get('author_handle') or '?'} repost — `tweet_id={source_tweets[0]['tweet_id']}`_" if source_tweets else "",
    ]).strip() + "\n"

    src_lines = [f"# Source for {owner_repo}", "", "## X tweets that surfaced this repo", ""]
    for t in source_tweets:
        src_lines += [
            f"### tweet_id={t['tweet_id']} ({t.get('source','?')})",
            f"https://x.com/i/status/{t['tweet_id']}",
            "",
            "```",
            (t.get("tweet_text") or "(no text)").strip(),
            "```",
            "",
        ]
    src_lines += [
        "## Raw GitHub metadata",
        "",
        "```json",
        json.dumps(meta, indent=2, ensure_ascii=False),
        "```",
        "",
    ]

    if DRY_RUN:
        log(f"[DRY] would write {summary_path} + {src_path}")
        return
    summary_path.write_text(summary, encoding="utf-8")
    src_path.write_text("\n".join(src_lines) + "\n", encoding="utf-8")


def clone_repo(url: str, target_dir: pathlib.Path) -> bool:
    """git clone --depth 1 with timeout."""
    repo_path = target_dir / "repo"
    if repo_path.exists():
        log(f"  skip clone (exists): {repo_path}")
        return True
    if DRY_RUN:
        log(f"[DRY] would clone {url} -> {repo_path}")
        return True
    target_dir.mkdir(parents=True, exist_ok=True)
    log(f"  cloning {url}")
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", "--quiet", url, str(repo_path)],
            check=True, timeout=CLONE_TIMEOUT_S, capture_output=True,
        )
        return True
    except subprocess.CalledProcessError as e:
        log(f"  clone failed: {e.stderr.decode('utf-8', errors='replace')[:200]}")
    except subprocess.TimeoutExpired:
        log(f"  clone timeout ({CLONE_TIMEOUT_S}s)")
    return False


def scrape_article(url: str, target_dir: pathlib.Path) -> bool:
    if (target_dir / "source.md").exists():
        log(f"  skip scrape (exists): {target_dir}")
        return True
    target_dir.mkdir(parents=True, exist_ok=True)
    if DRY_RUN:
        log(f"[DRY] would scrape {url} -> {target_dir}")
        return True
    try:
        from scrapling.fetchers import Fetcher
        page = Fetcher.get(
            url,
            timeout=SCRAPE_TIMEOUT_S,
            stealthy_headers=True,
            follow_redirects=True,
        )
        if page.status >= 400:
            log(f"  scrape http={page.status} {url}")
            return False
        title_el = page.css("title::text").get() or page.css("h1::text").get() or ""
        title = (title_el or "").strip()[:200]
        body = page.get_all_text() or ""
        body = re.sub(r"\n{3,}", "\n\n", body).strip()
        body = body[:30000]
        (target_dir / "source.md").write_text(
            f"# {title or url}\n\nSource URL: {url}\n\n---\n\n{body}\n",
            encoding="utf-8",
        )
        (target_dir / "summary.md").write_text(
            f"# {title or url}\n\n**URL**: {url}\n\n**Captured**: {dt.datetime.now(dt.UTC).isoformat()}\n\n"
            f"**First lines**:\n\n{(body[:600] or '(empty)')}…\n",
            encoding="utf-8",
        )
        return True
    except Exception as e:
        log(f"  scrape error: {e}")
        return False


def main() -> int:
    log(f"=== ingest start (limit {INGEST_TIME_LIMIT_S}s, repos<={MAX_REPOS_PER_RUN}, articles<={MAX_ARTICLES_PER_RUN}) ===")
    if not MANIFEST.exists():
        log(f"manifest not found: {MANIFEST}")
        return 0
    rows = [json.loads(l) for l in MANIFEST.read_text(encoding="utf-8").splitlines() if l.strip()]
    log(f"manifest rows: {len(rows)}")

    repo_to_tweets: dict[str, list[dict]] = {}
    article_urls: dict[str, list[dict]] = {}
    for r in rows:
        for u in r.get("gh_urls", []):
            slug = gh_slug(u)
            if slug:
                repo_to_tweets.setdefault(u, []).append(r)
        for u in URL_RE.findall(r.get("tweet_text", "")):
            u = u.rstrip(".,;:!?)\\]}\"'")
            host = urllib.parse.urlparse(u).netloc.lower()
            if any(host.endswith(h) for h in SKIP_HOSTS):
                continue
            article_urls.setdefault(u, []).append(r)

    log(f"unique repos to ingest: {len(repo_to_tweets)}")
    log(f"unique articles to ingest: {len(article_urls)}")

    repos_done, repos_skip, repos_fail = 0, 0, 0
    for url, tweets in repo_to_tweets.items():
        if repos_done + repos_skip + repos_fail >= MAX_REPOS_PER_RUN:
            log("repo cap hit"); break
        if time_left() < 60:
            log("time low — stopping repo ingest"); break
        slug = gh_slug(url)
        owner_repo = "/".join(slug.replace("-", "/", 1).split("-", 1)) if "-" in slug else slug
        # safer: use original url path
        m = re.match(r"https?://github\.com/([\w.-]+)/([\w.-]+)", url, re.IGNORECASE)
        owner_repo = f"{m.group(1)}/{m.group(2)}" if m else slug
        target = REPOS_DIR / slug
        if (target / "summary.md").exists() and (target / "repo").exists():
            repos_skip += 1
            continue
        log(f"REPO {owner_repo}")
        meta = fetch_gh_meta(owner_repo)
        if "full_name" in meta and meta["full_name"]:
            real_url = f"https://github.com/{meta['full_name']}"
        else:
            real_url = url
        ok = clone_repo(real_url, target)
        write_repo_summary(target, meta.get("full_name") or owner_repo, meta, tweets)
        if ok:
            repos_done += 1
        else:
            repos_fail += 1

    log(f"repos: done={repos_done} skip={repos_skip} fail={repos_fail}")

    art_done, art_skip, art_fail = 0, 0, 0
    for url, tweets in article_urls.items():
        if art_done + art_skip + art_fail >= MAX_ARTICLES_PER_RUN:
            log("article cap hit"); break
        if time_left() < 60:
            log("time low — stopping article ingest"); break
        slug = slug_from_url(url)
        target = ARTICLES_DIR / slug
        if (target / "source.md").exists():
            art_skip += 1
            continue
        log(f"ARTICLE {url}")
        if scrape_article(url, target):
            art_done += 1
        else:
            art_fail += 1

    log(f"articles: done={art_done} skip={art_skip} fail={art_fail}")
    log(f"=== ingest done ({int(time.time()-T_START)}s) ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
