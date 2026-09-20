#!/bin/zsh
# 상세 수집 재개 (2026-09-21 차단 회피로 멈춘 것). 거절률이 진정됐는지 보고 켠다.
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.court-auction.collect-details.plist
echo "상세 수집 재개함. 30분 뒤 거절률을 다시 볼 것:"
echo "  grep -c '세션 거절' logs/collect-details.log"
