import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from court_auction_crawler.models import AuctionItem
from court_auction_crawler.store import AuctionStore
from court_auction_crawler.web import (
    AuctionWebHandler,
    CollectorControlRunner,
    DbHealthWatchdog,
    collect_log_status,
    control_api_key_valid,
    openapi_schema,
    public_auction_detail,
    safe_external_url,
)


class WebApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AuctionStore(Path(self.tmp.name) / "auction.sqlite3")
        self.store.upsert_items(
            [
                AuctionItem(
                    {
                        "수집구분": "진행",
                        "사건번호": "서울중앙지방법원 2026타경100",
                        "물건번호": "1",
                        "소재지": "서울특별시 중구 세종대로 110 101동 201호 [집합건물 철근콘크리트구조 59.87㎡]",
                        "용도": "아파트",
                        "감정평가액": "200,000,000원",
                        "최저매각가격": "100,000,000원",
                        "매각기일": "2026.07.10",
                        "진행상태": "유찰 2회",
                    }
                ),
                AuctionItem(
                    {
                        "수집구분": "예정",
                        "사건번호": "부산지방법원 2026타경200",
                        "물건번호": "2",
                        "소재지": "부산광역시 해운대구",
                        "용도": "오피스텔",
                        "최저매각가격": "80,000,000원",
                        "매각기일": "2026.08.01",
                        "진행상태": "취하",
                    }
                ),
            ]
        )
        self.handler = object.__new__(AuctionWebHandler)
        self.handler.store = self.store
        self.handler.runner = None
        self.handler.collector_runner = None

    def tearDown(self):
        self.tmp.cleanup()

    def test_health_and_openapi_are_available(self):
        schema = openapi_schema()

        self.assertEqual(schema["openapi"], "3.0.3")
        self.assertIn("/api/v1/auctions", schema["paths"])
        self.assertIn("/api/v1/assets/{id}", schema["paths"])
        self.assertIn("/api/v1/documents/{id}", schema["paths"])

    def test_list_auctions_filters_and_paginates(self):
        payload = self.handler._public_auction_list_payload(
            {
                "q": ["서울"],
                "region": ["서울"],
                "active": ["true"],
                "sale_date_from": ["2026-07-01"],
                "sale_date_to": ["2026-07-31"],
                "limit": ["1"],
            },
        )

        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["count"], 1)
        self.assertFalse(payload["has_more"])
        self.assertEqual(payload["items"][0]["case_no"], "서울중앙지방법원 2026타경100")
        self.assertEqual(payload["items"][0]["price"]["minimum_bid"], 100000000)
        self.assertEqual(payload["items"][0]["price"]["minimum_bid_percent"], 50)
        self.assertEqual(payload["items"][0]["auction"]["fail_count"], 2)
        self.assertEqual(payload["items"][0]["property"]["address"]["sido"], "서울특별시")
        self.assertEqual(payload["items"][0]["property"]["address"]["sigungu"], "중구")
        self.assertEqual(payload["items"][0]["property"]["address"]["eup_myeon_dong"], "")
        self.assertEqual(payload["items"][0]["property"]["address"]["lot_number"], "")
        self.assertEqual(payload["items"][0]["property"]["address"]["dong"], "101동")
        self.assertEqual(payload["items"][0]["property"]["address"]["ho"], "201호")
        self.assertEqual(payload["items"][0]["property"]["area"]["building_sqm"], 59.87)

    def test_detail_endpoint_returns_public_shape(self):
        item_key = "auction:서울중앙지방법원:2026타경100:1"
        self.store.save_item_detail(item_key, {"sections": [{"title": "물건 상세"}]})
        self.store.save_document_status(item_key, "매각물건명세서", status="metadata_only")
        self.store.save_asset(
            item_key,
            kind="photo",
            label="전경도_1",
            file_path=str(Path(self.tmp.name) / "auction-assets" / "photo.jpg"),
            content_type="image/jpeg",
            file_size=100,
            sha256="image-hash",
        )
        item = self.store.get_item(item_key)
        payload = public_auction_detail(item)

        self.assertEqual(payload["id"], "auction:서울중앙지방법원:2026타경100:1")
        self.assertEqual(payload["address"], "서울특별시 중구 세종대로 110 101동 201호 [집합건물 철근콘크리트구조 59.87㎡]")
        self.assertEqual(payload["case"]["case_no"], "2026타경100")
        self.assertEqual(payload["property"]["type_guess"], "아파트")
        self.assertEqual(payload["property"]["registry_search_hint"]["realty_type_guess"], "집합건물")
        self.assertEqual(payload["property"]["registry_search_hint"]["dong"], "101동")
        self.assertEqual(payload["property"]["registry_search_hint"]["ho"], "201호")
        self.assertIn("권리확인 필요", payload["screening"]["flags"])
        self.assertIn("raw", payload)
        self.assertIn("events", payload)
        self.assertEqual(payload["detail_collection"]["status"], "collected")
        self.assertEqual(payload["documents"][0]["document_type"], "매각물건명세서")
        self.assertEqual(payload["assets"][0]["url"], "/api/v1/assets/1")

    def test_collector_uses_separate_current_and_scheduled_windows(self):
        runner = CollectorControlRunner(self.store)
        command = runner._collect_command(
            {
                "current_start": "2025-07-02",
                "current_end": "2027-07-02",
                "scheduled_start": "2026-07-02",
                "scheduled_end": "2026-12-29",
            }
        )

        self.assertNotIn("--start-date", command)
        self.assertNotIn("--end-date", command)
        self.assertEqual(command[command.index("--current-start-date") + 1], "2025-07-02")
        self.assertEqual(command[command.index("--current-end-date") + 1], "2027-07-02")
        self.assertEqual(command[command.index("--scheduled-start-date") + 1], "2026-07-02")
        self.assertEqual(command[command.index("--scheduled-end-date") + 1], "2026-12-29")
        self.assertEqual(command[command.index("--date-chunk-days") + 1], "31")
        # 상세 수집은 전용 프로세스가 담당한다. 러너 사이클에 섞이면
        # 목록 신선도가 무너지고 이중 실행 충돌이 난다.
        self.assertNotIn("--details", command)
        self.assertNotIn("--detail-limit", command)

    def test_collector_windows_start_today_without_past_range(self):
        import time

        runner = CollectorControlRunner(self.store)
        today = time.strftime("%Y-%m-%d")

        quick = runner._collection_window("quick")
        full = runner._collection_window("full")

        self.assertEqual(quick["current_start"], today)
        self.assertEqual(full["current_start"], today)
        self.assertEqual(quick["scheduled_start"], today)

    def test_collect_log_status_treats_waiting_cycle_as_idle(self):
        log_path = Path(self.tmp.name) / "collect-all.log"
        err_path = Path(self.tmp.name) / "collect-all.err.log"
        pid_path = Path(self.tmp.name) / "collect-all.pid"
        log_path.write_text(
            "\n".join(
                [
                    "[3960/3960] 진행 제주지방법원 2027-06-30~2027-07-02 수집 중",
                    "34개 물건 수집 완료: 신규 0개, 변경 0개, 동일 98개",
                    "===== 자동 수집 종료 2026-07-02 13:16:45 exit=0; 10800초 후 재시작 =====",
                    "===== 다음 자동 수집까지 10800초 대기 =====",
                ]
            ),
            encoding="utf-8",
        )

        payload = collect_log_status(log_path=log_path, err_path=err_path, pid_path=pid_path)

        self.assertEqual(payload["state"], "idle")
        self.assertEqual(payload["state_label"], "다음 수집 대기")
        self.assertEqual(payload["current"], "3시간 주기 대기 중")
        self.assertEqual(payload["last_result"], "===== 다음 자동 수집까지 10800초 대기 =====")
        self.assertEqual(payload["progress_percent"], 100)

    def test_healthcheck_watchdog_aborts_after_consecutive_failures(self):
        class BrokenStore:
            def healthcheck(self):
                raise OperationalError("unable to open database file")

        from sqlite3 import OperationalError

        aborted = []
        watchdog = DbHealthWatchdog(
            BrokenStore(), fail_limit=3, on_unhealthy=lambda: aborted.append(True)
        )

        self.assertFalse(watchdog.check_once())
        self.assertFalse(watchdog.should_abort())
        self.assertFalse(watchdog.check_once())
        self.assertFalse(watchdog.check_once())
        self.assertTrue(watchdog.should_abort())

    def test_healthcheck_watchdog_resets_on_recovery(self):
        class FlakyStore:
            def __init__(self):
                self.healthy = False

            def healthcheck(self):
                if not self.healthy:
                    raise RuntimeError("temporary")

        store = FlakyStore()
        watchdog = DbHealthWatchdog(store, fail_limit=3)

        watchdog.check_once()
        watchdog.check_once()
        self.assertEqual(watchdog.consecutive_failures, 2)

        store.healthy = True
        self.assertTrue(watchdog.check_once())
        self.assertEqual(watchdog.consecutive_failures, 0)
        self.assertFalse(watchdog.should_abort())

    def test_healthcheck_passes_on_real_store(self):
        watchdog = DbHealthWatchdog(self.store)
        self.assertTrue(watchdog.check_once())

    def test_collect_log_status_prefers_live_process_over_stale_log_age(self):
        log_path = Path(self.tmp.name) / "collect-all.log"
        err_path = Path(self.tmp.name) / "collect-all.err.log"
        pid_path = Path(self.tmp.name) / "collect-all.pid"
        log_path.write_text(
            "[7/3960] 예정 서울중앙지방법원 2026-09-28~2026-10-11 수집 중\n",
            encoding="utf-8",
        )
        old_time = time.time() - 600
        os.utime(log_path, (old_time, old_time))
        pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")

        payload = collect_log_status(log_path=log_path, err_path=err_path, pid_path=pid_path)

        self.assertEqual(payload["state"], "running")
        self.assertEqual(payload["state_label"], "수집 중")
        self.assertTrue(payload["process_running"])
        self.assertGreaterEqual(payload["seconds_since_log"], 590)

    def test_control_api_key_accepts_header_or_bearer(self):
        self.assertTrue(control_api_key_valid("", "", ""))
        self.assertTrue(control_api_key_valid("secret", "secret", ""))
        self.assertTrue(control_api_key_valid("secret", "", "Bearer secret"))
        self.assertFalse(control_api_key_valid("secret", "wrong", ""))

    def test_public_urls_only_allow_http_and_https(self):
        self.assertEqual(safe_external_url("https://example.test/a"), "https://example.test/a")
        self.assertEqual(safe_external_url("http://example.test/a"), "http://example.test/a")
        self.assertEqual(safe_external_url("javascript:alert(1)"), "")
        self.assertEqual(safe_external_url("/relative/path"), "")


if __name__ == "__main__":
    unittest.main()
