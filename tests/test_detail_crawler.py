import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from court_auction_crawler.detail_crawler import (
    CourtAuctionDetailCrawler,
    HealthGovernor,
    collect_details_sync,
    document_next_retry,
    find_document_title,
    find_table_value,
    is_benign_case_error,
    safe_path_part,
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
