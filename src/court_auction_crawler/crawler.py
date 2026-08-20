from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, timedelta
import os
from pathlib import Path
import re
from typing import Callable
from urllib.parse import urlparse

from playwright.async_api import Browser, Page, TimeoutError as PlaywrightTimeoutError, async_playwright

from .models import AuctionItem, CrawlPartition, SearchOptions
from .parser import rows_to_items

COURT_AUCTION_URL = "https://www.courtauction.go.kr/"
COURT_DETAIL_SEARCH_URL = "https://www.courtauction.go.kr/pgj/index.on?w2xPath=/pgj/ui/pgj100/PGJ151F00.xml"
COURT_SCHEDULED_SEARCH_URL = "https://www.courtauction.go.kr/pgj/index.on?w2xPath=/pgj/ui/pgj100/PGJ157M00.xml"
COURT_RESULT_SEARCH_URL = "https://www.courtauction.go.kr/pgj/index.on?w2xPath=/pgj/ui/pgj100/PGJ158M00.xml"

# 법원경매 사이트는 WebSquare SPA라 백그라운드 요청이 끊이지 않아 networkidle이
# 사실상 오지 않는다. 대신 결과 영역의 내용 토큰이 바뀌는 것을 직접 기다린다.
RESULTS_TOKEN_JS = """
() => {
  const table = [...document.querySelectorAll('table')]
    .find((candidate) => (candidate.innerText || '').includes('사건번호'));
  const text = table ? table.innerText : (document.body ? document.body.innerText : '');
  return text.length + ':' + text.slice(0, 400);
}
"""


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
    # 결과표를 찾는 표식과 마지막 상세 칸의 이름. 매각결과 화면만 '진행상태' 대신
    # '매각결과'를 쓰고 값에 낙찰가가 함께 들어온다(예: "매각 151,436,000").
    table_marker: str = "진행상태"
    status_field: str = "진행상태"
    # 매각결과 화면은 소재지와 내역이 한 칸으로 합쳐져 뒤쪽 열이 하나씩 앞당겨진다.
    column_offset: int = 0


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

