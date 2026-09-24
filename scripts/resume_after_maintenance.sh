#!/bin/zsh
# 법원 사이트 점검이 끝나면 상세 수집기를 자동으로 되살린다.
# launchd(com.court-auction.maintenance-watch)가 30분마다 부른다.
# **data/maintenance_pause 가 있을 때만 일한다** — 없으면 아무것도 안 하고 끝난다.
#
# 점검으로 멈출 때 (2026-09-24 추석 점검에 처음 씀):
#   touch data/maintenance_pause
#   launchctl bootout gui/$(id -u)/com.court-auction.collect-details
# 목록 수집기(collect)는 멈출 필요 없다 — 목록을 못 읽은 사이클은 생명주기 정리를
# 건너뛰고(store.apply_lifecycle(seen=)), 법원과 무관한 R2 푸시는 계속한다.
#
# '끝났다' 는 수집기가 실제로 여는 **앱 주소**로 판단한다. 첫 화면(/)은 점검 중에도
# 200 에 제목까지 멀쩡해 보인다(2026-09-24 실측) — 그걸로 보면 점검 중에 켠다.
# 점검 중엔 /pgj/index.on 이 error.html 로 넘어가며 '시스템 작업' 을 보여준다.
set -u

SCRIPT_DIR="${0:A:h}"
ROOT="${SCRIPT_DIR:h}"
cd "$ROOT"
FLAG=data/maintenance_pause
URL="${COURT_CHECK_URL:-https://www.courtauction.go.kr/pgj/index.on}"
now() { date '+%Y-%m-%d %H:%M:%S'; }

[ -f "$FLAG" ] || exit 0

body=$(mktemp)
meta=$(curl -s -L -o "$body" -w "%{http_code} %{url_effective}" --max-time 30 -A "Mozilla/5.0" "$URL" 2>/dev/null)
code=${meta%% *}
final=${meta#* }
if [ "$code" != "200" ] || [[ "$final" == *error.html* ]] || grep -q "시스템 작업" "$body"; then
  echo "[$(now)] 아직 점검 중 (HTTP ${code:-없음} · ${final##*/})"
  rm -f "$body"
  exit 0
fi
rm -f "$body"

echo "[$(now)] 점검 끝남 (HTTP $code · ${final##*/}) — 상세 수집기를 되살린다"
if [ -n "${DRY_RUN:-}" ]; then
  echo "  DRY_RUN — 실제로는 안 켠다"
  exit 0
fi

# 점검 중 타임아웃으로 실패 표시된 물건은 재시도 시각을 비워 바로 다시 받게 한다.
# 점검 직후 기일(추석 뒤 09-28~30 에 6,830건)의 서류를 기일 전에 받아야 해서다.
PYTHONPATH=src .venv/bin/python - <<'PY'
import sqlite3
db = sqlite3.connect("data/auction.sqlite3", timeout=60)
with db:
    n = db.execute(
        "UPDATE auction_items SET detail_next_retry_at = NULL "
        "WHERE detail_error LIKE '%sbx_auctnCsSrchCortOfc%' "
        "   OR detail_error LIKE '%Timeout 20000ms%'").rowcount
print(f"  점검 중 실패 표시 {n}건 — 재시도 시각 비움")
PY

if zsh scripts/resume_gentle.sh; then
  rm -f "$FLAG"
  echo "[$(now)] 재개 완료 — 점검 대기 표시를 지웠다"
else
  echo "[$(now)] !! 재개 실패 — 표시는 남겨 30분 뒤 다시 시도한다"
fi
