#!/bin/zsh
set -eu

SCRIPT_DIR="${0:A:h}"
PROJECT_ROOT="${COURT_AUCTION_WORKSPACE:-${SCRIPT_DIR:h}}"
cd "$PROJECT_ROOT"

export PYTHONUNBUFFERED=1
export PYTHONPATH=src
export PLAYWRIGHT_BROWSERS_PATH=.playwright-browsers

# 수집 속도(워커·지연)는 상태파일로 조절한다. 없으면 안전 기본값(워커 2·지연 3).
# 차단에서 회복할 때 얌전히(워커 1·지연 5) 시작하려고 둔 것 — scripts/resume_gentle.sh
# 가 이 파일을 쓴다. data/ 는 gitignore 라 코드가 아니라 런타임 상태다. 상태파일이
# 얌전한 값을 담고 있으면 KeepAlive·자가재시작을 거쳐도 그대로 유지된다.
PACE_FILE="$PROJECT_ROOT/data/detail_pace.env"
[ -f "$PACE_FILE" ] && source "$PACE_FILE"
WORKERS="${DETAIL_WORKERS:-2}"
DELAY="${DETAIL_DELAY:-3.0}"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 상세 수집 시작 — 워커 $WORKERS · 지연 ${DELAY}초"

# ⚠ 주석을 exec 의 인자 줄 사이에 끼우지 말 것. `\` 로 이어지는 명령 중간의 `#` 은
#   그 뒤 인자를 통째로 잘라먹는다. 2026-09-20 에 이렇게 망가져 --workers·--delay·
#   --loop 이 전부 안 먹었다(데몬은 기본값으로 돌았다). 주석은 여기 위에만 둔다.
#
# 설정 근거(CRAWL-LOAD.md):
#   --workers  법원이 세션을 거절하면 동시성을 낮춘다. 요청 간격(delay)이 아니라
#              동시 세션 수가 지렛대다. 상한 3. 차단 회복 중엔 1.
#   --delay    사건 사이 대기. 영구 차단이 가장 큰 위험이라 조금 느려도 안전하게. 하한 3.
#   --loop     백로그를 다 비워도 종료하지 않고 신건·재시도를 계속 수집한다.
#
# 단일 실행 보장은 CLI 가 data/collect-details.pid 락으로 처리한다.
exec .venv/bin/python -m court_auction_crawler.cli collect-details \
  --db data/auction.sqlite3 \
  --asset-dir data/auction-assets \
  --workers "$WORKERS" \
  --delay "$DELAY" \
  --loop
