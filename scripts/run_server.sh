#!/bin/zsh
set -eu

cd "${COURT_AUCTION_WORKSPACE:-/Users/bym/Documents/경매물건 크롤링}"

export PYTHONUNBUFFERED=1
export PYTHONPATH=src

exec .venv/bin/python -m court_auction_crawler.cli serve \
  --db data/auction.sqlite3 \
  --host 127.0.0.1 \
  --port 8000
