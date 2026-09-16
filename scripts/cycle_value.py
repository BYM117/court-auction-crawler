"""수집 사이클을 줄이면 무엇을 잃는가 — 로그로 실측한다. (G17-3, 세션 C)

묶음 F는 "하루 8사이클 중 6개가 신규 0이니 낭비"라고 봤다. G17이 그 기준을
뒤집었다 — 낮 사이클의 가치는 신규를 찾는 게 아니라 **망가진 창을 메우는 것**이다.
메운 물건은 '동일'로 세어지므로 신규·변경 카운트에 안 잡힌다.

그래서 여기서 재는 것은 신규 건수가 아니라 **복구 효과**다.
사이클을 K회로 줄였을 때 "그날 그 법원을 통째로 못 보는" 일이 몇 번 생기는가.

읽기 전용. 로그만 본다.
"""
from __future__ import annotations

import re
import sys
from collections import defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

LOGS = Path(__file__).resolve().parent.parent / "logs"

RE_CYCLE = re.compile(r"자동 수집 시작 (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) mode=(\w+)")
RE_COURT = re.compile(r"^\[(\d+)/(\d+)\] (진행|예정) (\S+) ")
RE_COUNT = re.compile(r"^\s+-> (\d+)개 반영 \(신규 (\d+), 변경 (\d+), 동일 (\d+)\)")


def log_files() -> list[Path]:
    """날짜 순. 자정을 넘긴 사이클이 다음 파일로 이어지므로 순서가 중요하다."""
    return sorted((LOGS / "archive").glob("collect-all-*.log")) + [LOGS / "collect-all.log"]


def parse() -> dict[str, dict[str, dict[str, int | None]]]:
    """{사이클키: {'진행 법원': 건수}} — 건수 None은 '반영 줄이 없었다'(실패).

    건수는 '긁은 줄 수'가 아니라 **DB에 실제로 들어간 수**(신규+변경+동일)다.
    물건이 없는 화면이 안내 행 7~8줄을 돌려주는데, 긁은 줄 수로 세면 그게 '8건 수집'이
    되어 0건을 가린다. 커버리지 통계(cli.py `court_counts`)가 지금 그 함정에 빠져 있다.
    """
    cycles: dict[str, dict[str, int | None]] = {}
    meta: dict[str, str] = {}
    cur: dict[str, int | None] | None = None
    pending: str | None = None

    for path in log_files():
        if not path.exists():
            continue
        for line in path.read_text(errors="replace").splitlines():
            m = RE_CYCLE.search(line)
            if m:
                if pending and cur is not None:
                    cur.setdefault(pending, None)
                key, pending = m.group(1), None
                cur = cycles.setdefault(key, {})
                meta[key] = m.group(2)
                continue
            m = RE_COURT.match(line)
            if m:
                if pending and cur is not None:
                    cur.setdefault(pending, None)   # 앞 법원이 반영 줄 없이 끝났다
                pending = f"{m.group(3)} {m.group(4)}"
                continue
            m = RE_COUNT.match(line)
            if m and pending and cur is not None:
                cur[pending] = sum(int(g) for g in m.groups()[1:])
                pending = None
    if pending and cur is not None:
        cur.setdefault(pending, None)
    return {"cycles": cycles, "meta": meta}


