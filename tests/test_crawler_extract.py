"""매각결과 표 추출 — 헤더의 빈 행이 rowspan 잔량을 먹어야 한다.

이 검사가 깨지면 페이지마다 첫 물건이 조용히 사라진다(2026-09-16 실측:
서울중앙 5페이지에서 5건). 예외도 로그도 안 남는 손실이라 검사로 묶어 둔다.
"""

import asyncio

import pytest

pytest.importorskip("playwright.async_api")

from playwright.async_api import async_playwright

from court_auction_crawler.crawler import RESULT_SEARCH, CourtAuctionCrawler, _prefer_local_browser_cache
from court_auction_crawler.models import SearchOptions


def _item_rows(case_no: str, item_no: str) -> str:
    return f"""
      <tr><td rowspan="2">{case_no}</td><td rowspan="2">{case_no}</td><td>{item_no}</td>
          <td rowspan="2">강원특별자치도 속초시 청학동 482</td><td rowspan="2">비고</td>
          <td>143,000,000</td><td>경매2계 2026.09.09</td></tr>
      <tr><td>근린시설</td><td>24,034,000</td><td>매각 25,100,000</td></tr>
    """


# 법원 사이트의 매각결과 표 구조. 헤더가 rowspan=3인데 셋째 줄은 셀이 하나도 없다.
FIXTURE = f"""<!doctype html><meta charset="utf-8"><body><table>
  <tr><th rowspan="3">전체</th><th rowspan="3">사건번호</th><th>물건번호</th>
      <th rowspan="3">소재지 및 내역</th><th rowspan="3">비고</th>
      <th>감정평가액</th><th>담당계 매각기일</th></tr>
  <tr><th rowspan="2">용도</th><th rowspan="2">최저매각가격</th><th rowspan="2">매각결과 매각대금</th></tr>
  <tr></tr>
  {_item_rows("속초지원 2025타경222", "1")}
  {_item_rows("속초지원 2025타경304", "2")}
</table></body>"""


async def _extract() -> list[str]:
    _prefer_local_browser_cache()
    crawler = CourtAuctionCrawler(SearchOptions(collection_mode="result"))
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(FIXTURE)
        items = await crawler._extract_court_items(page, RESULT_SEARCH)
        await browser.close()
    return [item.values["물건번호"] for item in items]


def test_빈_헤더_행이_있어도_첫_물건을_잃지_않는다():
    assert asyncio.run(_extract()) == ["1", "2"]
