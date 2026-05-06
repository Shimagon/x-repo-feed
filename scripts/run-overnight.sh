#!/usr/bin/env bash
# One-shot overnight pipeline: deep_crawl → ingest.
# Wrapped with caffeinate -i so the Mac stays awake while running
# (lid must be open — closing the lid causes clamshell sleep regardless).
set -uo pipefail

cd "$(dirname "$0")/.."
mkdir -p "${KNOWLEDGE_DIR:-/Users/taisei/Documents/課題/リポジトリ検証場所/.knowledge}/audit"

# Load .env
set -a
. ./.env
set +a

LOG_DIR="${KNOWLEDGE_DIR}/audit"
STAMP="$(date +%Y%m%d_%H%M%S)"
MAIN_LOG="${LOG_DIR}/overnight_${STAMP}.log"

echo "[$(date -Iseconds)] === overnight run start ===" >> "$MAIN_LOG"
echo "[$(date -Iseconds)] env: X_USERNAME=$X_USERNAME, KNOWLEDGE_DIR=$KNOWLEDGE_DIR" >> "$MAIN_LOG"

# 1) Deep crawl — 30 min budget, plenty of scrolls + status visits
echo "[$(date -Iseconds)] phase 1: deep_crawl" | tee -a "$MAIN_LOG"
DEEP_TIME_LIMIT_S=1800 \
MAX_PROFILE_SCROLLS=60 \
MAX_STATUS_VISITS=150 \
SCROLL_DELAY_MS=1500 \
python3 -u scripts/deep_crawl.py 2>&1 | tee -a "$MAIN_LOG"
deep_rc=${PIPESTATUS[0]}
echo "[$(date -Iseconds)] deep_crawl exit code: $deep_rc" >> "$MAIN_LOG"

if [ "$deep_rc" -ne 0 ]; then
  echo "[$(date -Iseconds)] deep_crawl failed — aborting overnight run" | tee -a "$MAIN_LOG"
  exit "$deep_rc"
fi

# 2) Ingest — 60 min budget
echo "[$(date -Iseconds)] phase 2: ingest" | tee -a "$MAIN_LOG"
INGEST_TIME_LIMIT_S=3600 \
MAX_REPOS_PER_RUN=300 \
MAX_ARTICLES_PER_RUN=200 \
CLONE_TIMEOUT_S=120 \
SCRAPE_TIMEOUT_S=45 \
python3 -u scripts/ingest.py 2>&1 | tee -a "$MAIN_LOG"
ing_rc=${PIPESTATUS[0]}
echo "[$(date -Iseconds)] ingest exit code: $ing_rc" >> "$MAIN_LOG"

# Summary
echo "[$(date -Iseconds)] === overnight summary ===" | tee -a "$MAIN_LOG"
echo "manifest lines: $(wc -l < "${KNOWLEDGE_DIR}/manifest.jsonl")" | tee -a "$MAIN_LOG"
echo "repos cloned: $(find "${KNOWLEDGE_DIR}/repos" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l)" | tee -a "$MAIN_LOG"
echo "articles scraped: $(find "${KNOWLEDGE_DIR}/articles" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l)" | tee -a "$MAIN_LOG"
echo "[$(date -Iseconds)] === overnight run done ===" | tee -a "$MAIN_LOG"
