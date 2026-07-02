#!/bin/zsh
set -eu

SCRIPT_DIR="${0:A:h}"
PROJECT_ROOT="${COURT_AUCTION_WORKSPACE:-${SCRIPT_DIR:h}}"
cd "$PROJECT_ROOT"

export PYTHONUNBUFFERED=1
export PYTHONPATH=src
export PLAYWRIGHT_BROWSERS_PATH=.playwright-browsers

CURRENT_START_DATE="${COURT_AUCTION_CURRENT_START_DATE:-$(date -v-365d +%Y-%m-%d)}"
CURRENT_END_DATE="${COURT_AUCTION_CURRENT_END_DATE:-$(date -v+365d +%Y-%m-%d)}"
SCHEDULED_START_DATE="${COURT_AUCTION_SCHEDULED_START_DATE:-$(date +%Y-%m-%d)}"
SCHEDULED_END_DATE="${COURT_AUCTION_SCHEDULED_END_DATE:-$(date -v+180d +%Y-%m-%d)}"

exec .venv/bin/python -m court_auction_crawler.cli collect-all \
  --db data/auction.sqlite3 \
  --mode both \
  --current-start-date "$CURRENT_START_DATE" \
  --current-end-date "$CURRENT_END_DATE" \
  --scheduled-start-date "$SCHEDULED_START_DATE" \
  --scheduled-end-date "$SCHEDULED_END_DATE" \
  --date-chunk-days 14 \
  --max-pages 50 \
  --delay 2.0
