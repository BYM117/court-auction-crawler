from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, timedelta
import os
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from playwright.async_api import Browser, Page, TimeoutError as PlaywrightTimeoutError, async_playwright

from .models import AuctionItem, CrawlPartition, SearchOptions
from .parser import rows_to_items

COURT_AUCTION_URL = "https://www.courtauction.go.kr/"
COURT_DETAIL_SEARCH_URL = "https://www.courtauction.go.kr/pgj/index.on?w2xPath=/pgj/ui/pgj100/PGJ151F00.xml"
COURT_SCHEDULED_SEARCH_URL = "https://www.courtauction.go.kr/pgj/index.on?w2xPath=/pgj/ui/pgj100/PGJ157M00.xml"


@dataclass(frozen=True, slots=True)
class SearchPageConfig:
    mode: str
    label: str
    source_label: str
    url: str
    court_selector: str
    start_date_selector: str
    end_date_selector: str
    search_button_selector: str


CURRENT_SEARCH = SearchPageConfig(
    mode="current",
    label="진행",
    source_label="진행",
    url=COURT_DETAIL_SEARCH_URL,
    court_selector="#mf_wfm_mainFrame_sbx_rletCortOfc",
    start_date_selector="#mf_wfm_mainFrame_cal_rletPerdStr_input",
    end_date_selector="#mf_wfm_mainFrame_cal_rletPerdEnd_input",
    search_button_selector="#mf_wfm_mainFrame_btn_gdsDtlSrch",
)

SCHEDULED_SEARCH = SearchPageConfig(
    mode="scheduled",
    label="예정",
    source_label="예정",
    url=COURT_SCHEDULED_SEARCH_URL,
    court_selector="#mf_wfm_mainFrame_sbx_dspslSchdGdsCortOfc",
    start_date_selector="#mf_wfm_mainFrame_cal_dspslSchdGdsPerdStr_input",
    end_date_selector="#mf_wfm_mainFrame_cal_dspslSchdGdsPerdEnd_input",
    search_button_selector="#mf_wfm_mainFrame_btn_dspslSchdGdsSrch",
)


