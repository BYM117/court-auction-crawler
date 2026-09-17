"""G12: 엉뚱하게 찍힌 좌표가 지금 다시 돌리면 풀리는지 잰다. (세션 C 진단 도구)

`_extract_building_hint`가 건물명 대신 **시도 이름**을 돌려주면 장소검색 쿼리가
"전라남도 여수시 삼산면 전라남도"가 되고, 브이월드가 그 읍면의 아무 지점을 준다.
같은 읍면의 토지가 전부 한 점에 뭉친다. `_same_region` 가드는 지역이 맞으니 통과시킨다.

**다만 원인이 둘이다.** 하나는 위 버그, 다른 하나는 **그때 브이월드가 못 찾았을 뿐**인
경우다. 광주·전남 통합 대응이 2026-08-24(`ebbf640`)에 들어왔는데 망가진 좌표의 78%가
그 전(2026-07)에 찍혔다. 그런 것은 **코드를 안 고쳐도 다시 돌리면 풀린다.**

이 도구는 그 비율을 잰다. 읽기만 한다 — DB에 쓰지 않는다.

    .venv/bin/python scripts/geocode_recheck.py            # 표본 50건
    .venv/bin/python scripts/geocode_recheck.py --limit 20
    .venv/bin/python scripts/geocode_recheck.py --check    # 자가 점검(망 안 씀)
    .venv/bin/python scripts/geocode_recheck.py --db "$HOME/Documents/경매물건 크롤링/data/auction.sqlite3"
"""
from __future__ import annotations

import argparse
import math
import random
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "auction.sqlite3"
sys.path.insert(0, str(ROOT / "src"))

# 광역시·도 이름. 건물명 힌트가 이 중 하나면 그 좌표는 폴백이 만든 가짜다.
SIDO = (
    "서울특별시", "부산광역시", "대구광역시", "인천광역시", "광주광역시", "대전광역시",
    "울산광역시", "세종특별자치시", "경기도", "강원도", "강원특별자치도", "충청북도",
    "충청남도", "전라북도", "전북특별자치도", "전라남도", "경상북도", "경상남도",
    "제주도", "제주특별자치도", "전남광주통합특별시",
)


def broken_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """이미 박힌 가짜 좌표를 고른다.

    **지금 코드의 `_extract_building_hint`로 판정하면 안 된다.** 그 함수를 고치면
    (2026-09-17) 가짜 행이 0건으로 보여 "다 고쳐졌다"는 착각을 준다. 판단 근거는
    당시 실제로 던진 쿼리(`geocode_query`)다 — 시도명으로 끝나면 폴백이 지어낸 것이다.
    """
    rows = conn.execute(
        "SELECT item_key, address, geocode_query, lat, lng, geocoded_at FROM auction_items "
        "WHERE coordinate_source='building' AND is_active=1 AND lat IS NOT NULL"
    ).fetchall()
    return [r for r in rows if str(r["geocode_query"] or "").rstrip().endswith(SIDO)]


def km_between(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    dy = (lat2 - lat1) * 111
    dx = (lng2 - lng1) * 111 * math.cos(math.radians(lat2))
    return math.hypot(dx, dy)


def main(limit: int, seed: int, db: Path) -> None:
    from court_auction_crawler.geocoder import geocode_address

    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = broken_rows(conn)
    print(f"엉뚱하게 찍힌 활성 물건 {len(rows)}건")

    random.seed(seed)
    sample = random.sample(rows, min(limit, len(rows)))
    dists, still = [], []
    for i, r in enumerate(sample, 1):
        res = geocode_address(r["address"] or "")
        if res and res.quality == "verified":
            dists.append(km_between(r["lat"], r["lng"], res.lat, res.lng))
        else:
            still.append(r["address"])
        if i % 10 == 0:
            print(f"  {i}/{len(sample)} …", flush=True)

    n = len(sample)
    print(f"\n표본 {n}건")
    print(f"  다시 돌리면 정상(verified) 좌표를 얻음 : {len(dists)}건 ({len(dists)/n*100:.0f}%)")
    print(f"  여전히 실패                            : {len(still)}건")
    if dists:
        dists.sort()
        far = sum(1 for d in dists if d > 1)
        print(f"\n  고쳐진 핀이 옮겨간 거리 — 중앙값 {dists[len(dists)//2]:.2f}km, 최대 {dists[-1]:.2f}km")
        print(f"  1km 넘게 틀려 있던 것 {far}건 ({far/len(dists)*100:.0f}%)")
    for addr in still[:5]:
        print(f"    여전히 실패: {addr}")


def selfcheck() -> None:
    """'가짜 좌표'를 가려내는 조건이 맞는지 본다. 망은 쓰지 않는다.

    이 도구의 전제는 하나다 — 건물명 힌트가 **시도 이름**이면 그 좌표는 폴백이
    지어낸 것이다. 건물명이 진짜로 있으면 건드리면 안 된다.
    """
    from court_auction_crawler.geocoder import _extract_building_hint

    # 고친 뒤(2026-09-17): 순수 토지에서 시도명이 새어나가지 않는다. 힌트가 비면
    # 폴백 자체를 포기하므로 가짜 핀이 더는 생기지 않는다.
    land = "전라남도 여수시 삼산면 덕촌리 1069 [토지 전 3498㎡]"
    assert _extract_building_hint(land) == "", _extract_building_hint(land)

    named = "인천광역시 서구 가좌동 146-44 영동빌라 1동 2층 201호"
    assert _extract_building_hint(named) not in SIDO, _extract_building_hint(named)

    # 이미 박힌 가짜는 당시 쿼리로 가려낸다 — 코드를 고쳐도 판정이 흔들리지 않아야 한다.
    assert "전라남도 여수시 삼산면 전라남도".rstrip().endswith(SIDO)
    assert not "인천광역시 서구 가좌동 영동빌라".rstrip().endswith(SIDO)

    assert km_between(37.0, 127.0, 37.0, 127.0) == 0
    assert 1.0 < km_between(37.0, 127.0, 37.01, 127.0) < 1.2
    print("자가 점검 통과: 시도명으로 끝난 쿼리만 가짜로 잡고, 진짜 건물명은 안 건드린다")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--seed", type=int, default=12)
    ap.add_argument("--check", action="store_true")
    # 워크트리에는 data/가 따라오지 않는다. 원본 폴더의 DB를 가리켜 읽는다.
    ap.add_argument("--db", type=Path, default=DB_PATH)
    a = ap.parse_args()
    selfcheck() if a.check else main(a.limit, a.seed, a.db)
