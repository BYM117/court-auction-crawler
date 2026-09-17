#!/usr/bin/env python3
"""입찰구분이 '전체'로 걸려 있는지, 그래서 잃는 물건이 없는지 법원 사이트에 직접 묻는다.

G09. 진행·예정 두 검색 화면의 입찰구분 **기본값이 `기일입찰`** 이다. 그냥 두면
기간입찰 물건이 에러도 경고도 없이 빠진다 — 행이 줄 뿐이라 `CLAUDE.md` 함정 ④의
연속 0건 감시에도 안 걸린다. 애초에 존재를 모르기 때문이다.

이 스크립트는 두 가지를 본다.

1. 수집기가 지나는 길(`_open_search_page`)로 화면을 열었을 때 '전체'가 눌려 있나
2. 같은 조건을 `기일입찰`과 `전체`로 각각 검색해 **건수가 같은가**

2번이 같으면 지금 기간입찰 물건이 정말 0이라는 뜻이고, 다르면 이미 잃고 있었다는
뜻이다. 제도가 재개되면 여기서 처음 갈라진다.

    PLAYWRIGHT_BROWSERS_PATH=.playwright-browsers .venv/bin/python scripts/g09_check_bid_type.py
"""
from __future__ import annotations

import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from playwright.async_api import async_playwright  # noqa: E402

from court_auction_crawler.crawler import (  # noqa: E402
    COURT_AUCTION_URL, CURRENT_SEARCH, SCHEDULED_SEARCH, CourtAuctionCrawler)
from court_auction_crawler.models import SearchOptions  # noqa: E402

법원들 = ("서울중앙지방법원", "수원지방법원", "부산지방법원")


async def 검색하고_센다(crawler, page, config, 기일입찰로, 법원, 시작, 끝) -> int | None:
    await crawler._open_search_page(page, config, force=True)
    await page.wait_for_timeout(1000)
    if 기일입찰로:
        # 기본값으로 되돌린다. 수집기가 '전체'를 누른 뒤이므로 명시적으로 눌러야 한다.
        기본값 = config.bid_type_all_input_id.replace("_input_2", "_input_0")
        await page.locator(f'label[for="{기본값}"]').click()
    await crawler._select_court(page, 법원, config.court_selector)
    await crawler._fill_sale_dates(page, 시작, 끝, config)
    await crawler._click_search(page, config)
    await page.wait_for_timeout(2500)
    총건수 = await crawler._read_total_count(page)
    if 총건수 is not None:
        return 총건수
    # 매각예정물건 화면에는 '총 N건' 표기가 없다(함정 ③). 첫 장 행수로 대신한다.
    return await page.evaluate(
        """() => { const n = [...document.querySelectorAll('table')]
                     .map(t => t.querySelectorAll('tbody tr').length);
                   return Math.max(0, ...n); }"""
    )


async def main() -> int:
    오늘 = date.today()
    crawler = CourtAuctionCrawler(SearchOptions())
    갈라진것: list[str] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 1000})
        await page.goto(COURT_AUCTION_URL, wait_until="domcontentloaded")

        for config in (CURRENT_SEARCH, SCHEDULED_SEARCH):
            await crawler._open_search_page(page, config, force=True)
            눌림 = await page.locator(f"#{config.bid_type_all_input_id}").is_checked()
            print(f"[{config.label}] 화면을 열면 '전체'가 눌려 있다: {눌림}")
            if not 눌림:
                갈라진것.append(f"{config.label} 화면에서 '전체'가 안 눌린다")

        for config, 끝 in ((CURRENT_SEARCH, 오늘 + timedelta(days=13)),
                           (SCHEDULED_SEARCH, 오늘 + timedelta(days=180))):
            for 법원 in 법원들:
                기일 = await 검색하고_센다(crawler, page, config, True, 법원, 오늘, 끝)
                전체 = await 검색하고_센다(crawler, page, config, False, 법원, 오늘, 끝)
                같나 = 기일 == 전체
                print(f"[{config.label}] {법원:10} 기일입찰 {기일} · 전체 {전체}"
                      + ("  → 같음" if 같나 else "  → ★다름★ 기간입찰 물건이 있다"))
                if not 같나:
                    갈라진것.append(f"{config.label}/{법원}: {기일} vs {전체}")
        await browser.close()

    if 갈라진것:
        print("\n확인 필요:")
        for 줄 in 갈라진것:
            print("  -", 줄)
        return 1
    print("\n두 화면 모두 '전체'로 검색하고 있고, 지금 기간입찰 물건은 없다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
