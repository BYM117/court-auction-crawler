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
import re
import subprocess
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 데몬이 실제로 읽는 소스는 **손으로 적지 않고 import 를 따라가 계산한다.** 손 목록은
# 두 번 구멍이 났다 — 2026-09-21 common.py, 09-29 rights.py·jev.py(관제실·JEV 세션이 발견).
# 세 데몬 모두 `python -m court_auction_crawler.cli …` 로 뜨니 cli.py 에서 닿는 전부가 대상이다.
# 감시는 '쉬지 않고 도는' 장수 데몬만 — baseline·logrotate·notices·maintenance-watch 는 매 실행
# 마다 새로 떠서 함정 ① 이 없다(넣으면 실행 사이에 '실행 중 아님' 으로 잘못 뜬다).
PKG = ROOT / "src" / "court_auction_crawler"
_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+(?:\.|court_auction_crawler\.?)(\w*)\s+import\s+([\w, ()]+)"
    r"|import\s+court_auction_crawler\.(\w+))", re.M)


def import_closure(entry: str = "cli.py") -> tuple[str, ...]:
    """entry 에서 패키지 안 모듈을 import 로 따라가 닿는 파일 전부(함수 안 지연 import 포함)."""
    seen: set[str] = set()
    todo = [entry]
    while todo:
        name = todo.pop()
        if name in seen or not (PKG / name).exists():
            continue
        seen.add(name)
        for mod, names, plain in _IMPORT_RE.findall((PKG / name).read_text(encoding="utf-8")):
            if plain:
                todo.append(f"{plain}.py")
            elif mod:
                todo.append(f"{mod}.py")
            else:  # from . import a, b
                # 'jev as jev_api' 처럼 별칭이 붙는다 — 앞 이름만(09-29 에 이걸 놓쳐 rights·jev 가 빠졌다)
                todo += [f"{n.split()[0]}.py" for n in names.replace("(", "").replace(")", "").split(",") if n.strip()]
    return tuple(f"src/court_auction_crawler/{n}" for n in sorted(seen))


_SOURCES = import_closure()
WATCHED = {label: _SOURCES for label in ("collect", "collect-details", "server")}
# 일부러 꺼 둔 데몬 — --fix 가 켜면 안 된다(2026-09-29: 09-30 명세서를 받으려고 collect 정지).
PAUSE_FLAGS = {"collect": ROOT / "data" / "collect_pause"}


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
        pause = PAUSE_FLAGS.get(label)
        if pause is not None and pause.exists():
            print(f"  {label:16} ⏸ 일부러 정지({pause.relative_to(ROOT)}) — 건드리지 않는다")
            continue
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
        # 시작 시각(ps)은 초까지라 수정 시각도 초로 자른다. 안 자르면 병합 직후 같은 초에 재시작한
        # 데몬이 '낡음' 으로 떴다(2026-09-30 하루 네 번 — 그중 한 번은 도는 사이클을 끊을 뻔했다).
        고친때 = datetime.fromtimestamp(int(최신[0]))
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
