from __future__ import annotations

import json

from playwright.sync_api import sync_playwright


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.goto("https://www.courtauction.go.kr/", wait_until="domcontentloaded", timeout=30_000)
        page.get_by_text("물건상세검색", exact=True).first.click()
        page.wait_for_load_state("networkidle", timeout=30_000)
        page.wait_for_timeout(2_000)
        print(page.url)
        print(page.title())
        fields = page.evaluate(
            """
            () => [...document.querySelectorAll('input,select,button,a')]
              .slice(0, 260)
              .map((el) => ({
                tag: el.tagName,
                name: el.getAttribute('name'),
                id: el.id,
                type: el.getAttribute('type'),
                text: (el.innerText || el.value || el.getAttribute('title') || el.getAttribute('aria-label') || '')
                  .replace(/\\s+/g, ' ')
                  .trim(),
                placeholder: el.getAttribute('placeholder'),
                options: el.tagName === 'SELECT'
                  ? [...el.options].slice(0, 12).map((option) => option.textContent.trim())
                  : undefined,
              }))
            """
        )
        print(json.dumps(fields, ensure_ascii=False, indent=2))
        page.locator("#mf_wfm_mainFrame_btn_gdsDtlSrch").click()
        page.wait_for_load_state("networkidle", timeout=30_000)
        page.wait_for_timeout(3_000)
        print("AFTER_SEARCH")
        print(page.url)
        print(page.locator("body").inner_text(timeout=10_000)[:4_000])
        tables = page.evaluate(
            """
            () => [...document.querySelectorAll('table')]
              .map((table) => [...table.querySelectorAll('tr')]
                .slice(0, 8)
                .map((tr) => [...tr.querySelectorAll('th,td')].map((cell) => cell.innerText.replace(/\\s+/g, ' ').trim())))
              .filter((rows) => rows.length)
            """
        )
        print(json.dumps(tables[:8], ensure_ascii=False, indent=2))
        page.screenshot(path="/private/tmp/court-search.png", full_page=True)
        browser.close()


if __name__ == "__main__":
    main()
