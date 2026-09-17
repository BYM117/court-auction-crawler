#!/bin/zsh
# 놓친 매각결과를 사건 화면의 '기일 내역'으로 메운다.
#
# 매각결과 화면은 기일 다음날부터 이레만 보여주지만 사건 화면에는 물건별 기일결과가
# 금액까지 영구히 남는다. 그 창이 닫힌 뒤에 남은 것을 사건 단위로 훑는다.
#
# 상세 수집 데몬과 같은 락(data/collect-details.pid)을 쓰므로 데몬을 잠시 내린다.
# 어떻게 끝나든(실패·강제종료 포함) 반드시 되돌린다.
#
# 사용: scripts/run_backfill_results.sh [돌릴 시간(분), 기본 430]
set -u

SCRIPT_DIR="${0:A:h}"
PROJECT_ROOT="${COURT_AUCTION_WORKSPACE:-${SCRIPT_DIR:h}}"
cd "$PROJECT_ROOT"

MINUTES="${1:-430}"
LABEL="gui/$(id -u)/com.court-auction.collect-details"
PLIST="$HOME/Library/LaunchAgents/com.court-auction.collect-details.plist"

restore() {
  launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null
  print "[$(date '+%m-%d %H:%M')] 상세 수집 데몬 복구"
}
trap restore EXIT INT TERM

print "[$(date '+%m-%d %H:%M')] 상세 수집 데몬 내림 — 매각결과 보충 ${MINUTES}분"
launchctl bootout "$LABEL" 2>/dev/null
/bin/sleep 5

export PYTHONUNBUFFERED=1
export PYTHONPATH=src
export PLAYWRIGHT_BROWSERS_PATH=.playwright-browsers

# 사이트가 차단하면 수집기가 스스로 패스를 접는다. 냉각하고 새 브라우저로 이어간다.
END=$(( $(date +%s) + MINUTES * 60 ))
PASS=0
while (( $(date +%s) < END )); do
  PASS=$((PASS + 1))
  print "\n===== 패스 $PASS  $(date '+%m-%d %H:%M') ====="
  .venv/bin/python -m court_auction_crawler.cli collect-details \
    --db data/auction.sqlite3 \
    --asset-dir data/auction-assets \
    --backfill-results --limit 1000 --delay 2.0 --skip-documents \
    || print "  !! 패스 $PASS 실패 — 다음 패스로 계속"
  (( $(date +%s) < END )) && /bin/sleep 180
done

print "\n[$(date '+%m-%d %H:%M')] 마감 — 총 $PASS 패스"
