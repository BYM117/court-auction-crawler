#!/bin/zsh
set -eu

SCRIPT_DIR="${0:A:h}"
PROJECT_ROOT="${COURT_AUCTION_WORKSPACE:-${SCRIPT_DIR:h}}"
cd "$PROJECT_ROOT"

export PYTHONUNBUFFERED=1
export PYTHONPATH=src
export PLAYWRIGHT_BROWSERS_PATH=.playwright-browsers

# 단일 실행 보장은 CLI가 data/collect-details.pid 락으로 처리한다.
# --loop: 백로그를 다 비워도 종료하지 않고 신건·재시도 도래분을 계속 수집한다.
exec .venv/bin/python -m court_auction_crawler.cli collect-details \
  --db data/auction.sqlite3 \
  --asset-dir data/auction-assets \
  # 2026-09-20: 2.0 -> 3.0. 감정평가서 상태 교정으로 3만 건이 한꺼번에 큐에
  # 들어가자 법원이 세션을 거절하기 시작했다(거절 2% -> 11%, 차단 의심 66 -> 212회).
  # **영구 차단이 가장 큰 사업 위험이다.** 조금 느려도 거절을 줄이는 쪽이 낫다.
  # 2026-09-20 23:15: 워커 3 -> 2. 지연 2.0 -> 3.0 은 실패율을 45% -> 44% 로
  # 못 낮췄다(효과 없음). 거절의 76%가 '있는 활성 사건을 없다고 함' = 소프트
  # 차단이고 연속 오류로 몰린다(버스트). 요청 간격이 아니라 **동시성**이 지렛대다.
  --workers 2 \
  --delay 3.0 \
  --loop
