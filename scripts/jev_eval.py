"""정답지(`jev_golden/`)로 규칙과 Jev 를 잰다 — 질문·규칙을 고칠 때의 관문.

    python3 scripts/jev_eval.py                 # 원본 폴더에서. 규칙 + Jev
    python3 scripts/jev_eval.py --no-jev        # 규칙만(키 없이, 돈 안 듦)
    python3 scripts/jev_eval.py --db /절대/경로/auction.sqlite3   # 워크트리에서

결과는 DB 옆 `jev_eval_history.jsonl` 에 쌓인다. **어느 묶음이든 지금까지 최고점보다
떨어지면 종료코드 1** — 질문 버전을 올리기 전에 이걸 통과해야 한다.

본문이 정답을 매길 때와 달라진 항목(지문 불일치)은 채점에서 빼고 따로 센다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from court_auction_crawler import jev as J  # noqa: E402
from court_auction_crawler import rights as R  # noqa: E402

fp = lambda t: hashlib.sha256(t.encode()).hexdigest()[:16]  # noqa: E731
name_hash = lambda n: hashlib.sha256(("jev-name:" + n).encode()).hexdigest()[:16]  # noqa: E731


def open_ro(path: str) -> sqlite3.Connection:
    if not Path(path).exists():   # 워크트리의 상대경로는 빈 DB 를 조용히 만든다 — 그 전에 멈춘다
        sys.exit(f"DB 가 없습니다: {path} — 원본 폴더에서 돌리거나 --db 에 절대경로를 주세요.")
    return sqlite3.connect(f"file:{path}?mode=rw", uri=True, timeout=30)


def load(name: str) -> dict:
    return json.loads((ROOT / "jev_golden" / f"{name}.json").read_text())


def note_of(db, key):
    """운영(`rights` 단계)과 같은 글 — 목록 비고 + 사건 비고. 예전 정답은 사건 비고만으로 지문을 찍었으니
    둘 다 후보로 돌려주고, 지문이 맞는 쪽으로 채점한다(대조 기준을 운영과 맞춘다, 함정 ⑤-1)."""
    row = db.execute("SELECT item_note, raw_json FROM auction_items WHERE item_key = ?", (key,)).fetchone()
    if not row:
        return None
    try:
        listed = str((json.loads(row[1] or "{}") or {}).get("비고") or "")
    except (TypeError, ValueError):
        listed = ""
    note = str(row[0] or "")
    return [" ".join(t for t in (listed, note) if t), note]


def doc_text(db, key=None, doc_id=None, kind="현황조사서"):
    row = (db.execute("SELECT metadata_json FROM auction_documents WHERE id = ?", (doc_id,)).fetchone() if doc_id
           else db.execute("SELECT metadata_json FROM auction_documents WHERE item_key = ? AND document_type = ? "
                           "AND status = 'collected'", (key, kind)).fetchone())
    return json.loads(row[0]).get("text", "") if row else None


def score(rows):
    """rows: [(정답, 규칙답, Jev답 또는 None)] → (규칙 맞음, Jev 맞음, Jev 채점 수)"""
    rule = sum(want == r for want, r, _ in rows)
    jev_rows = [(want, j) for want, _, j in rows if j is not None]
    return rule, sum(want == j for want, j in jev_rows), len(jev_rows)


def run(db, use_jev: bool) -> dict:
    results = {}

    def text_items(name, getter):
        data = load(name); out = []; drift = 0
        for it in data["items"]:
            got = getter(it)
            texts = got if isinstance(got, list) else [got]
            match = next((t for t in texts if t is not None and ("text_fp" not in it or
                          fp(t if name != "occupancy" else t[:200]) == it["text_fp"])), None)
            if match is None:
                drift += 1
                continue
            out.append((it, match))
        return out, drift

    items, drift = text_items("waiver", lambda it: note_of(db, it["key"]))
    rows = []
    for it, t in items:
        rule = "대항력" in t and (not R.WAIVER_RE.search(t) or R.waiver_leaves_other_tenant(t))
        j = None
        if use_jev:
            o = J.second_opinion(t, "waiver"); j = o["waiver"] < 0.5 or o["other_tenant"] >= 0.5
        rows.append((it["risk"], rule, j))
    results["waiver"] = (*score(rows), len(rows), drift)

    items, drift = text_items("lien", lambda it: note_of(db, it["key"]))
    rows = []
    for it, t in items:
        j = None
        if use_jev:
            o = J.second_opinion(t, "lien"); j = "해소" if o["lien_resolved"] >= 0.5 and o["lien_remaining"] < 0.5 else "남음"
        rows.append((it["status"], R.lien_status(t), j))
    results["lien"] = (*score(rows), len(rows), drift)

    items, drift = text_items("occupancy", lambda it: R.survey_memo(doc_text(db, key=it["key"]) or "") or None)
    rows = [(it["met"], R.occupancy_check(t)["confirmed"] is True, None) for it, t in items]
    results["occupancy"] = (*score(rows), len(rows), drift)

    rows = []; drift = 0
    for it in load("senior")["items"]:
        t = doc_text(db, doc_id=it["doc_id"])
        if t is None:
            drift += 1
            continue
        got = (R.senior_right(t) or {}).get("date")
        want = {R.parse_date(d).isoformat() for d in it["dates"]}
        j = None
        if use_jev:
            window, cands = R.senior_candidates(t)
            j = ((J.pick_senior(window, cands) or {}).get("date") in want) if cands else False
        rows.append((True, got in want, j))
    results["senior"] = (*score(rows), len(rows), drift)

    if use_jev:
        items, drift = text_items("names", lambda it: note_of(db, it["key"]))
        tp = total = fp_count = 0
        for it, t in items:
            found = {name_hash(n) for n in J.find_names(t, R.name_candidates(t))}
            want = set(it["name_hashes"]); tp += len(found & want); total += len(want); fp_count += len(found - want)
        results["names"] = (0, tp, total, total, drift, fp_count)
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="data/auction.sqlite3")
    ap.add_argument("--no-jev", action="store_true")
    args = ap.parse_args()
    use_jev = not args.no_jev and J.available()
    db = open_ro(args.db)
    results = run(db, use_jev)
    versions = {k: v["version"] for k, v in J.QUESTIONS.items()}
    print(f"정답지 채점 — 규칙 v{R.RIGHTS_VERSION} · Jev {'켜짐' if use_jev else '꺼짐'}")
    for name, r in results.items():
        if name == "names":
            print(f"  실명      Jev {r[1]}/{r[2]} 잡음 · 헛잡음 {r[5]} · 본문 바뀜 {r[4]}")
            continue
        rule_ok, jev_ok, jev_n, n, drift = r
        jev = f" · Jev {jev_ok}/{jev_n}" if jev_n else ""
        print(f"  {name:9} 규칙 {rule_ok}/{n}{jev} · 본문 바뀜 {drift}")
    hist = Path(args.db).resolve().parent / "jev_eval_history.jsonl"
    past = [json.loads(line) for line in hist.read_text().splitlines()] if hist.exists() else []
    # 정답지가 커지면(엇갈림 검토로 어려운 것이 들어온다) 규칙이 나아져도 비율은 떨어질 수 있다 —
    # 같은 정답지로 잰 기록끼리만 댄다. 정답지마다 지문을 남긴다.
    golden = {name: hashlib.sha256((ROOT / "jev_golden" / f"{name}.json").read_bytes()).hexdigest()[:12]
              for name in ("waiver", "lien", "occupancy", "senior", "names")}
    record = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "rules": R.RIGHTS_VERSION,
              "questions": versions, "jev": use_jev, "golden": golden,
              "scores": {k: {"rule": v[0] / v[3] if v[3] else None, "jev": (v[1] / v[2]) if v[2] else None}
                         for k, v in results.items()}}
    regress = []
    for name, cur in record["scores"].items():
        for kind in ("rule", "jev"):
            best = max((p["scores"].get(name, {}).get(kind) or 0 for p in past
                        if (p.get("golden") or {}).get(name) == golden.get(name)), default=0)
            if cur[kind] is not None and cur[kind] + 1e-9 < best:
                regress.append(f"{name}.{kind} {cur[kind]:.3f} < 최고 {best:.3f}")
    with hist.open("a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    if regress:
        print("!! 떨어졌다 — 반영하지 말 것:", "; ".join(regress))
        return 1
    print("통과 — 지금까지 최고점 이상")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
