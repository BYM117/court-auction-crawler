#!/bin/zsh
set -u

SCRIPT_DIR="${0:A:h}"
PROJECT_ROOT="${COURT_AUCTION_WORKSPACE:-${SCRIPT_DIR:h}}"
cd "$PROJECT_ROOT"

export PYTHONUNBUFFERED=1
export PYTHONPATH=src
export PLAYWRIGHT_BROWSERS_PATH=.playwright-browsers

# 목록 수집 상시 데몬. collector.enabled가 켜져 있을 때만 수집하고,
# 3시간(quick)/24시간(full) 주기를 자동 판단한다. 단일 실행 보장은 CLI가
# data/collect-all.pid 락으로 처리하고, 연속 실패 시 스스로 종료해 launchd가
# 깨끗하게 되살린다.
#
# --push-dest: 사이클마다 바뀐 물건·사진을 R2로 올린다(맥은 내보내기만 하고
# 외부 요청은 받지 않는다). .env에 R2 키가 없으면 조용히 건너뛴다.
PUSH_DEST="${COURT_AUCTION_PUSH_DEST:-s3://court-auction}"

exec .venv/bin/python -m court_auction_crawler.cli collect-loop \
  --db data/auction.sqlite3 \
  --geocode-limit 2000 \
  --push-dest "$PUSH_DEST"
