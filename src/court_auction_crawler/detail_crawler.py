from __future__ import annotations

import asyncio
import base64
import binascii
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import mimetypes
import os
from pathlib import Path
import re
from typing import Any, Iterator

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError, async_playwright

from .crawler import _prefer_local_browser_cache
from .store import AuctionStore, representative_case_no


COURT_CASE_SEARCH_URL = (
    "https://www.courtauction.go.kr/pgj/index.on?"
    "w2xPath=/pgj/ui/pgj100/PGJ159M00.xml"
)
COURT_SELECTOR = "#mf_wfm_mainFrame_sbx_auctnCsSrchCortOfc"
CASE_YEAR_SELECTOR = "#mf_wfm_mainFrame_sbx_auctnCsSrchCsYear"
CASE_NUMBER_SELECTOR = "#mf_wfm_mainFrame_ibx_auctnCsSrchCsNo"
CASE_SEARCH_BUTTON_SELECTOR = "#mf_wfm_mainFrame_btn_auctnCsSrchBtn"
CASE_TAB_SELECTOR = "#mf_wfm_mainFrame_tac_srchRsltDvs_tab_tabs1_tabHTML"
SCHEDULE_TAB_SELECTOR = "#mf_wfm_mainFrame_tac_srchRsltDvs_tab_tabs2_tabHTML"
FILING_TAB_SELECTOR = "#mf_wfm_mainFrame_tac_srchRsltDvs_tab_tabs3_tabHTML"
ITEM_DETAIL_BUTTON_SELECTOR = "input[value='물건상세조회']"
CASE_DETAIL_BUTTON_SELECTOR = "input[value='사건상세조회']"
CASE_NO_RE = re.compile(r"(?P<year>\d{4})타경(?P<number>\d+)")
DATA_URI_RE = re.compile(r"^data:(?P<mime>[^;,]+)(?:;charset=[^;,]+)?;base64,(?P<data>.+)$", re.S)
DOCUMENT_TYPES = {
    "매각물건명세서": 7,
    "현황조사서": 14,
    "감정평가서": 14,
}


@dataclass(slots=True)
class DetailCollectionSummary:
    targets: int = 0
    cases: int = 0
    collected: int = 0
    failed: int = 0
    unavailable: int = 0
    documents_collected: int = 0
    documents_pending: int = 0


