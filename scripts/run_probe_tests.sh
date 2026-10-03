#!/bin/zsh
# 차단 시험: scripts/probe_block.py 를 정해진 차례로 30분 간격으로 돈다. 막히면 그 탐침은 끝낸다(다시 안 들어감).
# 사용: zsh scripts/run_probe_tests.sh [시작 HHMM] [차례]
#   차례 예: "near:off near:on near:off near:on"(10-03 인근매각 시험) · "stage:tabs stage:item stage:full ..."(10-04 단계 시험)
cd "$(dirname "$0")/.." || exit 1
export PYTHONPATH=src PLAYWRIGHT_BROWSERS_PATH=.playwright-browsers PYTHONUNBUFFERED=1
START="${1:-0000}"
PLAN="${2:-near:off near:on near:off near:on}"
mkdir -p logs/probe-tests
until [ "$(date +%H%M)" -ge "$START" ]; do sleep 60; done
for step in ${=PLAN}; do
  kind=${step%%:*}; value=${step#*:}
  if [ "$kind" = near ]; then args=(--near "$value"); else args=(--near off --stage "$value"); fi
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 탐침 시작 — $step" >> logs/probe-tests/run.log
  .venv/bin/python scripts/probe_block.py "${args[@]}" --max 40 >> logs/probe-tests/run.log 2>&1
  sleep 1800
done
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 시험 끝" >> logs/probe-tests/run.log
