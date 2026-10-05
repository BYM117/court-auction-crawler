"""차단 시험 탐침: 상세 수집기의 _collect_case 를 그대로 돌려(DB 는 안 쓴다) 법원 보안 차단이 몇 건·몇 분 만에 오는지 잰다.

막히면 그 자리에서 끝낸다 — 새 세션으로 다시 들어가지 않는다. 인근매각 검색 버튼을 켜고/끄고 비교하는 데 쓴다.

    .venv/bin/python scripts/probe_block.py --near on|off [--stage tabs|item|full] [--max 60] [--out logs/probe-tests]

--stage 로 어디까지 할지 끊는다(무엇이 '비정상 접속' 판정을 부르는지 가리려고, 10-04~06, gaps/G20):
  tabs = 사건 검색 + 사건 화면 탭(기일내역·문건송달)까지. 물건 상세는 안 연다.
  item = + 물건 상세 화면. 문서(명세서·현황조사서 뷰어)는 안 연다.
  full = 수집기와 똑같이 전부(10-06 부터 수집기는 사건 화면 탭을 안 누른다).
  schedule / filing = 사건 화면에서 그 탭 하나만 누르고 돌아온다(물건 상세 안 엶).
  notabs = full 과 같되 사건 화면 탭을 확실히 안 누른다(10-06 새벽 40/40 무사).
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


async def run(near: bool, limit: int, out: Path, db: str, stage: str = "full") -> dict:
    detail_crawler.NEAR_SALES_SEARCH = "every" if near else "off"
    crawler = CourtAuctionDetailCrawler(FakeStore(), asset_dir=out / "assets", delay=5,
                                        collect_documents=stage == "full")
    cases = pick_cases(db, limit)
    detail_crawler.CASE_TABS = stage == "tabs"  # 'tabs' 단계만 옛 수집기처럼 두 탭을 누른다
    if stage == "notabs":  # 수집기와 똑같이 하되 사건 화면 탭은 안 누른다(첫 화면=사건내역 표만 읽음)
        async def no_tabs(page):
            return {"case_tables": await detail_crawler.extract_tables(page), "schedule_tables": [], "filing_and_service_tables": []}
        crawler._extract_case_shared = no_tabs
        crawler.collect_documents = True
    if stage in ("schedule", "filing"):  # 탭 하나만 누르고 사건내역 탭으로 돌아온다
        only = detail_crawler.SCHEDULE_TAB_SELECTOR if stage == "schedule" else detail_crawler.FILING_TAB_SELECTOR
        async def one_tab(page, _only=only):
            await crawler._click_if_present(page, _only)
            await crawler._click_if_present(page, detail_crawler.CASE_TAB_SELECTOR)
            return {"case_tables": [], "schedule_tables": [], "filing_and_service_tables": []}
        crawler._extract_case_shared = one_tab
    if stage in ("tabs", "schedule", "filing"):  # 받을 물건이 없으면 수집기는 물건 버튼을 누르지 않는다(사건 화면·탭까지만)
        cases = [(key, [{**t, "item_no": "__none__"} for t in targets]) for key, targets in cases]
    started = time.time()
    result = {"near": near, "stage": stage, "start": datetime.now().isoformat(timespec="seconds"),
              "ok": 0, "items": 0, "blocked_at": None, "requests": 0}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1440, "height": 1100})
        context.on("request", lambda r: result.__setitem__("requests", result["requests"] + 1)
                   if "courtauction.go.kr" in r.url else None)
        page = await context.new_page()
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
    ap.add_argument("--stage", choices=["tabs", "item", "full", "schedule", "filing", "notabs"], default="full")
    ap.add_argument("--max", type=int, default=60)
    ap.add_argument("--out", default="logs/probe-tests")
    ap.add_argument("--db", default="data/auction.sqlite3")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    result = asyncio.run(run(a.near == "on", a.max, out, a.db, a.stage))
    line = json.dumps(result, ensure_ascii=False)
    print(line)
    with (out / "results.jsonl").open("a", encoding="utf-8") as f:
        f.write(line + "\n")


if __name__ == "__main__":
    main()
