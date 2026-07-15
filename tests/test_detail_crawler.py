import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from court_auction_crawler.detail_crawler import (
    HealthGovernor,
    collect_details_sync,
    document_next_retry,
    find_document_title,
    find_table_value,
    is_benign_case_error,
    safe_path_part,
)
from court_auction_crawler.store import AuctionStore


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
