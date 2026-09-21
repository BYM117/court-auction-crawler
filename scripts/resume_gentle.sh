#!/bin/zsh
# 차단 회복용 '얌전한' 상세 수집 재개. 워커 1·지연 5 로 시작한다(평소 워커 2·지연 3).
#
# **collect(목록 수집) 데몬은 절대 건드리지 않는다.** 2026-09-22 차단은 collect 와
# 상세를 동시에 재시작해 부하가 튄 것이었다(DEVLOG·CRAWL-LOAD). 여기서는 상세만 켠다.
#
# 워커 2 로 올리는 것은 사람이 거절률을 보고 따로 한다(자동 상향 금지):
#   print -l 'DETAIL_WORKERS=2' 'DETAIL_DELAY=3.0' > data/detail_pace.env
#   launchctl kickstart -k gui/$(id -u)/com.court-auction.collect-details
set -eu

SCRIPT_DIR="${0:A:h}"
PROJECT_ROOT="${SCRIPT_DIR:h}"
cd "$PROJECT_ROOT"
uid=$(id -u)
JOB="gui/$uid/com.court-auction.collect-details"

# 얌전한 속도를 상태파일에 박는다 — KeepAlive·자가재시작을 거쳐도 유지된다.
mkdir -p data
print -l 'DETAIL_WORKERS=1' 'DETAIL_DELAY=5.0' > data/detail_pace.env
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 얌전한 속도 설정: 워커 1·지연 5초 (data/detail_pace.env)"

# bootout 으로 빠져 있으면 되살리고(bootstrap), 이미 떠 있으면 새 속도로 재적재(kickstart).
if launchctl print "$JOB" >/dev/null 2>&1; then
  launchctl kickstart -k "$JOB"
  echo "상세 수집기 재시작(kickstart) — 이미 등록돼 있었다."
else
  launchctl bootstrap "gui/$uid" "$HOME/Library/LaunchAgents/com.court-auction.collect-details.plist"
  echo "상세 수집기 되살림(bootstrap) — 정지 상태에서 복귀."
fi

echo "재개함. 15~20분 뒤 거절률을 확인할 것:"
echo "  grep -c '세션 거절' logs/collect-details.log   # 성공(수집 완료) 대비 비율을 본다"
echo "  거절률이 5% 넘으면 아직 차단이 남은 것 — 더 식힌다. collect 는 건드리지 않는다."