class CourtAuctionCrawler:
    def __init__(self, options: SearchOptions) -> None:
        self.options = options

    async def collect(self) -> list[AuctionItem]:
        _prefer_local_browser_cache()
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=not self.options.headful)
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            await page.goto(COURT_AUCTION_URL, wait_until="domcontentloaded")

            if self.options.auto_search:
                await self._try_auto_search(page)
            elif self.options.headful:
                input("검색 조건을 설정하고 결과 목록 화면이 보이면 Enter를 누르세요...")

            items = await self._collect_result_pages(page)
            if self.options.collect_details:
                await self._collect_details(browser, items)
            await browser.close()
            return items

    async def collect_all(
        self,
        on_partition: Callable[[CrawlPartition, list[AuctionItem]], None] | None = None,
    ) -> list[AuctionItem]:
        _prefer_local_browser_cache()
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=not self.options.headful)
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            start = self.options.start_date or date.today()
            end = self.options.end_date or start + timedelta(days=30)
            collection_plan: list[tuple[SearchPageConfig, CrawlPartition]] = []
            for config in self._collection_configs():
                await self._open_search_page(page, config, force=True)
                courts = await self._get_court_options(page, config.court_selector)
                courts = self._filter_courts(courts)
                start, end = self._date_range_for_config(config)
                collection_plan.extend(
                    (config, partition)
                    for partition in build_partitions(courts, start, end, self.options.date_chunk_days, config.mode)
                )

            items: list[AuctionItem] = []
            seen: set[tuple[tuple[str, str], ...]] = set()
            for index, (config, partition) in enumerate(collection_plan, start=1):
                print(f"[{index}/{len(collection_plan)}] {config.label} {partition.label()} 수집 중")
                try:
                    partition_items = await self._collect_partition(page, partition, config)
                except Exception as exc:
                    print(f"  !! 구간 실패: {config.label} {partition.label()} - {exc}")
                    continue
                if on_partition:
                    on_partition(partition, partition_items)
                for item in partition_items:
                    key = tuple(sorted(item.normalized().items()))
                    if key and key not in seen:
                        seen.add(key)
                        items.append(item)
                        if self.options.max_items and len(items) >= self.options.max_items:
                            await browser.close()
                            return items

            if self.options.collect_details:
                await self._collect_details(browser, items)
            await browser.close()
            return items

    async def _try_auto_search(self, page: Page) -> None:
        await self._open_search_page(page, CURRENT_SEARCH)
        if self.options.court:
            await self._select_court(page, self.options.court, CURRENT_SEARCH.court_selector)
        await self._maybe_fill(page, ["법원", "담당법원"], self.options.court)
        await self._maybe_fill(page, ["검색어", "소재지", "물건명"], self.options.keyword)
        await self._fill_sale_dates(page, self.options.start_date, self.options.end_date, CURRENT_SEARCH)

        await self._click_search(page, CURRENT_SEARCH)

    async def _open_detail_search(self, page: Page, force: bool = False) -> None:
        await self._open_search_page(page, CURRENT_SEARCH, force)

    async def _open_search_page(self, page: Page, config: SearchPageConfig, force: bool = False) -> None:
        has_court_select = await page.locator(config.court_selector).count() if not force else 0
        if force:
            await page.goto(config.url, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_load_state("networkidle", timeout=30_000)
            await page.wait_for_timeout(1_000)
        elif config.url.split("w2xPath=", 1)[-1] not in page.url or not has_court_select:
            await page.goto(COURT_AUCTION_URL, wait_until="domcontentloaded", timeout=30_000)
            if config.mode == "current":
                await page.get_by_text("물건상세검색", exact=True).first.click()
                await page.wait_for_load_state("networkidle", timeout=30_000)
                await page.wait_for_timeout(1_000)
            else:
                await page.goto(config.url, wait_until="domcontentloaded", timeout=30_000)
                await page.wait_for_load_state("networkidle", timeout=30_000)
                await page.wait_for_timeout(1_000)

    async def _collect_partition(
        self,
        page: Page,
        partition: CrawlPartition,
        config: SearchPageConfig,
    ) -> list[AuctionItem]:
        for attempt in range(1, 4):
            await self._open_search_page(page, config, force=True)
            try:
                await self._select_court(page, partition.court, config.court_selector)
                break
            except ValueError:
                if attempt >= 3:
                    raise
                await page.wait_for_timeout(1_000)
        await self._fill_sale_dates(page, partition.start_date, partition.end_date, config)
        await self._select_largest_page_size(page)
        await self._click_search(page, config)
        return await self._collect_result_pages(page, config)

    async def _get_court_options(self, page: Page, selector: str) -> list[str]:
        return await page.evaluate(
            """
            (selector) => {
              const select = document.querySelector(selector);
              if (!select) return [];
              return [...select.options].map((option) => option.textContent.trim()).filter((text) => text && text !== '전체');
            }
            """,
            selector,
        )

    async def _select_court(self, page: Page, court: str, selector: str) -> None:
        selected = await page.evaluate(
            """
            ({ court, selector }) => {
              const select = document.querySelector(selector);
              if (!select) return false;
              const option = [...select.options].find((item) => item.textContent.trim() === court)
                || [...select.options].find((item) => item.textContent.trim().includes(court));
              if (!option) return false;
              select.value = option.value;
              select.dispatchEvent(new Event('change', { bubbles: true }));
              select.dispatchEvent(new Event('input', { bubbles: true }));
              return true;
            }
            """,
            {"court": court, "selector": selector},
        )
        if not selected:
            raise ValueError(f"법원 선택 실패: {court}")
        await page.wait_for_timeout(500)

    async def _fill_sale_dates(
        self,
        page: Page,
        start: date | None,
        end: date | None,
        config: SearchPageConfig,
    ) -> None:
        if start:
            await page.locator(config.start_date_selector).fill(start.strftime("%Y.%m.%d"))
        if end:
            await page.locator(config.end_date_selector).fill(end.strftime("%Y.%m.%d"))
        await page.wait_for_timeout(200)

    async def _click_search(self, page: Page, config: SearchPageConfig) -> None:
        await page.locator(config.search_button_selector).click()
        try:
            await page.wait_for_load_state("networkidle", timeout=20_000)
        except PlaywrightTimeoutError:
            pass
        await page.wait_for_timeout(int(self.options.delay * 1000))

    async def _select_largest_page_size(self, page: Page) -> None:
        await page.evaluate(
            """
            () => {
              const selects = [...document.querySelectorAll('select')];
              const pageSize = selects.find((select) => [...select.options].some((option) => option.textContent.trim() === '40'));
              if (!pageSize) return;
              const option = [...pageSize.options].find((item) => item.textContent.trim() === '40');
              if (!option) return;
              pageSize.value = option.value;
              pageSize.dispatchEvent(new Event('change', { bubbles: true }));
            }
            """
        )
        await page.wait_for_timeout(300)

    async def _collect_result_pages(self, page: Page, config: SearchPageConfig = CURRENT_SEARCH) -> list[AuctionItem]:
        seen: set[tuple[tuple[str, str], ...]] = set()
        items: list[AuctionItem] = []

        for page_number in range(1, self.options.max_pages + 1):
            await page.wait_for_timeout(int(self.options.delay * 1000))
            page_items = await self._extract_court_items(page, config)
            if not page_items and not await self._is_court_result_page(page):
                table_rows = await self._extract_best_table(page)
                page_items = rows_to_items(table_rows)

            for item in page_items:
                key = tuple(sorted(item.normalized().items()))
                if key and key not in seen:
                    seen.add(key)
                    items.append(item)
                    if self.options.max_items and len(items) >= self.options.max_items:
                        return items

            if not await self._go_next_page(page, page_number):
                break

        return items

    async def _extract_best_table(self, page: Page) -> list[list[str]]:
        return await page.evaluate(
            """
            () => {
              const visible = (el) => {
                const style = window.getComputedStyle(el);
                const box = el.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden' && box.width > 0 && box.height > 0;
              };

              const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
              const tables = [...document.querySelectorAll('table')].filter(visible);

              const candidates = tables.map((table) => {
                let rows = [...table.querySelectorAll('tr')]
                  .filter(visible)
                  .map((tr) => {
                    const cells = [...tr.querySelectorAll('th,td')];
                    const row = cells.map((cell) => clean(cell.innerText));
                    const link = tr.querySelector('a[href]');
                    const href = link ? new URL(link.getAttribute('href'), window.location.href).href : '';
                    return { row, href };
                  })
                  .filter((entry) => entry.row.some(Boolean));
                const hasLinks = rows.some((entry) => entry.href);
                if (hasLinks && rows.length) {
                  rows = rows.map((entry, index) => ({
                    row: [...entry.row, index === 0 ? '상세URL' : entry.href],
                    href: entry.href,
                  }));
                }
                rows = rows.map((entry) => entry.row);
                const text = rows.flat().join(' ');
                const keywords = ['사건', '물건', '소재', '매각', '감정', '최저'];
                const score = keywords.reduce((sum, word) => sum + (text.includes(word) ? 100 : 0), 0)
                  + rows.length * 10
                  + rows.reduce((sum, row) => sum + row.length, 0);
                return { rows, score };
              }).filter((candidate) => candidate.rows.length >= 2);

              candidates.sort((a, b) => b.score - a.score);
              return candidates[0]?.rows || [];
            }
            """
        )

    async def _extract_court_items(self, page: Page, config: SearchPageConfig) -> list[AuctionItem]:
        rows = await page.evaluate(
            """
            ({ courtSelector, sourceLabel }) => {
              const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
              const courtSelect = document.querySelector(courtSelector);
              const court = courtSelect ? courtSelect.options[courtSelect.selectedIndex]?.textContent.trim() : '';
              const table = [...document.querySelectorAll('table')]
                .find((candidate) => {
                  const text = clean(candidate.innerText);
                  return text.includes('사건번호') && text.includes('최저매각가격') && text.includes('진행상태');
                });
              if (!table) return [];
              const trs = [...table.querySelectorAll('tr')]
                .map((tr) => {
                  const cells = [...tr.querySelectorAll('th,td')].map((cell) => clean(cell.innerText));
                  const link = tr.querySelector('a[href]');
                  const href = link ? new URL(link.getAttribute('href'), window.location.href).href : '';
                  return { cells, href };
                })
                .filter((row) => row.cells.some(Boolean));
              const items = [];
              for (let index = 0; index < trs.length - 1; index += 1) {
                const first = trs[index];
                const second = trs[index + 1];
                if (first.cells.length >= 8 && second.cells.length >= 3 && /^\\d+$/.test(first.cells[2] || '')) {
                  const deptDate = first.cells[7] || '';
                  const saleDate = (deptDate.match(/\\d{4}\\.\\d{2}\\.\\d{2}/) || [''])[0];
                  const dept = deptDate.replace(saleDate, '').trim();
                  items.push({
                    '수집구분': sourceLabel,
                    '법원': court,
                    '사건번호': first.cells[1] || '',
                    '물건번호': first.cells[2] || '',
                    '소재지': first.cells[3] || '',
                    '비고': first.cells[5] || '',
                    '감정평가액': first.cells[6] || '',
                    '담당계': dept,
                    '매각기일': saleDate,
                    '용도': second.cells[0] || '',
                    '최저매각가격': second.cells[1] || '',
                    '진행상태': second.cells[2] || '',
                    '상세URL': first.href || second.href || '',
                  });
                  index += 1;
                }
              }
              return items;
            }
            """,
            {"courtSelector": config.court_selector, "sourceLabel": config.source_label},
        )
        return [AuctionItem(row) for row in rows]

    async def _is_court_result_page(self, page: Page) -> bool:
        return await page.evaluate(
            """
            () => {
              const text = (document.body.innerText || '').replace(/\\s+/g, ' ');
              return text.includes('사건번호') && text.includes('최저매각가격') && text.includes('진행상태');
            }
            """
        )

    def _collection_configs(self) -> list[SearchPageConfig]:
        mode = (self.options.collection_mode or "current").lower()
        if mode == "both":
            return [SCHEDULED_SEARCH, CURRENT_SEARCH]
        if mode in {"scheduled", "schedule", "예정"}:
            return [SCHEDULED_SEARCH]
        return [CURRENT_SEARCH]

    def _date_range_for_config(self, config: SearchPageConfig) -> tuple[date, date]:
        today = date.today()
        if config.mode == "scheduled":
            start = self.options.scheduled_start_date or self.options.start_date or today
            end = self.options.scheduled_end_date or self.options.end_date or start + timedelta(days=60)
            return start, end
        start = self.options.current_start_date or self.options.start_date or today - timedelta(days=365)
        end = self.options.current_end_date or self.options.end_date or today + timedelta(days=365)
        return start, end

    def _filter_courts(self, courts: list[str]) -> list[str]:
        if self.options.court:
            courts = [court for court in courts if self.options.court in court]
        if self.options.court_start:
            start_index = next(
                (index for index, court in enumerate(courts) if self.options.court_start in court),
                0,
            )
            courts = courts[start_index:]
        if self.options.court_limit:
            courts = courts[: self.options.court_limit]
        return courts

    async def _collect_details(self, browser, items: list[AuctionItem]) -> None:
        detail_page = await browser.new_page(viewport={"width": 1440, "height": 1000})
        try:
            for item in items:
                url = item.normalized().get("상세URL", "")
                if not is_safe_detail_url(url):
                    continue
                try:
                    await detail_page.goto(url, wait_until="domcontentloaded", timeout=20_000)
                    await detail_page.wait_for_timeout(int(self.options.delay * 1000))
                    details = await self._extract_detail_fields(detail_page)
                    for key, value in details.items():
                        item.values.setdefault(key, value)
                except Exception as exc:
                    item.values.setdefault("상세수집오류", str(exc)[:300])
        finally:
            await detail_page.close()

    async def _extract_detail_fields(self, page: Page) -> dict[str, str]:
        return await page.evaluate(
            """
            () => {
              const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
              const result = {};
              const tables = [...document.querySelectorAll('table')];
              for (const table of tables) {
                for (const tr of table.querySelectorAll('tr')) {
                  const cells = [...tr.querySelectorAll('th,td')].map((cell) => clean(cell.innerText)).filter(Boolean);
                  if (cells.length === 2) {
                    result[cells[0]] = cells[1];
                  } else if (cells.length > 2) {
                    for (let index = 0; index < cells.length - 1; index += 2) {
                      result[cells[index]] = cells[index + 1];
                    }
                  }
                }
              }
              return result;
            }
            """
        )

    async def _go_next_page(self, page: Page, current_page: int) -> bool:
        labels = [str(current_page + 1), "다음", ">", "Next"]
        for label in labels:
            locator = page.get_by_text(label, exact=True).last
            try:
                if await locator.count() and await locator.is_visible():
                    await locator.click()
                    await page.wait_for_load_state("networkidle", timeout=10_000)
                    return True
            except PlaywrightTimeoutError:
                return True
            except Exception:
                continue
        return False

    async def _maybe_click_text(self, page: Page, labels: list[str]) -> bool:
        for label in labels:
            locator = page.get_by_text(label, exact=True).first
            try:
                if await locator.count() and await locator.is_visible():
                    await locator.click()
                    await page.wait_for_timeout(500)
                    return True
            except Exception:
                continue
        return False

    async def _maybe_fill(self, page: Page, labels: list[str], value: str | None) -> bool:
        if not value:
            return False
        for label in labels:
            try:
                field = page.get_by_label(label).first
                if await field.count() and await field.is_visible():
                    await field.fill(value)
                    return True
            except Exception:
                pass

            try:
                field = page.get_by_placeholder(label).first
                if await field.count() and await field.is_visible():
                    await field.fill(value)
                    return True
            except Exception:
                pass
        return False

    async def _maybe_fill_date(self, page: Page, labels: list[str], start: date | None, end: date | None) -> None:
        values = [value.isoformat() for value in (start, end) if value]
        if not values:
            return

        date_inputs = page.locator("input[type='date'], input[placeholder*='YYYY'], input[placeholder*='yyyy'], input[title*='일']")
        count = await date_inputs.count()
        for index, value in enumerate(values[:count]):
            try:
                await date_inputs.nth(index).fill(value)
            except Exception:
                continue


def collect_sync(options: SearchOptions) -> list[AuctionItem]:
    return asyncio.run(CourtAuctionCrawler(options).collect())


def collect_all_sync(
    options: SearchOptions,
    on_partition: Callable[[CrawlPartition, list[AuctionItem]], None] | None = None,
) -> list[AuctionItem]:
    return asyncio.run(CourtAuctionCrawler(options).collect_all(on_partition))


def build_partitions(
    courts: list[str],
    start: date,
    end: date,
    chunk_days: int,
    source_mode: str = "",
) -> list[CrawlPartition]:
    chunk_days = max(chunk_days, 1)
    partitions: list[CrawlPartition] = []
    for court in courts:
        cursor = start
        while cursor <= end:
            chunk_end = min(cursor + timedelta(days=chunk_days - 1), end)
            partitions.append(CrawlPartition(court, cursor, chunk_end, source_mode=source_mode))
            cursor = chunk_end + timedelta(days=1)
    return partitions


def is_safe_detail_url(value: str) -> bool:
    parsed = urlparse(str(value or "").strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _prefer_local_browser_cache() -> None:
    local_cache = Path.cwd() / ".playwright-browsers"
    if local_cache.exists():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(local_cache))
