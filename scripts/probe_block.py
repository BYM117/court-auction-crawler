"""차단 시험 탐침: 상세 수집기의 _collect_case 를 그대로 돌려(DB 는 안 쓴다) 법원 보안 차단이 몇 건·몇 분 만에 오는지 잰다.

막히면 그 자리에서 끝낸다 — 새 세션으로 다시 들어가지 않는다. 인근매각 검색 버튼을 켜고/끄고 비교하는 데 쓴다.

    .venv/bin/python scripts/probe_block.py --near on|off [--max 60] [--out logs/probe-tests]
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

from court_auction_crawler import detail_crawler
from court_auction_crawler.detail_crawler import CourtAuctionDetailCrawler, is_session_rejection, representative_case_no


class FakeStore:
    """수집 결과를 버린다. 문서는 '아직 없음' 으로 답해 수집기와 똑같이 문서까지 연다."""

    def document_statuses(self, key):
        return {}

    def backfill_sale_results(self, rows):
        return {"inserted": 0, "sold": 0}

    def save_asset(self, *args, **kwargs):
        return 0

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def pick_cases(db: str, limit: int) -> list[tuple[tuple[str, str], list[dict]]]:
    con = sqlite3.connect(db, timeout=60)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT item_key, court, case_no, item_no, sale_date, detail_collected_at FROM auction_items "
        "WHERE is_active=1 AND detail_collected_at IS NOT NULL ORDER BY random() LIMIT ?", (limit * 3,))]
    grouped: dict[tuple[str, str], list[dict]] = collections.defaultdict(list)
    for r in rows:
        grouped[(r["court"], representative_case_no(r["case_no"]))].append(r)
    return list(grouped.items())[:limit]


async def run(near: bool, limit: int, out: Path, db: str) -> dict:
    detail_crawler.NEAR_SALES_SEARCH = near
    crawler = CourtAuctionDetailCrawler(FakeStore(), asset_dir=out / "assets", delay=5)
    cases = pick_cases(db, limit)
    started = time.time()
    result = {"near": near, "start": datetime.now().isoformat(timespec="seconds"), "ok": 0, "items": 0, "blocked_at": None}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await (await browser.new_context(viewport={"width": 1440, "height": 1100})).new_page()
        for i, ((court, case_no), targets) in enumerate(cases, 1):
            try:
                r = await crawler._collect_case(page, court, case_no, targets)
                result["ok"] += 1
                result["items"] += r["collected"]
            except Exception as exc:  # noqa: BLE001
                if is_session_rejection(str(exc)):
                    result["blocked_at"] = i
                    await page.screenshot(path=str(out / f"blocked-{datetime.now():%Y%m%d-%H%M%S}.png"))
                    break
            await asyncio.sleep(5)
        await browser.close()
    result["minutes"] = round((time.time() - started) / 60, 1)
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--near", choices=["on", "off"], required=True)
    ap.add_argument("--max", type=int, default=60)
    ap.add_argument("--out", default="logs/probe-tests")
    ap.add_argument("--db", default="data/auction.sqlite3")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    result = asyncio.run(run(a.near == "on", a.max, out, a.db))
    line = json.dumps(result, ensure_ascii=False)
    print(line)
    with (out / "results.jsonl").open("a", encoding="utf-8") as f:
        f.write(line + "\n")


if __name__ == "__main__":
    main()
