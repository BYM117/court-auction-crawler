#!/bin/zsh
# 배당요구종기공고 수집 (G15). 하루 한 번이면 충분하다 — 개시결정 후 몇 달짜리
# 일정이라 3시간 사이클에 얹을 이유가 없고, 법원 60곳 × 경매계라 무겁다.
#
# **놓치면 영영 못 얻는다.** 배당요구종기일이 지나면 목록에서 빠진다(실측: 서울중앙
# 376건 중 종기일이 지난 것 0건). 낙찰가·법원문서와 같은 성격이다.
cd "$(dirname "$0")/.." || exit 1
export PYTHONPATH=src
export PLAYWRIGHT_BROWSERS_PATH=.playwright-browsers
mkdir -p logs
echo "===== 배당요구종기공고 수집 시작 $(date '+%Y-%m-%d %H:%M:%S') =====" >> logs/notices.log
.venv/bin/python -m court_auction_crawler.cli collect-notices >> logs/notices.log 2>> logs/notices.err.log
echo "===== 끝 $(date '+%Y-%m-%d %H:%M:%S') =====" >> logs/notices.log