def main() -> None:
    data = parse()
    cycles, meta = data["cycles"], data["meta"]

    by_day: dict[str, list[str]] = defaultdict(list)
    for key in sorted(cycles):
        by_day[key[:10]].append(key)

    print("=" * 72)
    print("① 실제 사이클 수 — 문서는 '하루 8회'라고 적어뒀다")
    print("=" * 72)
    print(f"{'날짜':<12}{'사이클':>6}{'평균간격':>10}  시작 시각")
    spans = []
    for day, keys in sorted(by_day.items()):
        ts = [datetime.fromisoformat(k) for k in keys]
        gap = ""
        if len(ts) > 1:
            mins = (ts[-1] - ts[0]).total_seconds() / 60 / (len(ts) - 1)
            gap, _ = f"{mins/60:.1f}h", spans.append(mins / 60)
        hhmm = " ".join(f"{t:%H:%M}" + ("*" if meta[k] == "full" else "")
                        for t, k in zip(ts, keys))
        print(f"{day:<12}{len(keys):>6}{gap:>10}  {hhmm}")
    full_days = [k for d, k in sorted(by_day.items()) if len(k) >= 3]
    print(f"\n* = full 모드. 하루 평균 {sum(len(k) for k in full_days)/len(full_days):.1f}회, "
          f"평균 간격 {sum(spans)/len(spans):.1f}시간")

    # ── 같은 날 안에서 0↔비0으로 흔들리는 (날짜×법원×구분) = 진짜 '창 망가짐'
    print()
    print("=" * 72)
    print("② 창이 망가진 흔적 — 같은 날 안에서 0과 비0이 섞인 경우")
    print("=" * 72)
    groups: dict[tuple[str, str], list[int | None]] = {}
    for day, keys in sorted(by_day.items()):
        if len(keys) < 2:
            continue
        for court in {c for k in keys for c in cycles[k]}:
            groups[(day, court)] = [cycles[k].get(court, "없음") for k in keys]

    steady = broken = 0
    broken_rows = []
    for (day, court), seq in sorted(groups.items()):
        seen = [v for v in seq if v != "없음"]
        zeros = [v for v in seen if v == 0 or v is None]
        if seen and zeros and len(zeros) < len(seen):
            broken += 1
            rescued = max(v for v in seen if isinstance(v, int))
            broken_rows.append((day, court, seq, rescued))
        else:
            steady += 1
    total = steady + broken
    print(f"일정(0이면 계속 0, 있으면 계속 있음) : {steady:>5} ({steady/total*100:.1f}%)")
    print(f"흔들림(같은 날 0↔비0)               : {broken:>5} ({broken/total*100:.1f}%)  ← 진짜 신호")
    print(f"\n※ 흔들린 {broken}건에서 다른 사이클이 되찾은 물건 "
          f"{sum(r[3] for r in broken_rows):,}건 (같은 날 최대치 기준)")
    print("\n최근 15건:")
    for day, court, seq, rescued in broken_rows[-15:]:
        shown = ["실패" if v is None else ("—" if v == "없음" else str(v)) for v in seq]
        print(f"  {day}  {court:<22} [{', '.join(shown)}]  → {rescued}건 복구")

    # ── 사이클을 줄이면? 가능한 모든 조합의 기댓값으로 본다
    print()
    print("=" * 72)
    print("③ 사이클을 K회로 줄이면 — 그날 그 법원을 '통째로 못 보는' 횟수")
    print("=" * 72)
    print("  가능한 모든 시간대 조합의 평균이다. 운이 좋은 조합도 나쁜 조합도 있다.")
    print()
    print(f"{'K회/일':<8}{'평균':>10}{'최선 조합':>12}{'최악 조합':>12}{'못 본 물건/일':>16}")
    for k in range(1, 7):
        lost_tot = items_tot = best_tot = 0.0
        worst = 0
        days = 0
        for day, keys in sorted(by_day.items()):
            if len(keys) < 3:
                continue          # 로그가 잘린 날은 제외
            days += 1
            combos = list(combinations(range(len(keys)), min(k, len(keys))))
            d_lost = d_items = 0.0
            combo_lost = []
            for combo in combos:
                lost = items = 0
                for court in {c for kk in keys for c in cycles[kk]}:
                    full = [cycles[keys[i]].get(court) for i in range(len(keys))]
                    best = max((v for v in full if isinstance(v, int)), default=0)
                    if best == 0:
                        continue   # 온종일 0 = 기일 소진. 정상이다
                    kept = [cycles[keys[i]].get(court) for i in combo]
                    if not any(isinstance(v, int) and v > 0 for v in kept):
                        lost += 1
                        items += best
                d_lost += lost / len(combos)
                d_items += items / len(combos)
                combo_lost.append(lost)
                worst = max(worst, lost)
            lost_tot += d_lost
            items_tot += d_items
            best_tot += min(combo_lost)   # 시간대를 가장 잘 고른 경우
        print(f"{k:<8}{lost_tot/days:>10.1f}{best_tot/days:>12.1f}{worst:>12}{items_tot/days:>16,.0f}")
    print(f"\n  ({days}일 평균. '완전손실' = 그날 그 법원×구분을 한 번도 비0으로 못 본 것)")


# ── 비용: 한 사이클이 무엇을 쓰는가 ──────────────────────────────────────────
RE_END = re.compile(r"자동 수집 종료 (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) exit")
RE_PUSH = re.compile(r"웹 푸시: 물건 ([\d,]+)건, 사진 ([\d,]+)건, ([\d.]+)MB, (\d+)초")


