"""규칙과 Jev 가 엇갈린 물건 — 정답을 매겨 정답지(`jev_golden/`)를 키운다.

수집 사이클의 `권리 판정` 단계가 규칙이 '안전'(대항력 포기·유치권 해소)이라 했는데 Jev 가
'위험이 남았다' 고 본 것을 `rights_json.review` 에 모은다. 규칙이 모르는 새 유형은 여기서 나온다.

    python3 scripts/jev_review.py                          # 엇갈린 것 보기 (원본 폴더에서)
    python3 scripts/jev_review.py --add <item_key> waiver true    # 위험 남음이 정답
    python3 scripts/jev_review.py --add <item_key> lien 해소       # 해소가 정답

정답을 보탠 뒤 `scripts/jev_eval.py` 로 다시 잰다. 규칙이 틀린 거면 rights.py 를 고치고,
Jev 가 틀린 거면 jev.QUESTIONS 문구를 고쳐 버전을 올린다 — 어느 쪽이든 eval 을 통과해야 한다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="data/auction.sqlite3")
    ap.add_argument("--add", nargs=3, metavar=("ITEM_KEY", "SET", "LABEL"))
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args()
    if not Path(args.db).exists():
        sys.exit(f"DB 가 없습니다: {args.db} — 원본 폴더에서 돌리거나 절대경로를 주세요.")
    db = sqlite3.connect(f"file:{args.db}?mode=rw", uri=True, timeout=30)
    if args.add:
        key, name, label = args.add
        row = db.execute("SELECT item_note, raw_json FROM auction_items WHERE item_key = ?", (key,)).fetchone()
        if row is None:
            sys.exit("그 물건이 없습니다")
        listed = str((json.loads(row[1] or "{}") or {}).get("비고") or "")
        note = " ".join(t for t in (listed, str(row[0] or "")) if t)   # 운영과 같은 글(목록 비고 + 사건 비고)
        path = ROOT / "jev_golden" / f"{name}.json"
        data = json.loads(path.read_text())
        field = {"waiver": "risk", "lien": "status"}[name]
        value = label.lower() == "true" if name == "waiver" else label
        data["items"] = [i for i in data["items"] if i.get("key") != key]
        data["items"].append({"key": key, "text_fp": hashlib.sha256(note.encode()).hexdigest()[:16],
                              field: value, "tag": "엇갈림검토"})
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1))
        print(f"{name} 정답지에 보탰다 ({len(data['items'])}건). 이제 scripts/jev_eval.py 로 다시 잴 것.")
        return 0
    rows = db.execute(
        """SELECT item_key, item_note, rights_json FROM auction_items
            WHERE json_array_length(json_extract(NULLIF(rights_json, ''), '$.review')) > 0
            ORDER BY rights_at DESC LIMIT ?""", (args.limit,)).fetchall()
    if not rows:
        print("엇갈린 것 없음.")
    for key, note, rights in rows:
        review = json.loads(rights)["review"][-1]
        note = note or "(사건 비고 없음 — 목록 비고를 볼 것)"
        print(f"\n■ {key}  [{review['kind']}] 규칙={review['rule']}  Jev={review['jev']}\n  {note[:400]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
