"""auction_items.detail_json 의 상세 원문을 auction_item_details 로 옮긴다(2026-10-04).

데몬을 멈추지 않고 rowid 구간(기본 2,000줄)씩 짧게 옮긴다. **먼저 세 데몬(collect·collect-details·server)이
새 코드로 돌고 있어야 한다** — 옛 코드는 옛 칸에 다시 써서, 옮긴 뒤 새 코드가 읽는 표와 어긋난다.
몇 번을 다시 돌려도 결과가 같다(store.move_detail_json).

    .venv/bin/python scripts/migrate_detail_json.py
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from court_auction_crawler.store import AuctionStore, move_detail_json  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="data/auction.sqlite3")
    ap.add_argument("--batch", type=int, default=2000)
    ap.add_argument("--sleep", type=float, default=0.3, help="구간 사이 쉬는 초(다른 데몬 쓰기에 자리 내주기)")
    a = ap.parse_args()
    if not Path(a.db).exists():
        print(f"DB 가 없다: {a.db} (워크트리에서 돌리면 빈 DB 를 만든다 — 원본 폴더에서)")
        return 1
    AuctionStore(a.db)  # 표가 없으면 만든다
    conn = sqlite3.connect(a.db, timeout=60)
    conn.execute("PRAGMA busy_timeout=60000")
    lo, hi = conn.execute("SELECT MIN(rowid), MAX(rowid) FROM auction_items").fetchone()
    moved, started = 0, time.time()
    for n, start in enumerate(range(lo, hi + 1, a.batch), 1):
        with conn:
            moved += move_detail_json(conn, start, start + a.batch - 1)
        if n % 5 == 0:
            print(f"  rowid {start + a.batch - 1:,}/{hi:,} · 옮김 {moved:,} · {time.time() - started:.0f}초", flush=True)
        time.sleep(a.sleep)
    left = conn.execute("SELECT COUNT(*) FROM auction_items WHERE detail_json NOT IN ('', '{}')").fetchone()[0]
    print(f"끝: 옮김 {moved:,} · 옛 칸에 남은 것 {left:,} · {time.time() - started:.0f}초")
    return 0 if left == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
