import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from court_auction_crawler.detail_crawler import (
    CourtAuctionDetailCrawler,
    HealthGovernor,
    case_search_error,
    collect_details_sync,
    document_next_retry,
    find_document_title,
    find_table_value,
    is_benign_case_error,
    safe_path_part,
    site_message,
    sniff_image_mime,
)
from court_auction_crawler.store import AuctionStore


class FakeFrame:
    def __init__(self, url: str, texts: list[str]) -> None:
        self.url = url
        self._texts = texts
        self.calls = 0

    async def evaluate(self, _script: str) -> str:
        value = self._texts[min(self.calls, len(self._texts) - 1)]
        self.calls += 1
        return value


class FakePopup:
    def __init__(self, frames: list[FakeFrame]) -> None:
        self.frames = frames
        self.waits = 0

    async def wait_for_timeout(self, _ms: int) -> None:
        self.waits += 1


class StreamdocsTextTests(unittest.IsolatedAsyncioTestCase):
    """매각물건명세서 본문은 뷰어가 다 그린 뒤에야 읽힌다. 덜 그려진 상태를 본문으로
    착각하면 빈 문서를 collected로 저장하게 된다."""

    def _crawler(self):
        return CourtAuctionDetailCrawler.__new__(CourtAuctionDetailCrawler)

    async def test_returns_body_once_it_stops_changing(self):
        body = "의 정 부 지 방 법 원 매각물건명세서 " + "가" * 400
        frame = FakeFrame("https://pvo.scourt.go.kr/streamdocs/view/sd", [body, body, body])
        popup = FakePopup([frame])

        text = await self._crawler()._read_streamdocs_text(popup)

        self.assertTrue(text.startswith("의 정 부 지 방 법 원"))

    async def test_waits_through_partially_rendered_text(self):
        short = "/ 5\n1/5"
        body = "매각물건명세서 " + "나" * 400
        frame = FakeFrame("https://pvo.scourt.go.kr/streamdocs/view/sd", [short, short, body, body, body])
        popup = FakePopup([frame])

        text = await self._crawler()._read_streamdocs_text(popup)

        self.assertIn("매각물건명세서", text)
        self.assertGreaterEqual(len(text), 300)

    async def test_page_indicator_alone_is_not_treated_as_content(self):
        frame = FakeFrame("https://pvo.scourt.go.kr/streamdocs/view/sd", ["/ 5\n1/5"])
        popup = FakePopup([frame])

        text = await self._crawler()._read_streamdocs_text(popup)

        self.assertEqual(text, "")

    async def test_missing_viewer_frame_yields_nothing(self):
        popup = FakePopup([FakeFrame("https://ecfs.scourt.go.kr/sgvo/other.html", ["x" * 900])])

        text = await self._crawler()._read_streamdocs_text(popup)

        self.assertEqual(text, "")


class ImageSniffTests(unittest.TestCase):
    """법원 사이트는 JPEG를 image/png로 알려준다(표본 300건 중 286건). 알려준 값을
    그대로 믿으면 확장자와 Content-Type이 전부 어긋난 채 저장된다."""

    def test_jpeg_bytes_are_detected_regardless_of_declared_type(self):
        self.assertEqual(sniff_image_mime(b"\xff\xd8\xff\xe0" + b"0" * 20), "image/jpeg")

    def test_png_and_gif_are_detected(self):
        self.assertEqual(sniff_image_mime(b"\x89PNG\r\n\x1a\n" + b"0" * 20), "image/png")
        self.assertEqual(sniff_image_mime(b"GIF89a" + b"0" * 20), "image/gif")

    def test_webp_needs_both_riff_and_webp_markers(self):
        self.assertEqual(sniff_image_mime(b"RIFF" + b"1234" + b"WEBP"), "image/webp")
        self.assertEqual(sniff_image_mime(b"RIFF" + b"1234" + b"AVI "), "")

    def test_unknown_bytes_fall_back_to_the_declared_type(self):
        # 빈 문자열을 주면 호출부가 사이트가 알려준 값을 그대로 쓴다.
        self.assertEqual(sniff_image_mime(b"not an image"), "")
        self.assertEqual(sniff_image_mime(b""), "")


