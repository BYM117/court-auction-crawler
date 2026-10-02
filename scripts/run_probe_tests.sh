#!/bin/zsh
# 차단 시험: 인근매각 버튼 끔/켬을 번갈아 탐침으로 잰다(scripts/probe_block.py). 막히면 그 탐침은 끝낸다.
# 사용: zsh scripts/run_probe_tests.sh [시작 HHMM] — 그 시각까지 기다렸다가 끔·켬·끔·켬을 30분 간격으로 돈다.
cd "$(dirname "$0")/.." || exit 1
export PYTHONPATH=src PLAYWRIGHT_BROWSERS_PATH=.playwright-browsers PYTHONUNBUFFERED=1
START="${1:-0000}"
until [ "$(date +%H%M)" -ge "$START" ]; do sleep 60; done
for near in off on off on; do
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 탐침 시작 — 인근매각 $near" >> logs/probe-tests/run.log
  .venv/bin/python scripts/probe_block.py --near "$near" --max 60 >> logs/probe-tests/run.log 2>&1
  sleep 1800
done
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 시험 끝" >> logs/probe-tests/run.log
