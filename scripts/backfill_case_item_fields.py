"""사건 화면 '물건내역'에서 뽑는 값(재매각·보증금·물건비고)을 이미 받아둔 상세에서 채운다.

상세는 여태 받아만 놓고 안 읽었다. 앞으로는 `save_item_detail`이 저장할 때 같이
뽑지만, 이미 들어와 있는 물건은 상세를 다시 받기 전까지 비어 있다. 법원 사이트를
다시 두드릴 필요 없이 손에 있는 JSON만 다시 읽으면 된다.

**updated_at을 건드리지 않는다.** 건드리면 3만 건이 전부 R2 재업로드 후보가 된다
(상세 763MB). 목록(스냅샷)은 매번 통째로 다시 만들므로 이 값들은 다음 푸시에
바로 실린다. 물건별 상세 파일은 그 물건이 갱신될 때 따라간다.

    python scripts/backfill_case_item_fields.py --db <경로> --dry-run
    python scripts/backfill_case_item_fields.py --db <경로>
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=ROOT / "data" / "auction.sqlite3")
    ap.add_argument("--limit", type=int, default=0, help="0이면 전부")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    # 워크트리엔 data/가 따라오지 않는다. 없는 DB를 열면 빈 DB가 조용히 생긴다.
    if not a.db.exists():
        print(f"DB가 없다: {a.db}\n원본 폴더의 DB 경로를 --db로 주어야 한다.")
        return 2

    from court_auction_crawler.enrichment import parse_case_item
    from court_auction_crawler.store import AuctionStore

    # 컬럼은 AuctionStore를 열 때 생긴다(ALTER TABLE). 생으로 붙으면 'no such column'이다.
    AuctionStore(a.db)

    read = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    read.row_factory = sqlite3.Row
    write = sqlite3.connect(a.db)
    write.execute("PRAGMA busy_timeout=30000")

    sql = (
        "SELECT item_key, item_no, detail_json, resale_reason, item_status_flow, "
        "deposit_amount, deposit_rate, item_note FROM auction_items "
        "WHERE detail_json IS NOT NULL AND detail_json != '' AND detail_json != '{}'"
    )
    if a.limit:
        sql += f" LIMIT {int(a.limit)}"

    seen = changed = resale = 0
    pending: list[tuple] = []
    for row in read.execute(sql):
        seen += 1
        try:
            detail = json.loads(row["detail_json"])
        except ValueError:
            continue
        info = parse_case_item(detail, row["item_no"])
        if info["resale_reason"]:
            resale += 1
        same = (
            (row["resale_reason"] or "") == info["resale_reason"]
            and (row["item_status_flow"] or "") == info["status_flow"]
            and row["deposit_amount"] == info["deposit_amount"]
            and row["deposit_rate"] == info["deposit_rate"]
            and (row["item_note"] or "") == info["note"]
        )
        if same:
            continue
        changed += 1
        pending.append(
            (
                info["resale_reason"], info["status_flow"], info["deposit_amount"],
                info["deposit_rate"], info["note"], row["item_key"],
            )
        )
        if len(pending) >= 500 and not a.dry_run:
            _flush(write, pending)
        if seen % 5000 == 0:
            print(f"  {seen}건 읽음 · 채울 것 {changed} · 재매각 {resale}", flush=True)
    if pending and not a.dry_run:
        _flush(write, pending)
    print(f"\n{'[예행] ' if a.dry_run else ''}상세 {seen}건 · 채운 것 {changed}건 · 재매각 {resale}건")
    return 0


def _flush(conn: sqlite3.Connection, pending: list[tuple]) -> None:
    with conn:
        conn.executemany(
            """
            UPDATE auction_items
               SET resale_reason = ?, item_status_flow = ?, deposit_amount = ?,
                   deposit_rate = ?, item_note = ?
             WHERE item_key = ?
            """,
            pending,
        )
    pending.clear()


if __name__ == "__main__":
    raise SystemExit(main())
