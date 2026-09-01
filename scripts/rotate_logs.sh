#!/bin/zsh
set -u

# 로그를 날짜별로 잘라 logs/archive/ 에 보관한다.
#
# 아무것도 지우지 않는다. 잘라낸 내용은 전부 archive 로 옮겨지고,
# 살아 있는 로그 파일만 0바이트로 비워진다. 과거 기록은 영구 보존한다.
#
# 왜 이렇게 하나:
#   수집기는 launchd 가 열어준 파일 하나에 계속 이어 쓴다(>> 추가 모드).
#   파일을 mv 하면 수집기는 옮겨간 파일에 계속 쓰기 때문에 소용이 없다.
#   그래서 "내용을 복사해두고 원본을 비우는" 방식을 쓴다. 추가 모드라
#   비운 직후부터 다시 0번지에 쌓이므로 수집기를 재시작할 필요가 없다.
#
# 복사와 비우기 사이에 새 줄이 들어오면 그 줄을 잃을 수 있다. 그래서 먼저
# "아무도 안 쓰는 순간"을 기다렸다가 자른다. 수집기는 사이클 사이에 쉬는
# 구간이 있어서(목록 3시간, 상세 1분) 대개 금방 조용해진다. 조용할 때 자르면
# 손실 가능성이 아예 0이다. 끝내 조용해지지 않으면 2단계 복사로 물러선다.

SCRIPT_DIR="${0:A:h}"
PROJECT_ROOT="${COURT_AUCTION_WORKSPACE:-${SCRIPT_DIR:h}}"
cd "$PROJECT_ROOT" || exit 1

ARCHIVE_DIR="logs/archive"
mkdir -p "$ARCHIVE_DIR"

# 잘라낸 파일의 이름에 붙일 날짜. 매일 00:05 에 도는 것을 전제로,
# 방금 끝난 하루(어제)를 가리킨다.
STAMP="$(date -v-1d +%Y-%m-%d)"
NOW="$(date '+%Y-%m-%d %H:%M:%S')"
HISTORY="$ARCHIVE_DIR/rotate-history.log"

# 파일이 이만큼(초) 손대지지 않았으면 "지금은 아무도 안 쓴다"고 본다.
QUIET_SECONDS=10
# 조용해질 때까지 최대 이만큼(초) 기다린다. 넘으면 그냥 자른다.
MAX_WAIT=1800

# 파일이 조용해질 때까지 기다린다. 조용해지면 0, 시간 초과면 1.
wait_until_quiet() {
  local f=$1 waited=0 age
  while (( waited < MAX_WAIT )); do
    age=$(( $(date +%s) - $(stat -f%m "$f") ))
    (( age >= QUIET_SECONDS )) && return 0
    sleep 5
    (( waited += 5 ))
  done
  return 1
}

# 지금 돌고 있는 작업들이 쓰는 로그만 다룬다. 과거에 한 번 쓰고 만
# geocode-*.log, push-*.log 같은 것은 건드리지 않는다.
LOGS=(
  logs/collect-all.log
  logs/collect-all.err.log
  logs/collect-details.log
  logs/collect-details.err.log
  logs/server.log
  logs/server.err.log
)

rotated=0
for f in "${LOGS[@]}"; do
  [[ -f "$f" ]] || continue

  size1=$(stat -f%z "$f")
  (( size1 > 0 )) || continue

  # 아무도 안 쓰는 순간을 기다린다.
  if wait_until_quiet "$f"; then
    how="조용할 때 잘랐음(손실 0 보장)"
  else
    how="쓰는 중에 잘랐음(2단계 복사)"
  fi

  base="${f:t:r}"                       # logs/collect-all.log -> collect-all
  dest="$ARCHIVE_DIR/${base}-${STAMP}.log"

  # 이 조각이 언제 잘렸는지 파일 안에 남긴다. 나중에 이 archive 를 열었을 때
  # 어디까지가 언제 것인지 헷갈리지 않게 하려는 것.
  print -r -- "===== [로그 분리] $NOW 까지의 기록 =====" >> "$dest"

  # 1단계: 지금까지 쌓인 만큼(size1)을 복사한다. 여기가 오래 걸리는 부분.
  head -c "$size1" "$f" >> "$dest"

  # 2단계: 1단계가 도는 동안 새로 들어온 줄이 있으면 그것도 마저 가져온다.
  size2=$(stat -f%z "$f")
  if (( size2 > size1 )); then
    tail -c "+$(( size1 + 1 ))" "$f" | head -c "$(( size2 - size1 ))" >> "$dest"
  fi

  # 원본을 비운다. 추가 모드라 수집기는 그대로 이어서 쓴다.
  : > "$f"

  print -r -- "$NOW  $f  ->  $dest  ($(( size2 / 1024 ))KB)  $how" >> "$HISTORY"
  print -r -- "$f -> $dest ($(( size2 / 1024 ))KB)"
  (( rotated += 1 ))
done

print -r -- "분리한 로그: ${rotated}개"