class DetailCrawlerHelperTests(unittest.TestCase):
    def test_find_table_value_reads_adjacent_cell(self):
        tables = [{"caption": "물건 기본정보", "rows": [["물건번호", "3", "용도", "아파트"]]}]

        self.assertEqual(find_table_value(tables, "물건번호"), "3")

    def test_find_document_title_uses_document_table(self):
        tables = [{"caption": "문서명 목록", "rows": [["번호", "문서명"], ["1", "감정평가서"]]}]

        self.assertEqual(find_document_title(tables), "감정평가서")

    def test_future_document_is_scheduled_for_release_window(self):
        sale_date = (date.today() + timedelta(days=30)).isoformat()

        self.assertTrue(document_next_retry(sale_date, 14))

    def test_safe_path_part_removes_path_separators(self):
        self.assertEqual(safe_path_part("서울/2026타경1:물건1"), "서울_2026타경1_물건1")

    def test_governor_trips_after_consecutive_distress_and_recovers(self):
        governor = HealthGovernor(trip_threshold=3, recovery_streak=2, base_cooldown_seconds=60)

        governor.record_distress()
        governor.record_distress()
        self.assertFalse(governor.degraded)

        governor.record_distress()
        self.assertTrue(governor.degraded)
        self.assertGreater(governor.cooldown_until, 0)
        self.assertEqual(governor.delay_multiplier(), 3.0)

        governor.record_healthy()
        self.assertTrue(governor.degraded)
        governor.record_healthy()
        self.assertFalse(governor.degraded)
        self.assertEqual(governor.delay_multiplier(), 1.0)

    def test_governor_success_resets_distress_count(self):
        governor = HealthGovernor(trip_threshold=3)

        governor.record_distress()
        governor.record_distress()
        governor.record_healthy()
        governor.record_distress()
        governor.record_distress()

        self.assertFalse(governor.degraded)

    def test_governor_cooldown_doubles_on_repeated_trips(self):
        governor = HealthGovernor(trip_threshold=1, base_cooldown_seconds=60, max_cooldown_seconds=900)
        import time as time_module

        governor.record_distress()
        first = governor.cooldown_until - time_module.monotonic()
        governor.record_distress()
        second = governor.cooldown_until - time_module.monotonic()

        self.assertAlmostEqual(first, 60, delta=2)
        self.assertAlmostEqual(second, 120, delta=2)

    def test_governor_detects_stall_only_after_attempts_and_time(self):
        import time as time_module

        governor = HealthGovernor(stall_limit_seconds=1800, min_attempts_for_stall=5)
        now = time_module.monotonic()

        # 시도가 없으면(유휴 대기) 아무리 오래돼도 정체가 아니다
        governor.last_healthy_at = now - 10_000
        self.assertFalse(governor.is_stalled(now))

        # 시도가 쌓였고 시간이 지나면 정체
        for _ in range(5):
            governor.record_distress()
        governor.last_healthy_at = now - 1801
        self.assertTrue(governor.is_stalled(now))

        # 정상 수집이 한 건이라도 나오면 리셋
        governor.record_healthy()
        self.assertFalse(governor.is_stalled(time_module.monotonic()))

    def test_governor_wait_turn_returns_immediately_on_abort(self):
        import asyncio

        governor = HealthGovernor()
        governor.degraded = True  # 평소라면 1번 워커는 여기서 무한 대기
        governor.abort_requested = True

        async def run():
            await asyncio.wait_for(governor.wait_turn(1), timeout=2)

        asyncio.run(run())  # 타임아웃 없이 즉시 반환되어야 한다

    def test_governor_detects_throughput_degradation(self):
        # 반오염: 완전 정체는 아니지만 성공률이 급락한 상태
        governor = HealthGovernor(
            throughput_window_seconds=100,
            min_throughput_attempts=8,
            min_throughput_success_ratio=0.25,
        )
        base = governor.window_start

        # 창이 차기 전에는 평가하지 않는다
        for _ in range(3):
            governor.record_distress()
        self.assertFalse(governor.is_throughput_degraded(base + 50))

        # 창이 찬 시점: 시도 10회 중 성공 1회(10%) < 25% -> 반오염
        governor.window_start = base
        governor.window_attempts = 10
        governor.window_success = 1
        self.assertTrue(governor.is_throughput_degraded(base + 101))

    def test_governor_throughput_ok_when_success_ratio_healthy(self):
        governor = HealthGovernor(
            throughput_window_seconds=100,
            min_throughput_attempts=8,
            min_throughput_success_ratio=0.25,
        )
        base = governor.window_start
        governor.window_attempts = 10
        governor.window_success = 9  # 90% 성공
        self.assertFalse(governor.is_throughput_degraded(base + 101))

    def test_governor_throughput_ignores_small_samples(self):
        # 시도 자체가 적으면(유휴 등) 반오염으로 판정하지 않는다
        governor = HealthGovernor(throughput_window_seconds=100, min_throughput_attempts=8)
        base = governor.window_start
        governor.window_attempts = 3
        governor.window_success = 0
        self.assertFalse(governor.is_throughput_degraded(base + 101))

    def test_benign_errors_are_not_blocking_signals(self):
        self.assertTrue(is_benign_case_error(LookupError("사건 검색 결과 없음")))
        self.assertTrue(is_benign_case_error(ValueError("사건번호 형식 오류")))
        self.assertFalse(is_benign_case_error(PlaywrightTimeoutError("Timeout 30000ms exceeded")))
        self.assertFalse(is_benign_case_error(RuntimeError("net::ERR_INTERNET_DISCONNECTED")))

    def test_singleton_lock_skips_when_another_collector_is_alive(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AuctionStore(Path(tmp) / "auction.sqlite3")
            # PID 1(launchd)은 항상 살아 있고 우리 프로세스가 아니다
            (Path(tmp) / "collect-details.pid").write_text("1\n", encoding="utf-8")

            summary = collect_details_sync(store, limit=5)

            self.assertEqual(summary.targets, 0)
            self.assertEqual(summary.collected, 0)
            # 락 파일은 남의 것이므로 지우지 않는다
            self.assertEqual(
                (Path(tmp) / "collect-details.pid").read_text(encoding="utf-8").strip(), "1"
            )


if __name__ == "__main__":
    unittest.main()


class FakeMessagePage:
    """processMsg iframe만 흉내 내는 최소 페이지."""

    def __init__(self, sources: list[str]) -> None:
        self._sources = sources

    async def evaluate(self, _script: str) -> list[str]:
        return [src for src in self._sources if "processMsg" in src]


class CaseSearchErrorTests(unittest.IsolatedAsyncioTestCase):
    # 실측: 없는 사건은 이 문구가 뜨고, 오류 화면은 '오류'로 끝난다.
    NO_CASE_TEXT = "검색조건\n해당 사건번호는 잘못된 번호입니다. 다시 한번 확인해 보시기 바랍니다.\n유의사항"
    ERROR_TEXT = "법원은 책임을 지지 않습니다.\nCOPYRIGHT\n맨 위로가기\n오류"
    # 사건 화면은 다 떴는데 물건상세조회 버튼만 없는 실제 화면(대구 2025타경8970).
    NO_ITEMS_TEXT = "검색조건\n법원 :대구지방법원\n사건기본내역\n사건번호\t2025타경8970전자\n유의사항"

    def test_never_collected_case_stays_benign(self):
        error = case_search_error(
            "강릉지원", "2025타경1", self.NO_CASE_TEXT, "", collected_before=False
        )
        self.assertIsInstance(error, LookupError)
        self.assertTrue(is_benign_case_error(error))

    def test_collected_case_called_missing_is_a_refusal(self):
        # 받아둔 적 있는 사건을 '없다'고 하면 거짓말이다(실측 59%가 같은 날 성공).
        error = case_search_error(
            "강릉지원", "2025타경1", self.NO_CASE_TEXT, "", collected_before=True
        )
        self.assertFalse(is_benign_case_error(error))
        self.assertIn("세션 거절", str(error))

    def test_rendered_case_without_items_does_not_wake_the_governor(self):
        # 사건 화면은 멀쩡한데 물건 버튼만 없는 것 — 전체 실패의 절반. 장애가
        # 아니므로 양성이어야 한다. 격상하면 거버너가 평상시에 계속 헛돈다.
        error = case_search_error(
            "강릉지원", "2025타경1", self.NO_ITEMS_TEXT, "", collected_before=True
        )
        self.assertTrue(is_benign_case_error(error))
        self.assertIn("물건 목록 없음", str(error))

    def test_blank_screen_is_infrastructure_failure(self):
        error = case_search_error(
            "강릉지원", "2025타경1", self.ERROR_TEXT, "조회중입니다.", collected_before=False
        )
        self.assertFalse(is_benign_case_error(error))
        self.assertIn("조회중입니다.", str(error))

    def test_blank_screen_without_readable_message_still_escalates(self):
        error = case_search_error(
            "강릉지원", "2025타경1", self.ERROR_TEXT, "", collected_before=False
        )
        self.assertFalse(is_benign_case_error(error))
        self.assertIn("메시지 못 읽음", str(error))

    async def test_site_message_decodes_euc_kr_param(self):
        page = FakeMessagePage(
            [
                "https://www.courtauction.go.kr/pgj/websquare/message/processMsg.html"
                "?param=%c1%b6%c8%b8%c1%df%c0%d4%b4%cf%b4%d9.&postfix=17888340751455083",
                "https://example.com/other.html",
            ]
        )
        self.assertEqual(await site_message(page), "조회중입니다.")

    async def test_site_message_is_empty_when_no_message_frame(self):
        self.assertEqual(await site_message(FakeMessagePage([])), "")


class GovernorFreshBrowserTests(unittest.TestCase):
    """차단 의심이 뜨면 냉각 사다리를 타지 말고 바로 브라우저를 새로 열어야 한다.

    실측(9/11~9/15, 자가 복구 167회): 냉각만 거친 뒤 성공률 48%, 브라우저를
    새로 열면 65%. 그런데 냉각이 60->120->240->480초로 올라가며 교체를 15분씩
    미뤘고 하루 4~17시간을 기다리는 데 썼다."""

    def test_trip_asks_for_a_fresh_browser(self):
        governor = HealthGovernor(trip_threshold=3)
        self.assertFalse(governor.wants_fresh_browser)
        for _ in range(3):
            governor.record_distress()
        self.assertTrue(governor.wants_fresh_browser)

    def test_distress_below_threshold_does_not_ask(self):
        governor = HealthGovernor(trip_threshold=3)
        for _ in range(2):
            governor.record_distress()
        self.assertFalse(governor.wants_fresh_browser)

    def test_success_before_threshold_keeps_the_browser(self):
        # 간간이 성공하면 연속 오류가 아니다 — 멀쩡한 세션을 버리면 안 된다.
        governor = HealthGovernor(trip_threshold=3)
        governor.record_distress()
        governor.record_distress()
        governor.record_healthy()
        governor.record_distress()
        governor.record_distress()
        self.assertFalse(governor.wants_fresh_browser)


class 문서본문판정Test(unittest.TestCase):
    """'수집됨'이 거짓말을 하던 자리 (G06).

    예전 판정은 iframe 에 `text`·`tables`·`resources` 가 하나라도 있으면 참이었다.
    감정평가서 뷰어는 Adobe 안내 이미지와 "열람이 안될 경우…" 문구를 **항상** 보내므로,
    본문을 한 글자도 못 받은 56,123건이 `collected` 로 적혔다.
    """

    껍데기 = {
        "iframe": {
            "resources": ["https://get.adobe.com/kr/reader/",
                          "https://ca.kapanet.or.kr/image/getacro.gif"],
            "tables": [{"caption": "", "rows": [["감정평가서", "(열람이 안될 경우 옆의 노란색 아이콘을 클릭하면 아크로벳을 다운받을수 있습니다.)"]]}],
            "text": "감정평가서\t(열람이 안될 경우 옆의 노란색 아이콘을 클릭하면 아크로벳을 다운받을수 있습니다.)",
            "url": "https://ca.kapanet.or.kr/view/000530/20240130000964/1/240126-19-0001/20240202",
        },
        "text": "",
    }

    def test_뷰어_껍데기는_내용이_아니다(self):
        from court_auction_crawler.detail_crawler import document_has_body
        self.assertFalse(document_has_body(self.껍데기))

    def test_PDF_주소를_아는_것은_가진_것이_아니다(self):
        """받아 올 실마리일 뿐이다. 주소는 metadata 에 남아 나중에 쓴다."""
        from court_auction_crawler.detail_crawler import document_has_body
        주소만 = {"iframe": dict(self.껍데기["iframe"])}
        주소만["iframe"]["resources"] = self.껍데기["iframe"]["resources"] + [
            "https://ca.kapanet.or.kr/825B2D1A/001/EF300039/000530-20240130000964-1-0000.pdf"]
        self.assertFalse(document_has_body(주소만))

    def test_내려받은_파일이_있으면_내용이다(self):
        from court_auction_crawler.detail_crawler import document_has_body
        self.assertTrue(document_has_body(self.껍데기, {"file_path": "data/docs/x.pdf"}))

    def test_본문이_길면_내용이다(self):
        from court_auction_crawler.detail_crawler import document_has_body
        self.assertTrue(document_has_body({"text": "가" * 250}))

    def test_짧은_본문은_내용이_아니다(self):
        from court_auction_crawler.detail_crawler import document_has_body
        self.assertFalse(document_has_body({"text": "감정평가서"}))

    def test_뷰어가_비면_바깥_머리표는_본문이_아니다(self):
        """실측: 감정평가서 바깥 글자는 202~324자(법원·사건번호·명령회차 머리표)라
        길이로 가르면 반드시 샌다. 현황조사서 최소 277자와 구간이 겹친다."""
        from court_auction_crawler.detail_crawler import document_has_body
        머리표만 = dict(self.껍데기)
        머리표만["text"] = ("감정평가서 법원,사건번호,명령회차,중복병합사건 "
                        "displayed in the table 법원 제주지방법원 사건번호 2024타경964 "
                        "명령회차 1 회 중복병합사건 -choose- " * 4)
        # 실측 최대(324자)를 넘겨 둔다 — 길이로는 못 가른다는 것이 요점이다
        self.assertGreater(len(머리표만["text"]), 324)
        self.assertFalse(document_has_body(머리표만))

    def test_뷰어_없이_본문만_있으면_내용이다(self):
        """현황조사서·매각물건명세서는 뷰어 없이 본문이 바로 온다. 막으면 안 된다."""
        from court_auction_crawler.detail_crawler import document_has_body
        self.assertTrue(document_has_body({"text": "현황조사 내용 " * 40}))
