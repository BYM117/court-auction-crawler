#!/bin/zsh
set -eu

SCRIPT_DIR="${0:A:h}"
PROJECT_ROOT="${COURT_AUCTION_WORKSPACE:-${SCRIPT_DIR:h}}"
cd "$PROJECT_ROOT"

export PYTHONUNBUFFERED=1
export PYTHONPATH=src

exec .venv/bin/python -m court_auction_crawler.cli serve \
  --db data/auction.sqlite3 \
  --host 127.0.0.1 \
  --port 8000
