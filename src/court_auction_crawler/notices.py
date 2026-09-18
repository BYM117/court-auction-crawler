"""배당요구종기공고 수집 (G15).

**법원이 매각공고보다 몇 달 먼저 공개하는 사건을 여기서 얻는다.**

    경매개시결정 → 배당요구종기 공고 → (감정평가) → 매각공고(기일 14일 전) → 매각기일
         ↑ 이 화면                                        ↑ 기존 수집기는 여기부터

화면이 주는 것은 **사건 단위**다. 물건번호별 감정가·최저가가 없다(감정평가 전이라
당연하다). 그래서 이것만으로 화면에 못 올린다. **사건번호를 먼저 확보**해 두었다가
상세 수집기가 사건 화면에서 기본정보를 채우게 하는 것이 목적이다.

## 검색 단위 — 법원만으로는 안 된다

담당계를 안 고르면 **첫 계(경매1계)만** 검색된다. 실측으로 확인했다.

    서울중앙 담당계 안 고름 → 73건  (경매1계와 정확히 같다)
    경매1계 73 · 2계 24 · 3계 29 · 4계 33 · 5계 0   → 5개만 159건

소재지 검색(시/도·시/군/구)은 결과표가 아예 안 나온다. 그래서 **법원 × 담당계**가
유일하게 확인된 경로다. 빈 계가 흔하므로(서울중앙 경매5계 0건) 0건인 계를 기억해
건너뛰면 호출이 크게 준다 — 그건 호출자가 정한다.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

from playwright.async_api import Page, async_playwright

NOTICE_URL = ("https://www.courtauction.go.kr/pgj/index.on"
              "?w2xPath=/pgj/ui/pgj100/PGJ141M00.xml&pgjId=142M01")
COURT_SELECT = "#mf_wfm_mainFrame_sbx_ddltCortOfcLst"
DEPT_SELECT = "#mf_wfm_mainFrame_sbx_ddltCortDeptLst"
SEARCH_BUTTON = "#mf_wfm_mainFrame_btn_srchPbanc"

# 결과표는 한 사건이 두 줄이다.
#   6칸 행: 사건번호 · 소재지 · 소유자 · 공고일 · 담당계 · '목록'
#   3칸 행: 채무자 · 경매개시결정일자 · 배당요구종기일
_ROWS_JS = """() => {
  const table = [...document.querySelectorAll('table')].find(
    (item) => (item.caption ? item.caption.innerText : '').includes('배당요구종기일'));
  if (!table) return [];
  return [...table.querySelectorAll('tr')]
    .map((row) => [...row.querySelectorAll('td')].map(
      (cell) => (cell.innerText || '').replace(/\\s+/g, ' ').trim()))
    .filter((cells) => cells.length);
}"""

CASE_NO_RE = re.compile(r"(\d{4}타경\d+)")


def parse_rows(cells: list[list[str]]) -> list[dict[str, Any]]:
    """6칸 행과 뒤따르는 3칸 행을 한 건으로 묶는다.

    3칸 행이 없거나 모양이 다르면 **그 건을 버리지 않고** 빈 칸으로 둔다. 사건번호를
    얻는 것이 목적이라, 종기일을 못 읽었다고 사건을 통째로 놓치는 쪽이 더 나쁘다.
    """
    out: list[dict[str, Any]] = []
    for index, row in enumerate(cells):
        if len(row) < 6 or not CASE_NO_RE.search(row[0]):
            continue
        머리 = row[0].strip()
        사건 = CASE_NO_RE.search(머리)
        법원 = 머리[: 사건.start()].strip()
        뒤 = cells[index + 1] if index + 1 < len(cells) else []
        붙음 = len(뒤) == 3 and not CASE_NO_RE.search(뒤[0])
        out.append({
            "court": 법원,
            "case_no": 머리,
            "address": row[1].strip(),
            "owner": row[2].strip(),
            "notice_date": row[3].strip(),
            "dept": row[4].strip(),
            "debtor": 뒤[0].strip() if 붙음 else "",
            "opened_at": 뒤[1].strip() if 붙음 else "",
            "dividend_deadline": 뒤[2].strip() if 붙음 else "",
        })
    return out


async def _departments(page: Page, court: str) -> list[str]:
    await page.goto(NOTICE_URL, wait_until="domcontentloaded", timeout=30_000)
    await page.wait_for_timeout(1800)
    await page.select_option(COURT_SELECT, label=court)
    await page.wait_for_timeout(1500)
    names = await page.evaluate(
        "(sel) => { const e = document.querySelector(sel);"
        " return e ? [...e.options].map((o) => o.text.trim()) : []; }", DEPT_SELECT)
    # '담당계 선택' 안내와 '관리자'는 경매계가 아니다.
    return [n for n in names if n.startswith("경매")]


async def _search(page: Page, court: str, dept: str) -> list[dict[str, Any]]:
    await page.goto(NOTICE_URL, wait_until="domcontentloaded", timeout=30_000)
    await page.wait_for_timeout(1800)
    await page.select_option(COURT_SELECT, label=court)
    await page.wait_for_timeout(1400)
    await page.select_option(DEPT_SELECT, label=dept)
    await page.wait_for_timeout(700)
    await page.locator(SEARCH_BUTTON).click(timeout=10_000)
    await page.wait_for_timeout(3000)
    return parse_rows(await page.evaluate(_ROWS_JS))


async def collect_notices(
    courts: list[str],
    *,
    headful: bool = False,
    skip_depts: set[tuple[str, str]] | None = None,
    on_court: Any = None,
) -> tuple[list[dict[str, Any]], set[tuple[str, str]]]:
    """(수집한 행, 이번에 0건이던 (법원, 계)). 0건 집합은 다음 회차에 건너뛰라고 주는 것이다."""
    skip = skip_depts or set()
    rows: list[dict[str, Any]] = []
    empty: set[tuple[str, str]] = set()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not headful)
        page = await browser.new_page(viewport={"width": 1440, "height": 1000})
        try:
            for court in courts:
                depts = await _departments(page, court)
                걸린것 = 0
                for dept in depts:
                    if (court, dept) in skip:
                        continue
                    try:
                        got = await _search(page, court, dept)
                    except Exception as error:  # noqa: BLE001
                        print(f"  !! {court} {dept}: {type(error).__name__} {str(error)[:70]}")
                        continue
                    if got:
                        rows.extend(got)
                        걸린것 += len(got)
                    else:
                        empty.add((court, dept))
                if on_court:
                    # 이번 법원 몫을 넘겨 준다 — 호출자가 바로 저장할 수 있게.
                    # 다 돌고 한 번에 쓰면 중간에 죽을 때 앞의 것을 통째로 잃는다.
                    on_court(court, len(depts), 걸린것, rows[len(rows) - 걸린것:])
        finally:
            await browser.close()
    return rows, empty


def collect_notices_sync(courts: list[str], **kwargs: Any):
    return asyncio.run(collect_notices(courts, **kwargs))