# 매각결과검색. 낙찰 여부와 낙찰가(매각대금)를 주는 유일한 화면이다.
# 기간 조건이 없고 법원만 고르면 되며, 사이트가 '매각기일 다음날부터 7일간'만
# 보여주므로 놓치면 그 기일의 낙찰가는 영영 받을 수 없다.
RESULT_SEARCH = SearchPageConfig(
    mode="result",
    label="결과",
    source_label="결과",
    url=COURT_RESULT_SEARCH_URL,
    court_selector="#mf_wfm_mainFrame_sbx_dspslRsltSrchCortOfc",
    start_date_selector="",
    end_date_selector="",
    search_button_selector="#mf_wfm_mainFrame_btn_dspslRsltSrch",
    table_marker="매각결과",
    status_field="매각결과",
    column_offset=-1,
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
                # 매각예정물건 화면은 기간 조건이 없어 어떤 날짜를 넣어도 법원 전체가
                # 나온다. 날짜로 쪼개봐야 같은 목록을 반복 수집하므로 법원당 1회면 된다.
                chunk_days = (
                    self.options.date_chunk_days
                    if config.mode == "current"
                    else max((end - start).days + 1, 1)
                )
                collection_plan.extend(
                    (config, partition)
                    for partition in build_partitions(courts, start, end, chunk_days, config.mode)
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
            await self._wait_for_search_form(page, config)
        elif config.url.split("w2xPath=", 1)[-1] not in page.url or not has_court_select:
            await page.goto(COURT_AUCTION_URL, wait_until="domcontentloaded", timeout=30_000)
            if config.mode == "current":
                await page.get_by_text("물건상세검색", exact=True).first.click()
            else:
                await page.goto(config.url, wait_until="domcontentloaded", timeout=30_000)
            await self._wait_for_search_form(page, config)

    async def _wait_for_search_form(self, page: Page, config: SearchPageConfig) -> None:
        try:
            await page.wait_for_selector(config.court_selector, timeout=20_000)
            await page.wait_for_function(
                """(selector) => {
                  const select = document.querySelector(selector);
                  return Boolean(select && select.options && select.options.length > 1);
                }""",
                arg=config.court_selector,
                timeout=10_000,
            )
        except PlaywrightTimeoutError:
            # 폼이 늦으면 이후 법원 선택 재시도 루프가 실패를 처리한다.
            pass
        await page.wait_for_timeout(300)

    async def _results_token(self, page: Page) -> str:
        try:
            return str(await page.evaluate(RESULTS_TOKEN_JS))
        except Exception:
            return ""

    async def _wait_for_results_change(self, page: Page, previous_token: str, timeout_ms: int = 15_000) -> None:
        try:
            await page.wait_for_function(
                f"(prev) => {{ const compute = {RESULTS_TOKEN_JS}; return compute() !== prev; }}",
                arg=previous_token,
                timeout=timeout_ms,
            )
        except PlaywrightTimeoutError:
            pass

    async def collect_results(
        self,
        on_court: Callable[[str, list[AuctionItem]], None] | None = None,
    ) -> list[AuctionItem]:
        """법원별 매각결과(낙찰 여부·낙찰가)를 훑는다.

        기간 조건이 없는 화면이라 법원만 바꿔가며 조회하면 된다. 사이트가 직전
        기일들의 결과만 짧게 보여주므로 자주 돌수록 놓치는 기일이 줄어든다."""
        _prefer_local_browser_cache()
        collected: list[AuctionItem] = []
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=not self.options.headful)
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            await page.goto(COURT_AUCTION_URL, wait_until="domcontentloaded")
            await self._open_search_page(page, RESULT_SEARCH, force=True)
            courts = self._filter_courts(await self._get_court_options(page, RESULT_SEARCH.court_selector))
            for index, court in enumerate(courts, start=1):
                print(f"[{index}/{len(courts)}] 결과 {court} 수집 중")
                try:
                    await self._open_search_page(page, RESULT_SEARCH, force=True)
                    await self._select_court(page, court, RESULT_SEARCH.court_selector)
                    await self._click_search(page, RESULT_SEARCH)
                    items = await self._collect_result_pages(page, RESULT_SEARCH)
                except Exception as exc:
                    print(f"  !! 결과 수집 실패: {court} - {exc}")
                    continue
                if on_court:
                    on_court(court, items)
                collected.extend(items)
            await browser.close()
        return collected

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
        fill_start = partition.start_date
        fill_end = partition.end_date
        if config.mode == "scheduled":
            # 매각예정물건 화면은 '2주 후 이후' 기일을 담당한다(그 전은 물건상세검색 몫).
            # 시작일이 그보다 이르거나 기간이 길면 검색이 조용히 실행되지 않는다.
            # 어차피 날짜는 결과에 반영되지 않으므로(항상 법원 전체 반환) 유효한
            # 짧은 구간만 채워 검색을 실행시킨다.
            fill_start = max(fill_start, date.today() + timedelta(days=15))
            fill_end = fill_start + timedelta(days=30)
        await self._fill_sale_dates(page, fill_start, fill_end, config)
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
        # 화면에 따라 기간 입력이 없을 수 있다(매각예정물건). 있을 때만 채운다.
        for selector, value in (
            (config.start_date_selector, start),
            (config.end_date_selector, end),
        ):
            if value is None:
                continue
            locator = page.locator(selector)
            if await locator.count():
                await locator.fill(value.strftime("%Y.%m.%d"))
        await page.wait_for_timeout(200)

    async def _click_search(self, page: Page, config: SearchPageConfig) -> None:
        token = await self._results_token(page)
        await page.locator(config.search_button_selector).click(timeout=10_000)
        await self._wait_for_results_change(page, token, timeout_ms=20_000)
        await page.wait_for_timeout(int(self.options.delay * 1000))

    async def _select_largest_page_size(self, page: Page) -> None:
        # 페이지 크기 셀렉트(10/20/30/40)는 검색 결과가 나온 뒤에야 DOM에 생기므로
        # 반드시 검색 후에 호출해야 한다. 기본 10건이면 페이지 수가 4배로 늘어난다.
        token = await self._results_token(page)
        changed = await page.evaluate(
            """
            () => {
              const selects = [...document.querySelectorAll('select')];
              const pageSize = selects.find((select) => {
                const texts = [...select.options].map((option) => option.textContent.trim());
                return texts.includes('40') && texts.includes('10');
              });
              if (!pageSize) return false;
              const option = [...pageSize.options].find((item) => item.textContent.trim() === '40');
              if (!option || pageSize.value === option.value) return false;
              pageSize.value = option.value;
              pageSize.dispatchEvent(new Event('change', { bubbles: true }));
              pageSize.dispatchEvent(new Event('input', { bubbles: true }));
              return true;
            }
            """
        )
        if changed:
            await self._wait_for_results_change(page, token, timeout_ms=10_000)
            await page.wait_for_timeout(int(self.options.delay * 500))

    async def _read_total_count(self, page: Page) -> int | None:
        try:
            value = await page.evaluate(
                r"""
                () => {
                  const match = (document.body.innerText || '').match(/총?\s*([\d,]+)\s*건/);
                  return match ? match[1] : null;
                }
                """
            )
        except Exception:
            return None
        if not value:
            return None
        try:
            return int(str(value).replace(",", ""))
        except ValueError:
            return None

    async def _collect_result_pages(self, page: Page, config: SearchPageConfig = CURRENT_SEARCH) -> list[AuctionItem]:
        seen: set[tuple[tuple[str, str], ...]] = set()
        items: list[AuctionItem] = []
        await self._select_largest_page_size(page)
        total_count = await self._read_total_count(page)
        # 같은 '총 N건'인데 화면마다 세는 단위가 다르다. 물건상세검색(진행)은 물건
        # 수라서 종료·경고 기준으로 쓸 수 있지만(옥션원 목록과 오차 0으로 확인),
        # 매각예정물건(예정)은 일괄매각 필지가 따로 잡힌 목록행 수라 크게 부풀려진다
        # (창원 09.29~30: 표시 157, 실제 80여 건). 예정 화면에서는 기준으로 쓰지 않고
        # 페이저가 끝날 때까지 훑는다.
        counts_items = config.mode == "current"
        pages_walked = 0

        for page_number in range(1, self.options.max_pages + 1):
            pages_walked = page_number
            page_items = await self._extract_court_items(page, config)
            if not page_items and not await self._is_court_result_page(page, config.table_marker):
                table_rows = await self._extract_best_table(page)
                page_items = rows_to_items(table_rows)

            for item in page_items:
                key = tuple(sorted(item.normalized().items()))
                if key and key not in seen:
                    seen.add(key)
                    items.append(item)
                    if self.options.max_items and len(items) >= self.options.max_items:
                        return items

            if counts_items and total_count is not None and len(items) >= total_count:
                break
            if not await self._go_next_page(page, page_number):
                break

        if pages_walked >= self.options.max_pages:
            print(f"  !! 페이지 상한({self.options.max_pages})에 걸려 뒷부분을 못 봤을 수 있음 (물건 {len(items)}건)")
        elif counts_items and total_count is not None and items:
            shortfall = total_count - len(items)
            if shortfall > max(3, int(total_count * 0.05)):
                print(
                    f"  !! 수집 부족 의심: 사이트 표시 {total_count}건 중 "
                    f"{len(items)}건 수집 ({pages_walked}페이지 순회)"
                )
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
        # 한 사건에 물건이 여러 개면 사건번호 셀이 rowspan으로 합쳐져 뒤 물건 행의
        # 셀 수가 모자란다. rowspan을 펼쳐 모든 행을 같은 열 구조로 정규화하고,
        # 이 행에서 새로 시작한 셀(fresh)인지 이월된 셀인지 구분해 물건 행을 찾는다.
        rows = await page.evaluate(
            """
            ({ courtSelector, sourceLabel, tableMarker, statusField, columnOffset }) => {
              const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
              const courtSelect = document.querySelector(courtSelector);
              const court = courtSelect ? courtSelect.options[courtSelect.selectedIndex]?.textContent.trim() : '';
              const table = [...document.querySelectorAll('table')]
                .find((candidate) => {
                  const text = clean(candidate.innerText);
                  return text.includes('사건번호') && text.includes('최저매각가격') && text.includes(tableMarker);
                });
              if (!table) return [];

              const pending = [];
              const grid = [];
              for (const tr of table.querySelectorAll('tr')) {
                const cells = [...tr.querySelectorAll('th,td')];
                if (!cells.length) continue;
                const texts = [];
                const fresh = [];
                let href = '';
                let col = 0;
                const absorbPending = () => {
                  while (pending[col] && pending[col].remaining > 0) {
                    texts[col] = pending[col].text;
                    fresh[col] = false;
                    pending[col].remaining -= 1;
                    col += 1;
                  }
                };
                for (const cell of cells) {
                  absorbPending();
                  const rowspan = parseInt(cell.getAttribute('rowspan') || '1', 10) || 1;
                  const colspan = parseInt(cell.getAttribute('colspan') || '1', 10) || 1;
                  const text = clean(cell.innerText);
                  const link = cell.querySelector('a[href]');
                  if (link && !href) href = new URL(link.getAttribute('href'), window.location.href).href;
                  for (let c = 0; c < colspan; c += 1) {
                    texts[col] = text;
                    fresh[col] = true;
                    if (rowspan > 1) pending[col] = { text, remaining: rowspan - 1 };
                    col += 1;
                  }
                }
                absorbPending();
                if (texts.some(Boolean)) grid.push({ texts, fresh, href });
              }

              const freshTexts = (row) => row.texts.filter((text, index) => row.fresh[index] && text);
              const items = [];
              for (let index = 0; index < grid.length - 1; index += 1) {
                const first = grid[index];
                const second = grid[index + 1];
                const itemNo = first.texts[2] || '';
                // 물건 행의 물건번호는 반드시 이 행에서 시작한 셀이어야 한다.
                // (일괄매각의 추가 필지 행은 물건번호가 이월값이라 여기서 걸러진다)
                if (
                  first.texts.length >= 8 + columnOffset &&
                  first.fresh[2] &&
                  /^\\d+$/.test(itemNo) &&
                  freshTexts(second).length >= 3
                ) {
                  const deptDate = first.texts[7 + columnOffset] || '';
                  const saleDate = (deptDate.match(/\\d{4}\\.\\d{2}\\.\\d{2}/) || [''])[0];
                  const dept = deptDate.replace(saleDate, '').trim();
                  const detail = freshTexts(second);
                  items.push({
                    '수집구분': sourceLabel,
                    '법원': court,
                    '사건번호': first.texts[1] || '',
                    '물건번호': itemNo,
                    '소재지': first.texts[3] || '',
                    '비고': first.texts[5 + columnOffset] || '',
                    '감정평가액': first.texts[6 + columnOffset] || '',
                    '담당계': dept,
                    '매각기일': saleDate,
                    '용도': detail[0] || '',
                    '최저매각가격': detail[1] || '',
                    [statusField]: detail[2] || '',
                    '상세URL': first.href || second.href || '',
                  });
                  index += 1;
                }
              }
              return items;
            }
            """,
            {
                "courtSelector": config.court_selector,
                "sourceLabel": config.source_label,
                "tableMarker": config.table_marker,
                "statusField": config.status_field,
                "columnOffset": config.column_offset,
            },
        )
        return [AuctionItem(row) for row in rows]

    async def _is_court_result_page(self, page: Page, marker: str = "진행상태") -> bool:
        return await page.evaluate(
            """
            (marker) => {
              const text = (document.body.innerText || '').replace(/\\s+/g, ' ');
              return text.includes('사건번호') && text.includes('최저매각가격') && text.includes(marker);
            }
            """,
            marker,
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
        # WebSquare 페이저(w2pageList)는 신뢰된 클릭(실제 마우스 이벤트)만 받는다.
        # JS el.click()이나 문서 전체 텍스트 매칭은 페이지크기 셀렉트의 '10' 같은
        # 엉뚱한 요소를 집어 수집이 90건(10건×9페이지)에서 잘리는 원인이었다.
        token = await self._results_token(page)
        clicked = False
        try:
            # 페이지 링크는 진행/예정 화면 공통으로 ..._page_N 아이디를 쓴다.
            link = page.locator(f'.w2pageList a[id$="_page_{current_page + 1}"]:visible').first
            if not await link.count():
                link = page.locator(
                    f'.w2pageList a.w2pageList_control_label:text-is("{current_page + 1}")'
                ).first
            if await link.count():
                await link.scroll_into_view_if_needed(timeout=3_000)
                await link.click(timeout=5_000)
                clicked = True
            else:
                # 현재 블록(1~10)에 다음 번호가 없으면 다음 블록 화살표로 넘어간다.
                next_block = page.locator(
                    '.w2pageList button[id$="_next_btn"]:visible, .w2pageList button.w2pageList_col_next:visible'
                ).first
                if await next_block.count():
                    await next_block.scroll_into_view_if_needed(timeout=3_000)
                    await next_block.click(timeout=5_000)
                    clicked = True
        except Exception:
            return False
        if not clicked:
            return False
        await self._wait_for_results_change(page, token, timeout_ms=10_000)
        if await self._results_token(page) == token:
            # 클릭했지만 내용이 그대로면 마지막 페이지다.
            return False
        await page.wait_for_timeout(int(self.options.delay * 1000))
        return True

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


def collect_results_sync(
    options: SearchOptions,
    on_court: Callable[[str, list[AuctionItem]], None] | None = None,
) -> list[AuctionItem]:
    return asyncio.run(CourtAuctionCrawler(options).collect_results(on_court))


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
