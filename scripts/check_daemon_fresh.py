#!/usr/bin/env python3
"""데몬이 지금 코드로 돌고 있나. **함정 ① 을 기억에 맡기지 않는다.**

`collect-loop` 은 한 프로세스가 안에서 사이클을 돈다. 파일을 고쳐도 재시작 전에는
옛 코드가 메모리에 남는다. 2026-09-18 과 09-20 에 이틀 연속으로 밟았고, 두 번째는
커밋 6분 전에 데몬이 시작해 훑기 우선순위 수정이 이틀 동안 안 먹었다.

    python3 scripts/check_daemon_fresh.py          # 확인만
    python3 scripts/check_daemon_fresh.py --fix    # 낡았으면 재시작까지
"""
from __future__ import annotations

import argparse
import os
import subprocess
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 데몬 이름 → 그 데몬이 실제로 읽는 소스. 여기 빠진 파일은 고쳐도 '최신' 으로 잡혀
# 거짓 음성이 된다(2026-09-21: common.py 가 빠져 self_restart 변경을 놓쳤다).
# **모든 데몬이 import 하는 common.py 를 반드시 포함한다.** 감시는 '쉬지 않고 도는'
# 장수 데몬만 대상이다 — baseline·logrotate·notices 는 매 실행마다 새로 떠서 함정 ①
# 이 없다(그걸 넣으면 실행 사이에 '실행 중 아님' 으로 잘못 뜬다).
_COMMON = "src/court_auction_crawler/common.py"
WATCHED = {
    "collect": ("src/court_auction_crawler/cli.py", "src/court_auction_crawler/crawler.py",
                "src/court_auction_crawler/store.py", "src/court_auction_crawler/enrichment.py",
                "src/court_auction_crawler/web_push.py", "src/court_auction_crawler/geocoder.py",
                "src/court_auction_crawler/transactions.py", _COMMON),
    "collect-details": ("src/court_auction_crawler/detail_crawler.py",
                        "src/court_auction_crawler/store.py",
                        "src/court_auction_crawler/cli.py", _COMMON),
    "server": ("src/court_auction_crawler/web.py", "src/court_auction_crawler/runners.py",
               "src/court_auction_crawler/store.py", _COMMON),
}


def _pid(label: str) -> int | None:
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True,
                             timeout=10).stdout
    except Exception:  # noqa: BLE001
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] == f"com.court-auction.{label}":
            return int(parts[0]) if parts[0].isdigit() else None
    return None


def _started_at(pid: int) -> datetime | None:
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        return datetime.strptime(out, "%a %b %d %H:%M:%S %Y") if out else None
    except Exception:  # noqa: BLE001
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fix", action="store_true", help="낡은 데몬을 재시작한다")
    a = ap.parse_args()

    낡음: list[str] = []
    for label, sources in WATCHED.items():
        pid = _pid(label)
        if not pid:
            print(f"  {label:16} 실행 중 아님")
            continue
        시작 = _started_at(pid)
        if not 시작:
            print(f"  {label:16} 시작 시각을 못 읽음")
            continue
        최신 = max(((ROOT / s).stat().st_mtime, s) for s in sources
                   if (ROOT / s).exists())
        고친때 = datetime.fromtimestamp(최신[0])
        if 고친때 > 시작:
            낡음.append(label)
            print(f"  {label:16} ★낡음★ 시작 {시작:%m-%d %H:%M} < {최신[1].split('/')[-1]} "
                  f"{고친때:%m-%d %H:%M}")
        else:
            print(f"  {label:16} 최신 (시작 {시작:%m-%d %H:%M})")

    if not 낡음:
        print("\n모든 데몬이 지금 코드로 돌고 있다.")
        return 0
    if not a.fix:
        print(f"\n낡은 데몬 {len(낡음)}개. `--fix` 를 붙이면 재시작한다.")
        return 1
    uid = os.getuid()
    for label in 낡음:
        subprocess.run(["launchctl", "kickstart", "-k",
                        f"gui/{uid}/com.court-auction.{label}"], timeout=30)
        print(f"  {label} 재시작함")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
