import json
import os
import tempfile
import time
import unicodedata
import unittest
from pathlib import Path

from court_auction_crawler.common import path_is_within
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
from court_auction_crawler.enrichment import (
    parse_case_item,
    parse_case_type,
    public_auction_summary,
)


def case_detail(item_no: str, status_flow: str, note: str = "", head_money: str = "") -> dict:
    """사건 화면 스냅샷 흉내. 같은 표가 세 리스트에 중복해 들어오는 것까지 재현한다."""
    table = {
        "caption": "물건내역",
        "rows": [
            ["물건번호", item_no, "물건용도", "상가",
             "감정평가액 (최저매각가격) (매수신청보증금)", head_money],
            ["물건상태", status_flow],
            ["물건비고", note],
        ],
    }
    return {"case": {"case_tables": [table], "schedule_tables": [table], "filing_and_service_tables": []}}


class CaseItemParseTests(unittest.TestCase):
    """사건 화면 '물건내역'은 여태 받아만 놓고 안 읽었다(G04). 재매각 여부와
    보증금이 여기 있는데, 재매각이면 보증금이 최저가의 20%라 모르고 가면
    입찰이 무효가 된다."""

    def test_resale_is_read_from_the_item_status(self):
        detail = case_detail("1", "매각준비 -> 매각공고 -> 매각 -> 매각허가결정 -> 대금미납")
        info = parse_case_item(detail, "1")
        self.assertEqual(info["resale_reason"], "대금미납")

    def test_normal_payment_is_not_a_resale(self):
        # 법원 오타 '대급납부'. '대금'으로 찾으면 정상 납부를 재매각으로 오인한다.
        detail = case_detail("1", "매각준비 -> 매각공고 -> 매각 -> 매각허가결정 -> 대급납부")
        self.assertEqual(parse_case_item(detail, "1")["resale_reason"], "")

    def test_sibling_item_status_is_not_borrowed(self):
        # 사건 화면에는 형제 물건이 같이 들어 있다. 남의 이력을 붙이면 안 된다.
        detail = case_detail("2", "매각준비 -> 매각공고 -> 매각 -> 매각허가결정 -> 대금미납")
        self.assertEqual(parse_case_item(detail, "1")["resale_reason"], "")
        self.assertEqual(parse_case_item(detail, "2")["resale_reason"], "대금미납")

    def test_deposit_rate_comes_from_the_head_row(self):
        detail = case_detail("1", "매각준비", head_money="304,000,000원 (8,557,000원) (1,711,400원)")
        info = parse_case_item(detail, "1")
        self.assertEqual(info["deposit_amount"], 1711400)
        self.assertEqual(info["deposit_rate"], 0.2)

    def test_zero_minimum_bid_does_not_divide(self):
        detail = case_detail("1", "매각준비", head_money="304,000,000원 (0원) (0원)")
        self.assertIsNone(parse_case_item(detail, "1")["deposit_rate"])

    def test_missing_case_tables_are_harmless(self):
        self.assertEqual(parse_case_item(None, "1")["status_flow"], "")
        self.assertEqual(parse_case_item({}, "1")["resale_reason"], "")


class ShareSaleFlagTests(unittest.TestCase):
    """지분매각은 property.share와 screening에서 잡고 있었지만, 웹이 딱지로 쓰는
    special_rights 배열에는 없어서 화면에 안 보였다(G02)."""

    def test_share_sale_reaches_special_rights(self):
        item = {
            "item_key": "auction:서울중앙지방법원:2025타경1:1",
            "address": "서울특별시 중구 세종대로 110 [토지 대 100㎡ 갑구 2번 김철수 지분 4분의 1 전부]",
            "status": "유찰 1회",
        }
        summary = public_auction_summary(item)
        self.assertTrue(summary["property"]["share"]["is_share_sale"])
        self.assertIn("지분매각", summary["auction"]["special_rights"])

    def test_plain_item_gets_no_share_flag(self):
        item = {
            "item_key": "auction:서울중앙지방법원:2025타경2:1",
            "address": "서울특별시 중구 세종대로 110 [집합건물 철근콘크리트 59.87㎡]",
            "status": "신건",
        }
        self.assertNotIn("지분매각", public_auction_summary(item)["auction"]["special_rights"])


