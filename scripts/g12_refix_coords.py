"""G12: 폴백이 지어낸 가짜 좌표를 제자리에서 맞는 좌표로 갈아끼운다.

좌표를 **지웠다 다시 찍지 않는다.** 지우면 다음 사이클까지 지도에서 핀이 사라지고,
그 사이 옛 코드를 돌고 있는 데몬이 다시 가짜를 박을 수도 있다. 한 건씩 새로 물어보고
답을 받은 것만 바로 덮어쓴다.

가짜의 판정은 **그때 실제로 던진 쿼리**(`geocode_query`)가 시도명으로 끝나는지다.
지금 코드의 `_extract_building_hint`로 판정하면 안 된다 — 그 함수를 고쳤으므로
0건으로 보인다. `scripts/geocode_recheck.py`와 같은 기준이다.

    python scripts/g12_refix_coords.py --db <경로> --dry-run
    python scripts/g12_refix_coords.py --db <경로>
    python scripts/g12_refix_coords.py --db <경로> --include-inactive
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

SIDO = (
    "서울특별시", "부산광역시", "대구광역시", "인천광역시", "광주광역시", "대전광역시",
    "울산광역시", "세종특별자치시", "경기도", "강원도", "강원특별자치도", "충청북도",
    "충청남도", "전라북도", "전북특별자치도", "전라남도", "경상북도", "경상남도",
    "제주도", "제주특별자치도", "전남광주통합특별시",
)


def fake_rows(db: Path, include_inactive: bool) -> list[sqlite3.Row]:
    where = "coordinate_source='building' AND lat IS NOT NULL"
    if not include_inactive:
        where += " AND is_active=1"
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT item_key, address, lat, lng, geocode_query, coordinate_source, "
            f"coordinate_quality, geocoded_at, is_active FROM auction_items WHERE {where}"
        ).fetchall()
    return [r for r in rows if str(r["geocode_query"] or "").rstrip().endswith(SIDO)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=ROOT / "data" / "auction.sqlite3")
    ap.add_argument("--limit", type=int, default=0, help="0이면 전부")
    ap.add_argument("--include-inactive", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    # 워크트리엔 data/가 따라오지 않는다. 없는 DB를 열면 AuctionStore가 빈 DB를
    # 조용히 새로 만들어 "0건 처리"로 끝난다. 그 전에 멈춘다.
    if not a.db.exists():
        print(f"DB가 없다: {a.db}\n원본 폴더의 DB 경로를 --db로 주어야 한다.")
        return 2

    from court_auction_crawler.geocoder import geocode_address, normalize_auction_address
    from court_auction_crawler.store import AuctionStore

    rows = fake_rows(a.db, a.include_inactive)
    if a.limit:
        rows = rows[: a.limit]
    print(f"가짜 좌표 {len(rows)}건" + (" (비활성 포함)" if a.include_inactive else " (활성만)"))
    if not rows:
        return 0

    if not a.dry_run:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = a.db.parent / f"g12-fake-coords-{stamp}.json"
        backup.write_text(
            json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(f"원본 값 백업: {backup}")

    store = None if a.dry_run else AuctionStore(a.db)
    fixed = coarse = cleared = 0
    for i, r in enumerate(rows, 1):
        result = geocode_address(r["address"] or "")
        if result is None:
            cleared += 1
            if store:
                # 답이 없으면 가짜를 그대로 두지 않는다. **점까지 지운다** —
                # quality만 바꾸면 틀린 핀이 지도에 그대로 남는다.
                # 그때 던진 쿼리는 남긴다. 지우면 나중에 무엇이 가짜였는지 못 찾는다.
                store.mark_coordinate_missing(
                    r["item_key"],
                    normalized_address=normalize_auction_address(r["address"] or ""),
                    geocode_query=r["geocode_query"] or "",
                    quality="approximate",
                    clear_point=True,
                )
        else:
            if result.quality == "verified":
                fixed += 1
            else:
                coarse += 1
            if store:
                store.update_coordinates(
                    r["item_key"],
                    lat=result.lat,
                    lng=result.lng,
                    pnu=result.pnu,
                    coordinate_source=result.source,
                    coordinate_quality=result.quality,
                    normalized_address=result.normalized_address,
                    geocode_query=result.query,
                )
        if i % 50 == 0:
            print(f"  {i}/{len(rows)} … 정확 {fixed} · 근사 {coarse} · 핀없음 {cleared}", flush=True)

    print(f"\n{'[예행]' if a.dry_run else '완료'} 정확 {fixed} · 근사 {coarse} · 핀 없음 {cleared}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
