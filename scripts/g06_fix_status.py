#!/usr/bin/env python3
"""'수집됨'이라 적혀 있지만 본문이 없는 문서를 `metadata_only`로 되돌린다 (G06).

판정은 수집기와 **같은 함수**(`detail_crawler.document_has_body`)를 쓴다. 두 곳에
두면 지표가 코드를 못 따라간다 — 오늘 여러 번 밟은 함정이다.

    python3 scripts/g06_fix_status.py --dry-run
    python3 scripts/g06_fix_status.py
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from court_auction_crawler.detail_crawler import document_has_body  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(ROOT / "data" / "auction.sqlite3"))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    conn = sqlite3.connect(a.db, timeout=60)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, document_type, metadata_json, file_path FROM auction_documents "
        "WHERE status = 'collected'").fetchall()
    print(f"'collected' 로 적힌 문서 {len(rows)}건을 다시 판정한다")

    거짓 = []
    종류별 = Counter()
    for row in rows:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except Exception:  # noqa: BLE001 - 깨진 JSON 은 내용 없음으로 본다
            metadata = {}
        download = {"file_path": row["file_path"]} if row["file_path"] else None
        if not document_has_body(metadata, download):
            거짓.append(row["id"])
            종류별[row["document_type"]] += 1

    print(f"실제로는 본문이 없는 것: {len(거짓)}건")
    for 종류, n in 종류별.most_common():
        print(f"   {종류:16} {n:7}건")
    if not 거짓 or a.dry_run:
        print("(예행 — 아무것도 바꾸지 않았다)" if a.dry_run else "고칠 것 없음")
        return 0

    with conn:
        for i in range(0, len(거짓), 500):
            묶음 = 거짓[i:i + 500]
            conn.execute(
                f"UPDATE auction_documents SET status='metadata_only' "
                f"WHERE id IN ({','.join('?' * len(묶음))})", 묶음)
    print(f"{len(거짓)}건을 'metadata_only' 로 되돌렸다")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
