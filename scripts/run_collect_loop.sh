#!/bin/zsh
set -u

SCRIPT_DIR="${0:A:h}"
PROJECT_ROOT="${COURT_AUCTION_WORKSPACE:-${SCRIPT_DIR:h}}"
cd "$PROJECT_ROOT"

INTERVAL_SECONDS="${COURT_AUCTION_INTERVAL_SECONDS:-10800}"
mkdir -p logs

while true; do
  : > logs/collect-all.err.log
  echo "===== 자동 수집 시작 $(date '+%Y-%m-%d %H:%M:%S') =====" >> logs/collect-all.log
  scripts/run_collect_all.sh >> logs/collect-all.log 2>> logs/collect-all.err.log
  exit_code=$?
  echo "===== 자동 수집 종료 $(date '+%Y-%m-%d %H:%M:%S') exit=${exit_code}; ${INTERVAL_SECONDS}초 후 재시작 =====" >> logs/collect-all.log
  sleep "$INTERVAL_SECONDS"
done
