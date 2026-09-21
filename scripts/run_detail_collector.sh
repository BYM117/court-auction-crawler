#!/bin/zsh
set -eu

SCRIPT_DIR="${0:A:h}"
PROJECT_ROOT="${COURT_AUCTION_WORKSPACE:-${SCRIPT_DIR:h}}"
cd "$PROJECT_ROOT"

export PYTHONUNBUFFERED=1
export PYTHONPATH=src
export PLAYWRIGHT_BROWSERS_PATH=.playwright-browsers

# ⚠ 주석을 exec 의 인자 줄 사이에 끼우지 말 것. `\` 로 이어지는 명령 중간의 `#` 은
#   그 뒤 인자를 통째로 잘라먹는다. 2026-09-20 에 이렇게 망가져 --workers·--delay·
#   --loop 이 전부 안 먹었고(데몬은 기본값 워커 3·지연 1.5·loop 없음으로 돎), 그것도
#   모르고 '지연/워커를 바꿔도 효과 없다' 고 잘못 결론냈다. 주석은 여기 위에만 둔다.
#
# 설정 근거(CRAWL-LOAD.md):
#   --workers 2  법원이 세션을 거절해서 동시성을 3 → 2 로 낮춘다. 요청 간격(delay)이
#                아니라 동시 세션 수가 지렛대다.
#   --delay 3.0  사건 사이 대기. 영구 차단이 가장 큰 위험이라 조금 느려도 안전하게.
#   --loop       백로그를 다 비워도 종료하지 않고 신건·재시도를 계속 수집한다.
#
# 단일 실행 보장은 CLI 가 data/collect-details.pid 락으로 처리한다.
exec .venv/bin/python -m court_auction_crawler.cli collect-details \
  --db data/auction.sqlite3 \
  --asset-dir data/auction-assets \
  --workers 2 \
  --delay 3.0 \
  --loop
