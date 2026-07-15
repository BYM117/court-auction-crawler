#!/bin/zsh
set -eu

cd "${COURT_AUCTION_WORKSPACE:-/Users/bym/Documents/경매물건 크롤링}"

export PYTHONUNBUFFERED=1
export PYTHONPATH=src
export PLAYWRIGHT_BROWSERS_PATH=.playwright-browsers

# 단일 실행 보장은 CLI가 data/collect-details.pid 락으로 처리한다.
# --loop: 백로그를 다 비워도 종료하지 않고 신건·재시도 도래분을 계속 수집한다.
exec .venv/bin/python -m court_auction_crawler.cli collect-details \
  --db data/auction.sqlite3 \
  --asset-dir data/auction-assets \
  --delay 2.0 \
  --loop
