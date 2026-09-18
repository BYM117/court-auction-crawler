#!/usr/bin/env python3
"""실거래를 새 기준으로 다시 받도록 큐에 되돌린다 (G13).

두 가지가 바뀌어서 기존 데이터가 낡았다.

1. **조회 기간 6개월 → 24개월.** 그 건물이 6개월에 안 팔렸으면 못 맞췄다.
   실측(건물 45건): 6개월 22% · 12개월 28% · **24개월 33%** · 36개월 35%.
2. **단독·다가구와 상가·근린시설을 새로 묻는다.** 예전에는 통째로 '대상외' 였다.
   못 맞춘 게 아니라 묻지도 않았다(활성 1,615건).

`list_missing_enrichment` 는 성공한 것(`ok`)과 대상외(`skip`)를 다시 안 잡으므로,
상태를 비워 후보로 되돌린다. 일일 한도를 넘으면 `RateLimitError` 로 그날치가
멈췄다가 다음 실행에서 이어진다 — 한 번에 다 받으려 하지 않는다.

    python3 scripts/g13_requeue.py --dry-run
    python3 scripts/g13_requeue.py
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from court_auction_crawler.transactions import classify_transaction_kind  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(ROOT / "data" / "auction.sqlite3"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--include-inactive", action="store_true",
                    help="비활성까지. 기본은 활성만 — 한도를 진행 물건에 먼저 쓴다")
    a = ap.parse_args()

    conn = sqlite3.connect(a.db, timeout=60)
    conn.row_factory = sqlite3.Row
    where = "" if a.include_inactive else " WHERE is_active = 1"
    rows = conn.execute(
        f"SELECT item_key, category, address, transactions_status, transactions_detail "
        f"FROM auction_items{where}").fetchall()

    되돌릴것: list[str] = []
    이유 = Counter()
    for row in rows:
        status = row["transactions_status"] or ""
        kind = classify_transaction_kind(row["category"] or "", row["address"] or "")
        if status == "skip" and kind:
            되돌릴것.append(row["item_key"]); 이유[f"새로 대상이 됨({kind})"] += 1
        elif status == "ok" and "match_level" not in (row["transactions_detail"] or ""):
            되돌릴것.append(row["item_key"]); 이유["옛 모양(6개월·이름 대조)"] += 1

    print(f"다시 받을 물건 {len(되돌릴것):,}건")
    for 설명, n in 이유.most_common():
        print(f"   {설명:28} {n:7,}")
    if not 되돌릴것 or a.dry_run:
        print("(예행 — 아무것도 바꾸지 않았다)" if a.dry_run else "되돌릴 것 없음")
        return 0

    with conn:
        for i in range(0, len(되돌릴것), 500):
            묶음 = 되돌릴것[i:i + 500]
            conn.execute(
                f"UPDATE auction_items SET transactions_status = '', transactions_at = NULL "
                f"WHERE item_key IN ({','.join('?' * len(묶음))})", 묶음)
    print(f"{len(되돌릴것):,}건을 큐로 되돌렸다. 한도를 넘으면 다음 실행에서 이어진다")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
