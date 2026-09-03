from __future__ import annotations

import asyncio
import base64
import binascii
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import mimetypes
from pathlib import Path
import re
import time
from typing import Any

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError, async_playwright

from .common import self_restart, singleton_lock, utc_now
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
# number 가 뒤 사건의 연도를 삼키면 법원 검색창에 없는 번호를 넣게 된다.
# store.CASE_NO_RE 와 같은 이유로 경계를 둔다.
CASE_NO_RE = re.compile(r"(?P<year>\d{4})타경(?P<number>\d+?)(?=\d{4}타경|\D|$)")
DATA_URI_RE = re.compile(r"^data:(?P<mime>[^;,]+)(?:;charset=[^;,]+)?;base64,(?P<data>.+)$", re.S)
DOCUMENT_TYPES = {
    "매각물건명세서": 7,
    "현황조사서": 14,
    "감정평가서": 14,
}
# 매각물건명세서 본문이 그려지는 뷰어. 경매 사이트가 아니라 법원 문서열람 시스템이다.
STREAMDOCS_FRAME_HINT = "streamdocs"
# 본문으로 인정할 최소 길이. 뷰어가 덜 그려졌을 때는 페이지 표시('1/5')뿐이라 10자 안팎이고,
# 실제 명세서는 1,300자를 넘는다(실측).
STREAMDOCS_MIN_CHARS = 300
STREAMDOCS_MAX_WAIT = 20.0


class HealthGovernor:
    """연속 인프라 오류(타임아웃·네트워크)를 차단 징후로 보고 자동으로 감속한다.

    - 연속 distress가 임계치에 닿으면: 냉각(반복될수록 배증) 후 단일 워커 +
      delay 3배의 보수 모드로 내려간다.
    - 보수 모드에서 연속 성공이 쌓이면 정상 속도로 자동 복귀한다.
    '검색 결과 없음' 같은 정상 응답은 사이트가 멀쩡하다는 뜻이므로 세지 않는다."""

    def __init__(
        self,
        *,
        trip_threshold: int = 5,
        base_cooldown_seconds: float = 60.0,
        max_cooldown_seconds: float = 900.0,
        recovery_streak: int = 10,
        stall_limit_seconds: float = 1800.0,
        min_attempts_for_stall: int = 5,
        throughput_window_seconds: float = 900.0,
        min_throughput_attempts: int = 8,
        min_throughput_success_ratio: float = 0.25,
    ) -> None:
        self.trip_threshold = trip_threshold
        self.base_cooldown_seconds = base_cooldown_seconds
        self.max_cooldown_seconds = max_cooldown_seconds
        self.recovery_streak = recovery_streak
        self.stall_limit_seconds = stall_limit_seconds
        self.min_attempts_for_stall = min_attempts_for_stall
        self.throughput_window_seconds = throughput_window_seconds
        self.min_throughput_attempts = min_throughput_attempts
        self.min_throughput_success_ratio = min_throughput_success_ratio
        self.distress_count = 0
        self.success_streak = 0
        self.trips = 0
        self.degraded = False
        self.cooldown_until = 0.0
        self.last_healthy_at = time.monotonic()
        self.attempts_since_healthy = 0
        self.abort_requested = False
        # 처리량(성공률) 측정 창: 반오염(느리게라도 간간이 성공)을 잡는다.
        self.window_start = time.monotonic()
        self.window_success = 0
        self.window_attempts = 0

    def record_healthy(self) -> None:
        self.distress_count = 0
        self.success_streak += 1
        self.last_healthy_at = time.monotonic()
        self.attempts_since_healthy = 0
        self.window_success += 1
        self.window_attempts += 1
        if self.degraded and self.success_streak >= self.recovery_streak:
            self.degraded = False
            self.success_streak = 0
            print("== 상태 양호: 정상 속도로 복귀합니다 ==")

    def record_distress(self) -> None:
        self.success_streak = 0
        self.distress_count += 1
        self.attempts_since_healthy += 1
        self.window_attempts += 1
        if self.distress_count < self.trip_threshold:
            return
        self.distress_count = 0
        self.trips += 1
        cooldown = min(
            self.base_cooldown_seconds * (2 ** (self.trips - 1)),
            self.max_cooldown_seconds,
        )
        self.cooldown_until = time.monotonic() + cooldown
        self.degraded = True
        print(
            f"!! 차단 의심: 연속 오류 {self.trip_threshold}회 -> "
            f"{int(cooldown)}초 냉각 후 단일 워커 보수 모드로 전환합니다"
        )

    def delay_multiplier(self) -> float:
        return 3.0 if self.degraded else 1.0

    def is_stalled(self, now: float | None = None) -> bool:
        """정상 수집이 stall_limit 동안 한 건도 없으면(시도는 있었는데) 갇힌 상태다.
        브라우저 세션 오염 등으로 보수 모드에서 영원히 못 빠져나오는 경우를 잡는다."""
        current = time.monotonic() if now is None else now
        return (
            self.attempts_since_healthy >= self.min_attempts_for_stall
            and current - self.last_healthy_at > self.stall_limit_seconds
        )

    def is_throughput_degraded(self, now: float | None = None) -> bool:
        """측정 창이 찰 때마다 성공률을 평가한다. 완전 정체(is_stalled)는 아니지만
        느리게라도 간간이 성공하는 '반오염' 상태(맥 잠자기 복귀 후 흔함)를 잡는다.
        평가 시 창을 리셋하므로 워치독에서만 호출한다."""
        current = time.monotonic() if now is None else now
        if current - self.window_start < self.throughput_window_seconds:
            return False
        attempts = self.window_attempts
        success = self.window_success
        self.window_start = current
        self.window_attempts = 0
        self.window_success = 0
        if attempts < self.min_throughput_attempts:
            return False
        return success < attempts * self.min_throughput_success_ratio

    async def wait_turn(self, worker_index: int) -> None:
        while not self.abort_requested:
            remaining = self.cooldown_until - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(min(remaining, 5.0))
                continue
            if self.degraded and worker_index != 0:
                # 보수 모드에서는 0번 워커만 진행하고 나머지는 복귀를 기다린다.
                await asyncio.sleep(5.0)
                continue
            return