class CaseTypeTests(unittest.TestCase):
    """임의(담보권 실행)와 강제(집행권원)는 권리 분석이 갈리는 구분인데 안 실었다(G07)."""

    @staticmethod
    def _detail(case_name: str) -> dict:
        table = {"caption": "사건 기본 내역 검색결과",
                 "rows": [["사건번호", "2024타경178", "사건명", case_name]]}
        return {"case": {"case_tables": [table]}}

    def test_reads_the_case_name(self):
        self.assertEqual(parse_case_type(self._detail("부동산강제경매")), "부동산강제경매")
        self.assertEqual(parse_case_type(self._detail("부동산임의경매")), "부동산임의경매")

    def test_missing_table_is_harmless(self):
        self.assertEqual(parse_case_type(None), "")
        self.assertEqual(parse_case_type({"case": {}}), "")


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
        # 검색 그룹 라벨('이 셋 중 하나')은 용도가 아니다. 부분 문자열로 훑으면
        # 활성 4천 건이 통째로 '오피스텔'이 된다.
        self.assertEqual(
            public_auction_summary({"category": "상가,오피스텔,근린시설", "address": "서울특별시 중구 세종대로 110"})[
                "property"
            ]["type_guess"],
            "상가,오피스텔,근린시설",
        )
        self.assertEqual(payload["property"]["registry_search_hint"]["realty_type_guess"], "집합건물")
        self.assertEqual(payload["property"]["registry_search_hint"]["dong"], "101동")
        self.assertEqual(payload["property"]["registry_search_hint"]["ho"], "201호")
        self.assertIn("권리확인 필요", payload["screening"]["flags"])
        self.assertIn("raw", payload)
        self.assertIn("events", payload)
        self.assertEqual(payload["detail_collection"]["status"], "collected")
        self.assertEqual(payload["documents"][0]["document_type"], "매각물건명세서")
        self.assertEqual(payload["assets"][0]["url"], "/api/v1/assets/1")

    def test_asset_path_containment_survives_unicode_form_mismatch(self):
        # 맥을 옮기면 DB에 적힌 한글 경로(NFC)와 실행 시 계산한 루트(NFD)의 형태가
        # 갈린다. 형태만 다를 뿐 같은 폴더이므로 사진·문서 서빙이 막히면 안 된다.
        root = "/Users/bym/Documents/경매물건 크롤링/data/auction-assets"
        stored = unicodedata.normalize(
            "NFC", root + "/auction_서울중앙지방법원_2026타경100_1/photos/01.png"
        )

        self.assertTrue(path_is_within(stored, unicodedata.normalize("NFD", root)))
        self.assertTrue(path_is_within(stored, unicodedata.normalize("NFC", root)))
        self.assertFalse(path_is_within("/Users/bym/Documents/기타/photo.png", root))

    def test_collector_control_only_toggles_enabled_file(self):
        # 서버 컨트롤러는 수집을 직접 실행하지 않고 enabled 파일만 토글한다.
        # 실제 수집은 독립 collect-loop 프로세스가 담당한다.
        runner = CollectorControlRunner(self.store)
        self.assertFalse(runner.enabled)

        started = runner.start()
        self.assertTrue(started)
        self.assertTrue(runner.enabled)
        self.assertTrue(runner.enabled_path.exists())
        # 서버 스레드로 수집을 띄우지 않으므로 실행 스레드 속성이 없다
        self.assertFalse(hasattr(runner, "_thread"))

        # 이미 켜져 있으면 재요청은 False(중복 생성 아님)
        self.assertFalse(runner.start())

        stopped = runner.stop()
        self.assertTrue(stopped)
        self.assertFalse(runner.enabled)
        self.assertFalse(runner.enabled_path.exists())

    def test_collector_windows_start_today_without_past_range(self):
        import time

        runner = CollectorControlRunner(self.store)
        today = time.strftime("%Y-%m-%d")

        quick = runner.collection_window("quick")
        full = runner.collection_window("full")

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