class CourtAuctionDetailCrawler:
    def __init__(
        self,
        store: AuctionStore,
        *,
        asset_dir: str | Path = "data/auction-assets",
        delay: float = 1.5,
        headful: bool = False,
        collect_documents: bool = True,
        download_document_files: bool = False,
    ) -> None:
        self.store = store
        self.asset_dir = Path(asset_dir)
        self.delay = max(delay, 0.3)
        self.headful = headful
        self.collect_documents = collect_documents
        self.download_document_files = download_document_files

    async def collect_due(
        self,
        *,
        limit: int | None = None,
        include_inactive: bool = False,
        force: bool = False,
        item_key: str = "",
    ) -> DetailCollectionSummary:
        targets = self.store.list_detail_targets(
            limit=limit,
            include_inactive=include_inactive,
            force=force,
            item_key=item_key,
        )
        summary = DetailCollectionSummary(targets=len(targets))
        if not targets:
            return summary

        grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for target in targets:
            case_no = representative_case_no(target.get("case_no", ""))
            grouped[(target.get("court", ""), case_no)].append(target)

        self.asset_dir.mkdir(parents=True, exist_ok=True)
        _prefer_local_browser_cache()
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=not self.headful)
            page = await browser.new_page(viewport={"width": 1440, "height": 1100})
            try:
                total_cases = len(grouped)
                for index, ((court, case_no), case_targets) in enumerate(grouped.items(), start=1):
                    summary.cases += 1
                    print(f"[상세 {index}/{total_cases}] {court} {case_no} ({len(case_targets)}개 물건)")
                    try:
                        result = await self._collect_case(page, court, case_no, case_targets)
                    except Exception as exc:
                        error = str(exc)[:500]
                        for target in case_targets:
                            self.store.mark_detail_failure(target["item_key"], error)
                            summary.failed += 1
                        print(f"  !! 상세 수집 실패: {error}")
                        await page.wait_for_timeout(int(self.delay * 1000))
                        continue
                    summary.collected += result["collected"]
                    summary.failed += result["failed"]
                    summary.unavailable += result["unavailable"]
                    summary.documents_collected += result["documents_collected"]
                    summary.documents_pending += result["documents_pending"]
                    await page.wait_for_timeout(int(self.delay * 1000))
            finally:
                await browser.close()
        return summary

    async def _collect_case(
        self,
        page: Page,
        court: str,
        case_no: str,
        targets: list[dict[str, Any]],
    ) -> dict[str, int]:
        match = CASE_NO_RE.search(case_no)
        if not match:
            raise ValueError(f"사건번호 형식 오류: {case_no}")

        await self._open_case_search(page)
        await self._select_court_option(page, court)
        await page.locator(CASE_YEAR_SELECTOR).select_option(label=match.group("year"))
        await page.locator(CASE_NUMBER_SELECTOR).fill(match.group("number"))
        await page.locator(CASE_SEARCH_BUTTON_SELECTOR).click()
        # 고정 1초 대기는 응답이 늦으면 '검색 결과 없음' 오탐으로 케이스 전체를
        # 실패 처리한다. 버튼이 붙을 때까지 기다린다.
        try:
            await page.wait_for_selector(ITEM_DETAIL_BUTTON_SELECTOR, state="attached", timeout=15_000)
        except PlaywrightTimeoutError:
            pass
        await page.wait_for_timeout(300)

        buttons = page.locator(ITEM_DETAIL_BUTTON_SELECTOR)
        button_count = await buttons.count()
        if button_count == 0:
            text = await page.locator("main, body").first.inner_text(timeout=5_000)
            raise LookupError(f"사건 검색 결과 없음: {text[-200:]}")

        # 활성/비활성을 함께 스냅샷해서, 종결·취하로 버튼이 영구 비활성인 물건을
        # 실패(재시도 대상)가 아니라 unavailable(수집 불가)로 구분한다.
        button_states: list[tuple[str, bool]] = []
        for index in range(button_count):
            button = buttons.nth(index)
            button_states.append((await item_number_for_button(button), await button.is_disabled()))

        shared = await self._extract_case_shared(page)
        target_by_no = {str(row.get("item_no", "")): row for row in targets}
        seen_item_nos: set[str] = set()
        counts = {
            "collected": 0,
            "failed": 0,
            "unavailable": 0,
            "documents_collected": 0,
            "documents_pending": 0,
        }

        enabled_indices = [index for index, (_no, disabled) in enumerate(button_states) if not disabled]
        for position, button_index in enumerate(enabled_indices):
            current_buttons = page.locator(ITEM_DETAIL_BUTTON_SELECTOR)
            if button_index >= await current_buttons.count():
                break
            button = current_buttons.nth(button_index)
            button_item_no = button_states[button_index][0]
            try:
                await button.click(timeout=10_000)
                await page.wait_for_selector(CASE_DETAIL_BUTTON_SELECTOR, state="visible", timeout=15_000)
                detail = await self._extract_item_detail(page)
            except Exception as exc:
                # 한 물건의 진입 실패가 사건 전체를 실패로 만들지 않게 격리한다.
                target = target_by_no.get(button_item_no)
                if target is not None:
                    seen_item_nos.add(button_item_no)
                    self.store.mark_detail_failure(
                        target["item_key"], f"상세 화면 진입 실패: {str(exc)[:300]}"
                    )
                    counts["failed"] += 1
                    print(f"  !! 물건 {button_item_no} 상세 진입 실패: {str(exc)[:200]}")
                await self._return_to_case_detail(page)
                continue
            item_no = button_item_no or str(detail.get("item_no", ""))
            detail["item_no"] = item_no
            target = target_by_no.get(item_no)
            if target is not None:
                seen_item_nos.add(item_no)
                try:
                    photos = await self._save_photos(page, target["item_key"], detail.pop("photo_data", []))
                    detail["photos"] = photos
                    detail["case"] = shared
                    self.store.save_item_detail(target["item_key"], detail)
                    counts["collected"] += 1
                    if self.collect_documents:
                        doc_counts = await self._collect_item_documents(page, target)
                        counts["documents_collected"] += doc_counts["collected"]
                        counts["documents_pending"] += doc_counts["pending"]
                    else:
                        for document_type in DOCUMENT_TYPES:
                            self.store.save_document_status(
                                target["item_key"],
                                document_type,
                                status="pending",
                                next_retry_at=utc_now(),
                                error="이번 실행에서 문서 수집 생략",
                            )
                            counts["documents_pending"] += 1
                except Exception as exc:
                    self.store.mark_detail_failure(target["item_key"], str(exc)[:500])
                    counts["failed"] += 1
                    print(f"  !! 물건 {item_no} 상세 처리 실패: {str(exc)[:300]}")
            elif item_no:
                print(f"  -- 화면 물건번호 {item_no}는 이번 수집 대상이 아님")
            else:
                print("  !! 상세 화면에서 물건번호를 읽지 못함")

            if position < len(enabled_indices) - 1:
                await self._return_to_case_detail(page)

        disabled_by_no = {no: disabled for no, disabled in button_states if no}
        for item_no, target in target_by_no.items():
            if item_no in seen_item_nos:
                continue
            if disabled_by_no.get(item_no) is True:
                self.store.mark_detail_unavailable(
                    target["item_key"], "물건상세조회 버튼 비활성 (종결·취하 등으로 조회 불가)"
                )
                counts["unavailable"] += 1
            else:
                self.store.mark_detail_failure(
                    target["item_key"], f"물건번호 {item_no} 상세 버튼을 찾지 못함"
                )
                counts["failed"] += 1
        return counts

    async def _select_court_option(self, page: Page, court: str) -> None:
        """목록 화면은 '동부지원'처럼 축약된 법원명을 쓰는데 사건검색 셀렉트는
        '부산동부지원' 같은 전체 명칭이라 라벨 완전일치가 실패한다. 포함 관계로 매칭한다."""
        selected = await page.evaluate(
            """
            ({ selector, court }) => {
              const select = document.querySelector(selector);
              if (!select) return '';
              const options = [...select.options];
              const labelOf = (option) => (option.textContent || '').trim();
              const option =
                options.find((item) => labelOf(item) === court)
                || options.find((item) => labelOf(item).includes(court))
                || options.find((item) => labelOf(item).length >= 3 && court.includes(labelOf(item)));
              if (!option) return '';
              select.value = option.value;
              select.dispatchEvent(new Event('change', { bubbles: true }));
              select.dispatchEvent(new Event('input', { bubbles: true }));
              return labelOf(option);
            }
            """,
            {"selector": COURT_SELECTOR, "court": court},
        )
        if not selected:
            raise LookupError(f"사건검색 법원 옵션을 찾지 못함: {court}")
        await page.wait_for_timeout(300)

    async def _open_case_search(self, page: Page) -> None:
        # SPA라 URL로는 화면 상태를 알 수 없다. 이전 사건의 상세 화면에 남아 있으면
        # 검색폼이 가려져 있으므로, 짧게 확인하고 바로 새로 연다.
        # (URL만 보고 goto를 생략하면 케이스마다 20초 타임아웃을 지불한다.)
        try:
            await page.wait_for_selector(COURT_SELECTOR, state="visible", timeout=2_000)
            return
        except PlaywrightTimeoutError:
            pass
        await page.goto(COURT_CASE_SEARCH_URL, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_selector(COURT_SELECTOR, state="visible", timeout=20_000)

    async def _extract_case_shared(self, page: Page) -> dict[str, Any]:
        case_tables = await extract_tables(page)
        await self._click_if_present(page, SCHEDULE_TAB_SELECTOR)
        schedule_tables = await extract_tables(page)
        await self._click_if_present(page, FILING_TAB_SELECTOR)
        filing_tables = await extract_tables(page)
        await self._click_if_present(page, CASE_TAB_SELECTOR)
        return {
            "case_tables": case_tables,
            "schedule_tables": schedule_tables,
            "filing_and_service_tables": filing_tables,
        }

    async def _extract_item_detail(self, page: Page) -> dict[str, Any]:
        try:
            await page.wait_for_selector(
                "img[alt*='전경도'], img[alt*='내부구조도']",
                state="attached",
                timeout=3_000,
            )
        except PlaywrightTimeoutError:
            pass
        tables = await extract_tables(page)
        sections = await extract_sections(page)
        photos = await page.evaluate(
            """
            () => [...document.querySelectorAll('img')]
              .map((img, domIndex) => ({
                label: (img.alt || img.title || '').trim(),
                src: img.src || '',
                dom_index: domIndex,
              }))
              .filter((item) => item.src && /전경도|내부구조도/.test(item.label))
            """
        )
        item_no = find_table_value(tables, "물건번호")
        return {
            "item_no": item_no,
            "tables": tables,
            "sections": sections,
            "photo_data": photos,
            "source_url": page.url,
        }

    async def _collect_item_documents(
        self,
        page: Page,
        target: dict[str, Any],
    ) -> dict[str, int]:
        result = {"collected": 0, "pending": 0}
        existing = self.store.document_statuses(target["item_key"])
        pending_documents = [
            (document_type, lead_days)
            for document_type, lead_days in DOCUMENT_TYPES.items()
            if existing.get(document_type) != "collected"
        ]
        for document_type, lead_days in pending_documents:
            popup: Page | None = None
            try:
                button = page.locator(f"input[value='{document_type}'], button:has-text('{document_type}')")
                visible_button = None
                for index in range(await button.count()):
                    candidate = button.nth(index)
                    if await candidate.is_visible() and await candidate.is_enabled():
                        visible_button = candidate
                        break
                if visible_button is None:
                    next_retry = document_next_retry(target.get("sale_date", ""), lead_days)
                    self.store.save_document_status(
                        target["item_key"],
                        document_type,
                        status="pending",
                        error="문서 보기 버튼 없음",
                        next_retry_at=next_retry or retry_after(days=1),
                    )
                    result["pending"] += 1
                elif document_type in {"현황조사서", "감정평가서"}:
                    metadata = await self._collect_inline_document(page, visible_button)
                    resource_download = None
                    if document_type == "감정평가서":
                        pdf_url = next(
                            (
                                url
                                for url in metadata.get("iframe", {}).get("resources", [])
                                if str(url).lower().split("?", 1)[0].endswith(".pdf")
                            ),
                            "",
                        )
                        if pdf_url and self.download_document_files:
                            resource_download = await self._download_resource(
                                page,
                                target["item_key"],
                                document_type,
                                pdf_url,
                            )
                    has_content = len(metadata.get("text", "")) >= 200 or bool(
                        metadata.get("iframe", {}).get("text")
                        or metadata.get("iframe", {}).get("tables")
                        or metadata.get("iframe", {}).get("resources")
                    )
                    self.store.save_document_status(
                        target["item_key"],
                        document_type,
                        status="collected" if has_content else "metadata_only",
                        title=document_type,
                        source_url=page.url,
                        file_path=resource_download.get("file_path", "") if resource_download else "",
                        content_type=resource_download.get("content_type", "") if resource_download else "",
                        file_size=resource_download.get("file_size", 0) if resource_download else 0,
                        sha256=resource_download.get("sha256", "") if resource_download else "",
                        metadata=metadata,
                        next_retry_at="" if has_content else retry_after(hours=12),
                    )
                    result["collected" if has_content else "pending"] += 1
                else:
                    async with page.expect_popup(timeout=20_000) as popup_info:
                        await visible_button.click()
                    popup = await popup_info.value
                    await popup.wait_for_load_state("domcontentloaded", timeout=20_000)
                    await popup.wait_for_timeout(1_500)
                    metadata = await extract_tables(popup)
                    title = find_document_title(metadata) or document_type
                    download = (
                        await self._download_document(popup, target["item_key"], document_type)
                        if self.download_document_files
                        else None
                    )
                    status = "collected" if download else "metadata_only"
                    self.store.save_document_status(
                        target["item_key"],
                        document_type,
                        status=status,
                        title=title,
                        source_url=popup.url,
                        file_path=download.get("file_path", "") if download else "",
                        content_type=download.get("content_type", "") if download else "",
                        file_size=download.get("file_size", 0) if download else 0,
                        sha256=download.get("sha256", "") if download else "",
                        metadata={
                            "tables": metadata,
                            "capture_method": download.get("capture_method", "") if download else "",
                        },
                        next_retry_at="" if download else retry_after(hours=12),
                    )
                    if download:
                        result["collected"] += 1
                    else:
                        result["pending"] += 1
            except Exception as exc:
                self.store.save_document_status(
                    target["item_key"],
                    document_type,
                    status="pending",
                    error=str(exc)[:500],
                    next_retry_at=retry_after(hours=12),
                )
                result["pending"] += 1
            finally:
                if popup is not None and not popup.is_closed():
                    await popup.close()
            await page.wait_for_timeout(1_000)
        return result

    async def _download_resource(
        self,
        page: Page,
        item_key: str,
        document_type: str,
        url: str,
    ) -> dict[str, Any] | None:
        response = await page.context.request.get(url, headers={"Referer": page.url}, timeout=30_000)
        if not response.ok:
            return None
        content = await response.body()
        if len(content) < 1_000:
            return None
        content_type = response.headers.get("content-type", "application/pdf").split(";", 1)[0]
        suffix = Path(url.split("?", 1)[0]).suffix or mimetypes.guess_extension(content_type) or ".bin"
        target_dir = self.asset_dir / safe_path_part(item_key) / "documents"
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / f"{safe_path_part(document_type)}{suffix}"
        target_path.write_bytes(content)
        return {
            "file_path": str(target_path.resolve()),
            "content_type": content_type,
            "file_size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "capture_method": "linked_resource",
        }

    async def _collect_inline_document(self, page: Page, button: Any) -> dict[str, Any]:
        await button.click()
        dialog = page.locator("[role='dialog']:visible").last
        await dialog.wait_for(state="visible", timeout=15_000)
        await page.wait_for_timeout(800)
        try:
            metadata: dict[str, Any] = {
                "tables": await extract_tables_from_locator(dialog),
                "text": (await dialog.inner_text()).strip(),
            }
            iframe_locator = dialog.locator("iframe")
            if await iframe_locator.count():
                handle = await iframe_locator.first.element_handle()
                frame = await handle.content_frame() if handle is not None else None
                if frame is not None:
                    await frame.wait_for_load_state("domcontentloaded", timeout=20_000)
                    await frame.wait_for_timeout(1_500)
                    metadata["iframe"] = {
                        "url": frame.url,
                        "tables": await extract_tables(frame),
                        "sections": await extract_sections(frame),
                        "text": (await frame.locator("body").inner_text()).strip(),
                        "resources": await frame.evaluate(
                            """
                            () => [...document.querySelectorAll('a,img,embed,object,iframe')]
                              .map((element) => element.href || element.src || element.data || '')
                              .filter((url) => /^https?:/.test(url))
                            """
                        ),
                    }
            return metadata
        finally:
            close_buttons = dialog.locator("input[value='닫기'], button:has-text('닫기')")
            for index in range(await close_buttons.count()):
                candidate = close_buttons.nth(index)
                if await candidate.is_visible() and await candidate.is_enabled():
                    await candidate.click()
                    break
            await dialog.wait_for(state="hidden", timeout=10_000)

    async def _download_document(
        self,
        popup: Page,
        item_key: str,
        document_type: str,
    ) -> dict[str, Any] | None:
        buttons = popup.locator("input[value='파일저장'], button:has-text('파일저장')")
        visible_button = None
        for index in range(await buttons.count()):
            candidate = buttons.nth(index)
            if await candidate.is_visible():
                visible_button = candidate
                break
        if visible_button is None:
            return await self._capture_rendered_document(popup, item_key, document_type)
        try:
            async with popup.expect_download(timeout=12_000) as download_info:
                await visible_button.click()
            download = await download_info.value
            suffix = Path(download.suggested_filename).suffix or ".pdf"
            target_dir = self.asset_dir / safe_path_part(item_key) / "documents"
            target_dir.mkdir(parents=True, exist_ok=True)
            target_path = target_dir / f"{safe_path_part(document_type)}{suffix}"
            await download.save_as(target_path)
            content = target_path.read_bytes()
            return {
                "file_path": str(target_path.resolve()),
                "content_type": mimetypes.guess_type(target_path.name)[0] or "application/octet-stream",
                "file_size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "capture_method": "original_download",
            }
        except PlaywrightTimeoutError:
            return await self._capture_rendered_document(popup, item_key, document_type)

    async def _capture_rendered_document(
        self,
        popup: Page,
        item_key: str,
        document_type: str,
    ) -> dict[str, Any] | None:
        target_dir = self.asset_dir / safe_path_part(item_key) / "documents"
        target_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = target_dir / f"{safe_path_part(document_type)}-rendered.pdf"
        try:
            await popup.emulate_media(media="screen")
            await popup.pdf(
                path=pdf_path,
                format="A4",
                print_background=True,
                margin={"top": "8mm", "right": "8mm", "bottom": "8mm", "left": "8mm"},
            )
            if pdf_path.stat().st_size > 5_000:
                content = pdf_path.read_bytes()
                return {
                    "file_path": str(pdf_path.resolve()),
                    "content_type": "application/pdf",
                    "file_size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "capture_method": "rendered_pdf",
                }
        except Exception as exc:
            print(f"  -- {document_type} PDF 대체 저장 실패: {str(exc)[:200]}")

        image_path = target_dir / f"{safe_path_part(document_type)}-rendered.png"
        try:
            await popup.screenshot(path=image_path, full_page=True)
            content = image_path.read_bytes()
            if len(content) <= 20_000:
                return None
            return {
                "file_path": str(image_path.resolve()),
                "content_type": "image/png",
                "file_size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "capture_method": "rendered_screenshot",
            }
        except Exception as exc:
            print(f"  -- {document_type} 화면 대체 저장 실패: {str(exc)[:200]}")
            return None

    async def _return_to_case_detail(self, page: Page) -> None:
        button = page.locator(CASE_DETAIL_BUTTON_SELECTOR)
        try:
            if await button.count():
                await button.first.click(timeout=5_000)
                # 버튼이 다시 붙기 전에 다음 물건을 클릭하면 비활성 레이스가 난다.
                await page.wait_for_selector(ITEM_DETAIL_BUTTON_SELECTOR, state="attached", timeout=10_000)
                await page.wait_for_timeout(300)
                return
        except PlaywrightTimeoutError:
            pass
        await page.goto(COURT_CASE_SEARCH_URL, wait_until="domcontentloaded", timeout=30_000)

    async def _click_if_present(self, page: Page, selector: str) -> bool:
        locator = page.locator(selector)
        if not await locator.count():
            return False
        await locator.first.click()
        await page.wait_for_timeout(500)
        return True

    async def _save_photos(
        self,
        page: Page,
        item_key: str,
        photos: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        saved: list[dict[str, Any]] = []
        for index, photo in enumerate(photos, start=1):
            loaded = await self._load_photo(page, photo)
            if loaded is None:
                continue
            content, mime = loaded
            digest = hashlib.sha256(content).hexdigest()
            suffix = ".jpg" if "jpeg" in mime else ".png" if "png" in mime else ".gif" if "gif" in mime else ".bin"
            label = photo.get("label", "") or f"사진_{index}"
            target_dir = self.asset_dir / safe_path_part(item_key) / "photos"
            target_dir.mkdir(parents=True, exist_ok=True)
            target_path = target_dir / f"{index:02d}-{digest[:16]}{suffix}"
            if not target_path.exists():
                target_path.write_bytes(content)
            asset_id = self.store.save_asset(
                item_key,
                kind="photo",
                label=label,
                file_path=str(target_path.resolve()),
                content_type=mime,
                sha256=digest,
                file_size=len(content),
            )
            saved.append(
                {
                    "id": asset_id,
                    "label": label,
                    "content_type": mime,
                    "file_size": len(content),
                    "sha256": digest,
                    "source_url": photo.get("src", "") if str(photo.get("src", "")).startswith("http") else "",
                }
            )
        return saved

    async def _load_photo(self, page: Page, photo: dict[str, Any]) -> tuple[bytes, str] | None:
        source = str(photo.get("src", ""))
        match = DATA_URI_RE.match(source)
        if match:
            try:
                return base64.b64decode(match.group("data"), validate=False), match.group("mime").lower()
            except (ValueError, binascii.Error):
                return None

        if source.startswith(("http://", "https://")):
            try:
                response = await page.context.request.get(
                    source,
                    headers={"Referer": page.url},
                    timeout=20_000,
                )
                if response.ok:
                    content = await response.body()
                    mime = response.headers.get("content-type", "image/jpeg").split(";", 1)[0]
                    if content:
                        return content, mime
            except Exception:
                pass

        try:
            encoded = await page.evaluate(
                """
                async (url) => {
                  const response = await fetch(url);
                  const blob = await response.blob();
                  const bytes = new Uint8Array(await blob.arrayBuffer());
                  let binary = '';
                  for (let index = 0; index < bytes.length; index += 0x8000) {
                    binary += String.fromCharCode(...bytes.subarray(index, index + 0x8000));
                  }
                  return {mime: blob.type || 'image/jpeg', data: btoa(binary)};
                }
                """,
                source,
            )
            return base64.b64decode(encoded["data"]), str(encoded["mime"]).lower()
        except Exception:
            try:
                content = await page.locator("img").nth(int(photo.get("dom_index", -1))).screenshot()
                return content, "image/png"
            except Exception:
                return None


async def extract_tables(page: Page) -> list[dict[str, Any]]:
    return await page.evaluate(
        r"""
        () => {
          const clean = (value) => (value || '').replace(/\s+/g, ' ').trim();
          const cellText = (cell) => {
            const visible = clean(cell.innerText);
            if (visible) return visible;
            const values = [...cell.querySelectorAll('input,select,textarea')]
              .map((control) => {
                if (control.tagName === 'SELECT') {
                  return clean(control.selectedOptions?.[0]?.textContent || control.value);
                }
                return clean(control.value);
              })
              .filter(Boolean);
            return values.join(' ') || clean(cell.textContent);
          };
          return [...document.querySelectorAll('table')]
            .map((table) => ({
              caption: clean(table.getAttribute('summary') || table.querySelector('caption')?.innerText || ''),
              rows: [...table.querySelectorAll('tr')]
                .map((tr) => [...tr.querySelectorAll('th,td')].map(cellText).filter(Boolean))
                .filter((row) => row.length),
            }))
            .filter((table) => table.rows.length);
        }
        """
    )


async def item_number_for_button(button: Any) -> str:
    return await button.evaluate(
        r"""
        (element) => {
          const clean = (value) => (value || '').replace(/\s+/g, ' ').trim();
          const group = element.closest('tbody') || element.closest('table');
          if (!group) return '';
          const cells = [...group.querySelectorAll('th,td')];
          const label = cells.find((cell) => clean(cell.innerText || cell.textContent) === '물건번호');
          if (!label) return '';
          const valueCell = label.nextElementSibling;
          const text = clean(valueCell?.innerText || valueCell?.textContent);
          const match = text.match(/\d+/);
          return match ? match[0] : '';
        }
        """
    )


async def extract_sections(page: Page) -> list[dict[str, str]]:
    return await page.evaluate(
        r"""
        () => {
          const clean = (value) => (value || '').replace(/\s+/g, ' ').trim();
          return [...document.querySelectorAll('h2,h3,h4')].map((heading) => {
            const parts = [];
            let node = heading.nextElementSibling;
            while (node && !/^H[234]$/.test(node.tagName) && parts.join(' ').length < 30000) {
              const text = clean(node.innerText);
              if (text) parts.push(text);
              node = node.nextElementSibling;
            }
            return { title: clean(heading.innerText), text: parts.join(' ') };
          }).filter((section) => section.title && section.text);
        }
        """
    )


async def extract_tables_from_locator(locator: Any) -> list[dict[str, Any]]:
    return await locator.evaluate(
        r"""
        (root) => {
          const clean = (value) => (value || '').replace(/\s+/g, ' ').trim();
          const cellText = (cell) => {
            const visible = clean(cell.innerText);
            if (visible) return visible;
            const values = [...cell.querySelectorAll('input,select,textarea')]
              .map((control) => control.tagName === 'SELECT'
                ? clean(control.selectedOptions?.[0]?.textContent || control.value)
                : clean(control.value))
              .filter(Boolean);
            return values.join(' ') || clean(cell.textContent);
          };
          return [...root.querySelectorAll('table')]
            .map((table) => ({
              caption: clean(table.getAttribute('summary') || table.querySelector('caption')?.innerText || ''),
              rows: [...table.querySelectorAll('tr')]
                .map((tr) => [...tr.querySelectorAll('th,td')].map(cellText).filter(Boolean))
                .filter((row) => row.length),
            }))
            .filter((table) => table.rows.length);
        }
        """
    )


def find_table_value(tables: list[dict[str, Any]], label: str) -> str:
    for table in tables:
        for row in table.get("rows", []):
            for index, cell in enumerate(row[:-1]):
                if cell == label:
                    return str(row[index + 1])
    return ""


def find_document_title(tables: list[dict[str, Any]]) -> str:
    for table in tables:
        if "문서명" not in table.get("caption", ""):
            continue
        for row in table.get("rows", [])[1:]:
            if row:
                return str(row[-1])
    return ""


def document_next_retry(sale_date: str, lead_days: int) -> str:
    try:
        sale = date.fromisoformat(str(sale_date).replace(".", "-")[:10])
    except ValueError:
        return ""
    today = date.today()
    opens = sale - timedelta(days=lead_days)
    if today < opens:
        return datetime.combine(opens, datetime.min.time(), tzinfo=timezone.utc).isoformat(timespec="seconds")
    return ""


def retry_after(*, days: int = 0, hours: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days, hours=hours)).isoformat(timespec="seconds")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_path_part(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z가-힣._-]+", "_", str(value)).strip("._")
    return cleaned[:160] or "unknown"


@contextmanager
def _detail_singleton_lock(store: AuctionStore) -> Iterator[bool]:
    """상세 수집은 브라우저로 법원 사이트를 순회하므로 동시에 두 개가 돌면
    같은 사건을 중복 크롤링하고 사이트 부하만 배가 된다. pid 파일로 단일 실행을 보장한다."""
    lock_path = store.db_path.parent / "collect-details.pid"
    try:
        existing = int(lock_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        existing = None
    if existing and existing != os.getpid() and _process_is_running(existing):
        yield False
        return
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
    try:
        yield True
    finally:
        try:
            if int(lock_path.read_text(encoding="utf-8").strip()) == os.getpid():
                lock_path.unlink()
        except (OSError, ValueError):
            pass


def _process_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def collect_details_sync(
    store: AuctionStore,
    *,
    limit: int | None = None,
    include_inactive: bool = False,
    force: bool = False,
    item_key: str = "",
    asset_dir: str | Path = "data/auction-assets",
    delay: float = 1.5,
    headful: bool = False,
    collect_documents: bool = True,
    download_document_files: bool = False,
) -> DetailCollectionSummary:
    with _detail_singleton_lock(store) as acquired:
        if not acquired:
            print("상세 수집이 이미 실행 중이라 이번 실행은 건너뜁니다 (data/collect-details.pid).")
            return DetailCollectionSummary()
        crawler = CourtAuctionDetailCrawler(
            store,
            asset_dir=asset_dir,
            delay=delay,
            headful=headful,
            collect_documents=collect_documents,
            download_document_files=download_document_files,
        )
        return asyncio.run(
            crawler.collect_due(
                limit=limit,
                include_inactive=include_inactive,
                force=force,
                item_key=item_key,
            )
        )