def is_benign_case_error(exc: Exception) -> bool:
    """사이트가 정상 응답한 실패(사건 없음·형식 오류 등)는 차단 징후가 아니다."""
    return isinstance(exc, (LookupError, ValueError, KeyError))


@dataclass(slots=True)
class DetailCollectionSummary:
    targets: int = 0
    cases: int = 0
    collected: int = 0
    failed: int = 0
    unavailable: int = 0
    documents_collected: int = 0
    documents_pending: int = 0
    aborted: bool = False  # 자가 복구로 패스를 중단함 — 곧바로 새 패스를 시작해야 한다


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
        workers: int = 3,
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
        total_cases = len(grouped)
        queue: asyncio.Queue[tuple[tuple[str, str], list[dict[str, Any]]]] = asyncio.Queue()
        for entry in grouped.items():
            queue.put_nowait(entry)
        progress = {"index": 0}
        worker_count = max(1, min(workers, total_cases))
        governor = HealthGovernor()

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=not self.headful)

            # 워커마다 독립 컨텍스트(쿠키·세션 분리)를 써서 서로 다른 사건을
            # 동시에 처리한다. 워커별 예의 delay는 그대로 유지되므로 사이트
            # 입장에서는 동시 사용자 worker_count명 수준이다.
            async def run_worker(worker_index: int) -> None:
                context = await browser.new_context(viewport={"width": 1440, "height": 1100})
                page = await context.new_page()
                try:
                    while not governor.abort_requested:
                        await governor.wait_turn(worker_index)
                        if governor.abort_requested:
                            break
                        try:
                            (court, case_no), case_targets = queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                        progress["index"] += 1
                        summary.cases += 1
                        print(
                            f"[상세 {progress['index']}/{total_cases}] {court} {case_no} "
                            f"({len(case_targets)}개 물건)"
                        )
                        try:
                            result = await self._collect_case(page, court, case_no, case_targets)
                        except Exception as exc:
                            error = str(exc)[:500]
                            for target in case_targets:
                                self.store.mark_detail_failure(target["item_key"], error)
                                summary.failed += 1
                            print(f"  !! 상세 수집 실패: {error}")
                            if is_benign_case_error(exc):
                                governor.record_healthy()
                            else:
                                governor.record_distress()
                            await page.wait_for_timeout(
                                int(self.delay * 1000 * governor.delay_multiplier())
                            )
                            continue
                        governor.record_healthy()
                        summary.collected += result["collected"]
                        summary.failed += result["failed"]
                        summary.unavailable += result["unavailable"]
                        summary.documents_collected += result["documents_collected"]
                        summary.documents_pending += result["documents_pending"]
                        await page.wait_for_timeout(
                            int(self.delay * 1000 * governor.delay_multiplier())
                        )
                finally:
                    await context.close()

            async def watchdog() -> None:
                # 브라우저 세션 오염 등으로 정상 수집이 장시간 끊기면 거버너의
                # 보수 모드만으로는 못 빠져나온다(실측: 이틀 정체). 이번 패스를
                # 중단시켜 새 브라우저로 재시작하고, 워커가 그마저 못 멈추면
                # 프로세스를 종료해 launchd가 깨끗하게 되살리게 한다.
                hard_deadline: float | None = None
                while True:
                    await asyncio.sleep(15)
                    now = time.monotonic()
                    if not governor.abort_requested:
                        stalled = governor.is_stalled(now)
                        throttled = governor.is_throughput_degraded(now)
                        if stalled or throttled:
                            governor.abort_requested = True
                            summary.aborted = True
                            hard_deadline = now + 180
                            reason = (
                                f"{int(governor.stall_limit_seconds // 60)}분 이상 정상 수집 없음"
                                if stalled
                                else "성공률 급락(반오염 의심)"
                            )
                            print(
                                f"!! 자가 복구: {reason} -> 이번 패스를 중단하고 브라우저를 새로 엽니다"
                            )
                    if hard_deadline is not None and now > hard_deadline:
                        self_restart("!! 자가 복구 실패(워커 미응답) -> 프로세스를 종료합니다. launchd가 재시작합니다")

            watchdog_task = asyncio.create_task(watchdog())
            try:
                await asyncio.gather(*(run_worker(index) for index in range(worker_count)))
            finally:
                # 워치독은 browser.close()가 행에 걸리는 경우까지 지켜야 하므로
                # 브라우저를 닫은 뒤에 취소한다.
                try:
                    await browser.close()
                finally:
                    watchdog_task.cancel()
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
        result = await page.evaluate(
            """
            ({ selector, court }) => {
              const select = document.querySelector(selector);
              if (!select) return { state: 'no_select', optionCount: 0 };
              const options = [...select.options];
              const labelOf = (option) => (option.textContent || '').trim();
              const option =
                options.find((item) => labelOf(item) === court)
                || options.find((item) => labelOf(item).includes(court))
                || options.find((item) => labelOf(item).length >= 3 && court.includes(labelOf(item)));
              if (!option) return { state: 'no_match', optionCount: options.length };
              select.value = option.value;
              select.dispatchEvent(new Event('change', { bubbles: true }));
              select.dispatchEvent(new Event('input', { bubbles: true }));
              return { state: 'ok', label: labelOf(option) };
            }
            """,
            {"selector": COURT_SELECTOR, "court": court},
        )
        state = result.get("state") if isinstance(result, dict) else ""
        if state == "ok":
            await page.wait_for_timeout(300)
            return
        if state == "no_match" and int(result.get("optionCount", 0)) > 1:
            # 옵션은 정상 로드됐는데 이름이 안 맞는 것 — 데이터 문제(양성 오류)
            raise LookupError(f"사건검색 법원 옵션에 없음: {court}")
        # 셀렉트가 없거나 옵션이 비어 있으면 화면 미로딩 — 인프라 장애로 취급해
        # 거버너가 차단 징후로 세도록 한다 (양성으로 분류하면 정체를 못 잡는다)
        raise RuntimeError(
            f"사건검색 화면 미로딩 (법원 옵션 {int(result.get('optionCount', 0))}개): {court}"
        )

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
                    try:
                        await popup.wait_for_function(
                            "() => document.body && document.body.innerText.trim().length > 40",
                            timeout=6_000,
                        )
                    except PlaywrightTimeoutError:
                        pass
                    await popup.wait_for_timeout(200)
                    metadata = await extract_tables(popup)
                    title = find_document_title(metadata) or document_type
                    # 이 팝업(매각물건명세서)은 본문이 표가 아니라 다른 시스템의
                    # StreamDocs 뷰어 iframe 안에 그려진다. 팝업 자체에서 뽑을 수 있는 건
                    # 사건 정보와 문서 목록뿐이라, 뷰어에서 본문 텍스트를 따로 가져온다.
                    body_text = await self._read_streamdocs_text(popup)
                    download = (
                        await self._download_document(popup, target["item_key"], document_type)
                        if self.download_document_files
                        else None
                    )
                    # 예전에는 파일을 받았을 때만 collected로 쳤다. 그래서 파일 저장을 끄고
                    # 돌리는 평소 운영에서는 이 문서가 영원히 collected가 되지 못했다.
                    # 다른 두 문서와 같이 '본문을 확보했는가'로 판정한다.
                    has_content = len(body_text) >= STREAMDOCS_MIN_CHARS or bool(download)
                    self.store.save_document_status(
                        target["item_key"],
                        document_type,
                        status="collected" if has_content else "metadata_only",
                        title=title,
                        source_url=popup.url,
                        file_path=download.get("file_path", "") if download else "",
                        content_type=download.get("content_type", "") if download else "",
                        file_size=download.get("file_size", 0) if download else 0,
                        sha256=download.get("sha256", "") if download else "",
                        metadata={
                            "tables": metadata,
                            "text": body_text,
                            "capture_method": (
                                download.get("capture_method", "") if download else
                                ("streamdocs_text" if body_text else "")
                            ),
                        },
                        next_retry_at="" if has_content else retry_after(hours=12),
                    )
                    result["collected" if has_content else "pending"] += 1
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
            await page.wait_for_timeout(500)
        return result

    async def _read_streamdocs_text(self, popup: Page) -> str:
        """StreamDocs 뷰어 iframe에서 문서 본문 텍스트를 읽는다.

        뷰어는 문서를 그려 넣는 데 시간이 들쭉날쭉하다(실측 7~8초, 때로 그 이상).
        고정 대기로는 페이지 표시('1/5')만 잡히고 본문을 놓치므로, 내용이 임계치를
        넘고 두 번 연속 같아질 때까지 기다린다. 텍스트 레이어가 전 페이지를 한 번에
        담고 있어 페이지를 넘길 필요는 없다."""
        deadline = time.monotonic() + STREAMDOCS_MAX_WAIT
        last = ""
        stable = 0
        while time.monotonic() < deadline:
            frame = next((f for f in popup.frames if STREAMDOCS_FRAME_HINT in f.url), None)
            if frame is not None:
                try:
                    text = await frame.evaluate(
                        "() => document.body ? document.body.innerText : ''"
                    )
                except Exception:  # noqa: BLE001 - 렌더 도중 프레임이 갈리면 다음 회차에 다시 본다
                    text = ""
                if len(text) >= STREAMDOCS_MIN_CHARS:
                    if text == last:
                        stable += 1
                        if stable >= 2:
                            return text.strip()
                    else:
                        stable = 0
                last = text
            await popup.wait_for_timeout(600)
        return last.strip() if len(last) >= STREAMDOCS_MIN_CHARS else ""

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
        # 고정 대기 대신 다이얼로그에 내용이 붙는 것을 확인한다.
        try:
            await dialog.locator("table, iframe, p, pre").first.wait_for(state="attached", timeout=5_000)
        except PlaywrightTimeoutError:
            pass
        await page.wait_for_timeout(200)
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
                    try:
                        await frame.wait_for_function(
                            "() => (document.body && document.body.innerText.trim().length > 40)"
                            " || document.querySelector('table, embed, object, img')",
                            timeout=6_000,
                        )
                    except PlaywrightTimeoutError:
                        pass
                    await frame.wait_for_timeout(200)
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
        await page.wait_for_timeout(300)
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
            # 사이트가 JPEG를 image/png로 알려주는 경우가 많다(실측 표본 300건 중
            # 286건이 실제로는 JPEG). 알려준 값을 그대로 믿으면 파일 확장자와
            # Content-Type이 전부 어긋난다. 바이트가 말하는 쪽을 우선한다.
            mime = sniff_image_mime(content) or mime
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


IMAGE_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def sniff_image_mime(content: bytes) -> str:
    """이미지 바이트 앞머리로 실제 형식을 판정한다. 모르면 빈 문자열."""
    for signature, mime in IMAGE_SIGNATURES:
        if content.startswith(signature):
            return mime
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    return ""


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


def safe_path_part(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z가-힣._-]+", "_", str(value)).strip("._")
    return cleaned[:160] or "unknown"


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
    workers: int = 3,
) -> DetailCollectionSummary:
    with singleton_lock(store.db_path.parent / "collect-details.pid") as acquired:
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
                workers=workers,
            )
        )