def cost() -> None:
    """사이클 하나의 소요 시간과 R2 푸시량. 줄여서 아끼는 쪽의 숫자다."""
    runs, pushes = [], []
    start = None
    for path in log_files():
        if not path.exists():
            continue
        for line in path.read_text(errors="replace").splitlines():
            m = RE_CYCLE.search(line)
            if m:
                start = datetime.fromisoformat(m.group(1))
                continue
            m = RE_END.search(line)
            if m and start:
                mins = (datetime.fromisoformat(m.group(1)) - start).total_seconds() / 60
                if 0 < mins < 24 * 60:
                    runs.append(mins)
                start = None
                continue
            m = RE_PUSH.search(line)
            if m:
                pushes.append((int(m.group(1).replace(",", "")),
                               float(m.group(3)), int(m.group(4))))

    print()
    print("=" * 72)
    print("④ 사이클 하나가 쓰는 것 — 줄이면 아끼는 쪽")
    print("=" * 72)
    runs.sort()
    print(f"완주한 사이클 {len(runs)}회 — 소요 시간 중앙값 {runs[len(runs)//2]/60:.1f}시간 "
          f"(최소 {runs[0]/60:.1f} / 최대 {runs[-1]/60:.1f})")
    print(f"launchd 대기 3시간 + 실행 {runs[len(runs)//2]/60:.1f}시간 → 실제 주기 "
          f"{3 + runs[len(runs)//2]/60:.1f}시간, 그래서 하루 8회가 아니라 5회다.")

    if pushes:
        items = sorted(p[0] for p in pushes)
        mbs = sorted(p[1] for p in pushes)
        secs = sorted(p[2] for p in pushes)
        n = len(pushes)
        print(f"\nR2 푸시 {n}회 — 사이클마다 물건 {items[n//2]:,}건 / {mbs[n//2]:,.0f}MB / {secs[n//2]}초 (중앙값)")
        print(f"  하루 5사이클 기준 {mbs[n//2]*5/1024:.1f}GB, {secs[n//2]*5/3600:.1f}시간")
        zero = sum(1 for i in items if i == 0)
        print(f"  물건 0건을 푸시한 사이클 {zero}회 ({zero/n*100:.0f}%) "
              f"— 나머지는 매번 수천 건을 다시 올린다")

def selfcheck() -> None:
    """파서가 무엇을 세는지 확인한다. `python3 scripts/cycle_value.py --check`

    두 가지를 틀리기 쉬워서 남긴다.
      ① 사이클이 자정을 넘기면 로그 파일이 갈린다 — 뒤 파일로 이어지는 줄까지
         앞 사이클에 붙어야 한다.
      ② 건수는 '긁은 줄 수'가 아니라 '들어간 수'다. 물건이 없는 화면이 안내 행
         7~8줄을 돌려주는데, 긁은 줄 수로 세면 0건이 8건으로 보인다.
    """
    import tempfile

    global LOGS
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "archive").mkdir()
        (root / "archive" / "collect-all-2026-01-01.log").write_text(
            "===== 자동 수집 시작 2026-01-01 23:00:00 mode=quick =====\n"
            "[1/120] 진행 가법원 2026-01-01~2026-01-14 수집 중\n"
            "  -> 100개 반영 (신규 1, 변경 2, 동일 97)\n"
            "[2/120] 진행 나법원 2026-01-01~2026-01-14 수집 중\n"
        )
        (root / "archive" / "collect-all-2026-01-02.log").write_text(
            "===== [로그 분리] =====\n"
            "  -> 8개 반영 (신규 0, 변경 0, 동일 0)\n"
            "[3/120] 진행 다법원 2026-01-01~2026-01-14 수집 중\n"
        )
        LOGS = root
        cycles = parse()["cycles"]
        got = cycles["2026-01-01 23:00:00"]

    assert got["진행 가법원"] == 100, got      # 1+2+97
    assert got["진행 나법원"] == 0, got        # 자정 넘어 이어진 줄, 안내 행은 0건
    assert got["진행 다법원"] is None, got     # 반영 줄이 없으면 '실패'
    print("자가 점검 통과: 자정 넘김 이어붙이기 · 안내 행 0건 · 실패 구분")


if __name__ == "__main__":
    if "--check" in sys.argv:
        selfcheck()
    else:
        main()
        cost()
