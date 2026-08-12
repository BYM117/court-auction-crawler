import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from court_auction_crawler.common import index_problems
from court_auction_crawler.models import AuctionItem
from court_auction_crawler.store import (
    SCHEMA_VERSION,
    AuctionStore,
    build_item_key,
    infer_court_from_case,
    extract_common_fields,
    is_valid_auction_item,
    merge_detail_json,
    representative_case_no,
    strip_address_label,
)


class CourtInferenceTests(unittest.TestCase):
    """사이트가 법원명을 사건번호에 붙여서 주는 경우가 있다. 이걸 놓치면 item_key가
    'auction:-:...'가 되고, 상세 크롤러가 법원을 못 골라 그 물건은 상세 수집 대상에서
    통째로 빠진다."""

    def test_court_glued_to_case_number_is_recovered(self):
        self.assertEqual(infer_court_from_case("안양지원2025타경101127"), "안양지원")
        self.assertEqual(infer_court_from_case("수원지방법원2024타경8712"), "수원지방법원")

    def test_space_separated_form_still_works(self):
        self.assertEqual(infer_court_from_case("서울중앙지방법원 2025타경100"), "서울중앙지방법원")

    def test_merged_case_uses_the_leading_court(self):
        self.assertEqual(
            infer_court_from_case("안양지원2025타경101127(중복)2025타경2"), "안양지원"
        )

    def test_case_number_without_court_yields_nothing(self):
        self.assertEqual(infer_court_from_case("2025타경1234"), "")
        self.assertEqual(infer_court_from_case(""), "")

    def test_prefix_that_is_not_a_court_is_rejected(self):
        self.assertEqual(infer_court_from_case("경매물건2025타경1234"), "")

    def test_glued_court_produces_a_key_that_includes_the_court(self):
        # 법원이 키에 들어가야 다른 법원의 같은 사건번호와 섞이지 않는다.
        key = build_item_key({"사건번호": "안양지원2025타경101127", "물건번호": "1"})

        self.assertEqual(key, "auction:안양지원:2025타경101127:1")


class ItemKeyTests(unittest.TestCase):
    def test_build_item_key_uses_court_case_and_item_number(self):
        key = build_item_key({"사건번호": "서울중앙지방법원 2025타경1234", "물건번호": "1"})

        self.assertEqual(key, "auction:서울중앙지방법원:2025타경1234:1")

    def test_build_item_key_without_court_uses_placeholder(self):
        key = build_item_key({"사건번호": "2025타경1234", "물건번호": "1"})

        self.assertEqual(key, "auction:-:2025타경1234:1")

    def test_scheduled_and_current_share_the_same_key(self):
        current = build_item_key({"사건번호": "서울중앙지방법원 2024타경22026", "물건번호": "1", "수집구분": "진행"})
        scheduled = build_item_key({"사건번호": "서울중앙지방법원 2024타경22026", "물건번호": "1", "수집구분": "예정"})

        self.assertEqual(current, scheduled)

    def test_merged_case_uses_leading_case_number(self):
        self.assertEqual(
            representative_case_no("서울중앙지방법원 2020타경107804 2022타경106133 2024타경4880"),
            "2020타경107804",
        )
        key = build_item_key(
            {"사건번호": "서울중앙지방법원 2020타경107804 2022타경106133", "물건번호": "1"}
        )
        self.assertEqual(key, "auction:서울중앙지방법원:2020타경107804:1")

    def test_extract_common_fields_infers_court_from_case_number(self):
        fields = extract_common_fields({"사건번호": "서울중앙지방법원 2023타경114490", "물건번호": "2"})

        self.assertEqual(fields["court"], "서울중앙지방법원")

    def test_is_valid_auction_item_requires_case_and_item_number(self):
        self.assertFalse(is_valid_auction_item({"소재지": "서울"}))
        self.assertTrue(is_valid_auction_item({"사건번호": "서울중앙지방법원 2023타경114490", "물건번호": "2"}))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "auction.sqlite3"
        self.store = AuctionStore(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_upsert_tracks_insert_update_and_unchanged(self):
        first = [AuctionItem({"사건번호": "서울중앙지방법원 2025타경1234", "물건번호": "1", "최저매각가격": "100원"})]
        second = [AuctionItem({"사건번호": "서울중앙지방법원 2025타경1234", "물건번호": "1", "최저매각가격": "90원"})]

        inserted = self.store.upsert_items(first)
        unchanged = self.store.upsert_items(first)
        updated = self.store.upsert_items(second)
        item = self.store.get_item("auction:서울중앙지방법원:2025타경1234:1")

        self.assertEqual(inserted.inserted, 1)
        self.assertEqual(unchanged.unchanged, 1)
        self.assertEqual(updated.updated, 1)
        self.assertEqual(item["minimum_bid"], "90원")
        self.assertEqual(len(item["events"]), 2)

    def test_scheduled_sighting_renews_sale_date_in_same_row(self):
        current = AuctionItem(
            {
                "수집구분": "진행",
                "사건번호": "홍성지원 2024타경22026",
                "물건번호": "1",
                "매각기일": "2026.06.23",
                "진행상태": "유찰 2회",
                "최저매각가격": "49,000,000",
            }
        )
        renewed = AuctionItem(
            {
                "수집구분": "예정",
                "사건번호": "홍성지원 2024타경22026",
                "물건번호": "1",
                "매각기일": "2026.07.28",
                "진행상태": "유찰 3회",
                "최저매각가격": "34,300,000",
            }
        )

        self.store.upsert_items([current])
        result = self.store.upsert_items([renewed])
        items = self.store.list_items()["items"]

        self.assertEqual(result.updated, 1)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["sale_date"], "2026.07.28")
        self.assertEqual(items[0]["status"], "유찰 3회")

    def test_older_sighting_does_not_regress_sale_date(self):
        renewed = AuctionItem(
            {
                "수집구분": "진행",
                "사건번호": "홍성지원 2024타경22026",
                "물건번호": "1",
                "매각기일": "2026.07.28",
                "진행상태": "유찰 3회",
            }
        )
        stale = AuctionItem(
            {
                "수집구분": "진행",
                "사건번호": "홍성지원 2024타경22026",
                "물건번호": "1",
                "매각기일": "2026.06.23",
                "진행상태": "유찰 2회",
            }
        )

        self.store.upsert_items([renewed])
        result = self.store.upsert_items([stale])
        item = self.store.get_item("auction:홍성지원:2024타경22026:1")

        self.assertEqual(result.unchanged, 1)
        self.assertEqual(result.updated, 0)
        self.assertEqual(item["sale_date"], "2026.07.28")
        self.assertEqual(item["status"], "유찰 3회")

    def test_bulk_sale_parcels_merge_deterministically(self):
        def parcel(address):
            return AuctionItem(
                {
                    "수집구분": "진행",
                    "사건번호": "서울중앙지방법원 2020타경107804 2022타경106133",
                    "물건번호": "1",
                    "소재지": address,
                    "매각기일": "2026.08.10",
                    "진행상태": "유찰 5회",
                }
            )

        parcels = [
            parcel("서울특별시 종로구 평창동 425-23 [토지 임야 35㎡]"),
            parcel("서울특별시 종로구 평창동 425-2 [토지 임야 2193㎡]"),
            parcel("서울특별시 종로구 평창동 425-8 [토지 임야 568㎡]"),
        ]

        first = self.store.upsert_items(parcels)
        second = self.store.upsert_items(list(reversed(parcels)))
        item = self.store.get_item("auction:서울중앙지방법원:2020타경107804:1")

        self.assertEqual(first.inserted, 1)
        self.assertEqual(second.unchanged, 1)
        self.assertEqual(second.updated, 0)
        self.assertEqual(item["address"], "서울특별시 종로구 평창동 425-2 [토지 임야 2193㎡]")
        self.assertIn("425-23", item["detail"]["소재지목록"])
        self.assertIn("425-8", item["detail"]["소재지목록"])

    def test_source_flip_alone_does_not_create_update_event(self):
        base = {
            "사건번호": "부산지방법원 2025타경777",
            "물건번호": "1",
            "매각기일": "2026.08.01",
            "진행상태": "신건",
            "최저매각가격": "50,000,000",
        }

        self.store.upsert_items([AuctionItem({**base, "수집구분": "진행"})])
        result = self.store.upsert_items([AuctionItem({**base, "수집구분": "예정"})])

        self.assertEqual(result.unchanged, 1)
        self.assertEqual(result.updated, 0)

    def test_detail_targets_complete_and_retry_on_due_document(self):
        item = AuctionItem(
            {
                "사건번호": "서울중앙지방법원 2026타경333",
                "물건번호": "1",
                "매각기일": "2026.08.01",
            }
        )
        self.store.upsert_items([item])
        item_key = "auction:서울중앙지방법원:2026타경333:1"

        self.assertEqual([row["item_key"] for row in self.store.list_detail_targets()], [item_key])

        self.store.save_item_detail(item_key, {"tables": [{"caption": "물건 기본정보"}]})
        self.assertEqual(self.store.list_detail_targets(), [])

        self.store.save_document_status(
            item_key,
            "감정평가서",
            status="pending",
            next_retry_at="2000-01-01T00:00:00+00:00",
        )
        self.assertEqual([row["item_key"] for row in self.store.list_detail_targets()], [item_key])

    def test_detail_unavailable_stops_retry_until_item_changes(self):
        self.store.upsert_items(
            [
                AuctionItem(
                    {
                        "사건번호": "수원지방법원 2026타경666",
                        "물건번호": "1",
                        "매각기일": "2026.08.01",
                        "진행상태": "유찰 1회",
                    }
                )
            ]
        )
        item_key = "auction:수원지방법원:2026타경666:1"

        self.store.mark_detail_unavailable(item_key, "물건상세조회 버튼 비활성")

        # 재시도 큐에서 빠진다 (문서 due가 있어도 unavailable이면 제외)
        self.store.save_document_status(
            item_key, "감정평가서", status="pending", next_retry_at="2000-01-01T00:00:00+00:00"
        )
        self.assertEqual(self.store.list_detail_targets(), [])
        item = self.store.get_item(item_key)
        self.assertEqual(item["detail_status"], "unavailable")

        # 타임스탬프가 초 단위라 테스트에서는 시간 경과를 시뮬레이션한다
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE auction_items SET detail_checked_at = '2026-01-01T00:00:00+00:00', "
                "last_changed_at = '2026-01-01T00:00:00+00:00' WHERE item_key = ?",
                (item_key,),
            )
        self.assertEqual(self.store.list_detail_targets(), [])

        # 확인 이후 목록에서 변경이 잡히면(재공고) 다시 대상이 된다
        renewed = AuctionItem(
            {
                "사건번호": "수원지방법원 2026타경666",
                "물건번호": "1",
                "매각기일": "2026.09.15",
                "진행상태": "유찰 2회",
            }
        )
        self.store.upsert_items([renewed])
        self.assertEqual([row["item_key"] for row in self.store.list_detail_targets()], [item_key])

    def test_detail_failure_obeys_retry_time(self):
        self.store.upsert_items(
            [AuctionItem({"사건번호": "부산지방법원 2026타경444", "물건번호": "2"})]
        )
        item_key = "auction:부산지방법원:2026타경444:2"

        self.store.mark_detail_failure(item_key, "법원 응답 지연")

        self.assertEqual(self.store.list_detail_targets(), [])
        self.assertEqual(len(self.store.list_detail_targets(force=True)), 1)
        item = self.store.get_item(item_key)
        self.assertEqual(item["detail_status"], "failed")
        self.assertEqual(item["detail_fail_count"], 1)
        self.assertIn("법원 응답 지연", item["detail_error"])

    def test_detail_failure_keeps_collected_when_data_already_exists(self):
        self.store.upsert_items(
            [AuctionItem({"사건번호": "서울중앙지방법원 2026타경700", "물건번호": "1", "매각기일": "2026.08.01"})]
        )
        item_key = "auction:서울중앙지방법원:2026타경700:1"
        self.store.save_item_detail(item_key, {"sections": [{"title": "감정평가"}]})

        # 목록 갱신으로 재수집 대상이 됐다가 재수집이 실패한 상황
        self.store.mark_detail_failure(item_key, "재수집 중 타임아웃")

        item = self.store.get_item(item_key)
        # 상세 데이터가 이미 있으므로 collected 유지 (failed로 덮지 않음)
        self.assertEqual(item["detail_status"], "collected")
        self.assertEqual(item["detail_fail_count"], 1)
        # 백오프가 걸려 즉시 재수집 대상이 되지 않는다
        self.assertEqual(self.store.list_detail_targets(), [])

    def test_detail_documents_and_assets_are_returned(self):
        self.store.upsert_items(
            [AuctionItem({"사건번호": "대전지방법원 2026타경555", "물건번호": "1"})]
        )
        item_key = "auction:대전지방법원:2026타경555:1"
        self.store.save_item_detail(item_key, {"sections": [{"title": "감정평가요항표"}]})
        self.store.save_document_status(
            item_key,
            "감정평가서",
            status="collected",
            title="감정평가서",
            file_path="/tmp/report.pdf",
            content_type="application/pdf",
            file_size=120,
            sha256="doc-hash",
            metadata={"text": "감정평가서 본문"},
        )
        asset_id = self.store.save_asset(
            item_key,
            kind="photo",
            label="전경도_1",
            file_path="/tmp/photo.jpg",
            content_type="image/jpeg",
            file_size=80,
            sha256="image-hash",
        )

        item = self.store.get_item(item_key)

        self.assertEqual(item["detail"]["sections"][0]["title"], "감정평가요항표")
        self.assertEqual(item["documents"][0]["status"], "collected")
        self.assertEqual(item["documents"][0]["metadata"]["text"], "감정평가서 본문")
        self.assertEqual(item["assets"][0]["id"], asset_id)

    def test_lifecycle_deactivates_expired_and_upsert_revives(self):
        item = AuctionItem(
            {
                "수집구분": "진행",
                "사건번호": "부산지방법원 2025타경888",
                "물건번호": "1",
                "매각기일": "2026.06.01",
                "진행상태": "유찰 1회",
            }
        )
        self.store.upsert_items([item])

        result = self.store.apply_lifecycle(now="2026-07-06T00:00:00+00:00")
        stale = self.store.get_item("auction:부산지방법원:2025타경888:1")

        self.assertEqual(result["deactivated"], 1)
        self.assertFalse(stale["is_active"])
        self.assertIn("deactivated", [event["event_type"] for event in stale["events"]])

        renewed = AuctionItem(
            {
                "수집구분": "예정",
                "사건번호": "부산지방법원 2025타경888",
                "물건번호": "1",
                "매각기일": "2026.08.20",
                "진행상태": "유찰 2회",
            }
        )
        self.store.upsert_items([renewed])
        revived = self.store.get_item("auction:부산지방법원:2025타경888:1")

        self.assertTrue(revived["is_active"])
        self.assertEqual(revived["sale_date"], "2026.08.20")

    def test_list_missing_coordinates_respects_retry_policy(self):
        self.store.upsert_items(
            [
                AuctionItem({"사건번호": "부산지방법원 2025타경901", "물건번호": "1", "소재지": "부산 A", "매각기일": "2026.08.01"}),
                AuctionItem({"사건번호": "부산지방법원 2025타경902", "물건번호": "1", "소재지": "부산 B", "매각기일": "2026.08.01"}),
                AuctionItem({"사건번호": "부산지방법원 2025타경903", "물건번호": "1", "소재지": "사용본거지 : 서울", "매각기일": "2026.08.01"}),
            ]
        )
        self.store.mark_coordinate_missing("auction:부산지방법원:2025타경901:1")
        self.store.mark_coordinate_missing("auction:부산지방법원:2025타경903:1", quality="not_applicable")

        targets = {row["item_key"] for row in self.store.list_missing_coordinates()}
        self.assertEqual(targets, {"auction:부산지방법원:2025타경902:1"})

        with self.store.connect() as conn:
            conn.execute(
                "UPDATE auction_items SET geocoded_at = '2026-06-01T00:00:00+00:00' WHERE item_key = ?",
                ("auction:부산지방법원:2025타경901:1",),
            )
        targets = {row["item_key"] for row in self.store.list_missing_coordinates()}
        self.assertEqual(
            targets,
            {"auction:부산지방법원:2025타경901:1", "auction:부산지방법원:2025타경902:1"},
        )

    def test_lifecycle_keeps_upcoming_and_grace_period_items(self):
        upcoming = AuctionItem(
            {
                "사건번호": "부산지방법원 2025타경889",
                "물건번호": "1",
                "매각기일": "2026.07.20",
                "진행상태": "신건",
            }
        )
        just_passed = AuctionItem(
            {
                "사건번호": "부산지방법원 2025타경890",
                "물건번호": "1",
                "매각기일": "2026.07.04",
                "진행상태": "유찰 1회",
            }
        )
        self.store.upsert_items([upcoming, just_passed])

        result = self.store.apply_lifecycle(now="2026-07-06T00:00:00+00:00")

        self.assertEqual(result["deactivated"], 0)


class CoverageStatsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AuctionStore(Path(self.tmp.name) / "auction.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def test_record_court_stats_warns_on_drop_and_missing(self):
        first = self.store.record_court_stats(
            {
                ("current", "서울중앙지방법원"): 120,
                ("current", "부산지방법원"): 80,
                ("current", "제주지방법원"): 5,
            }
        )
        self.assertEqual(first, [])

        second = self.store.record_court_stats(
            {
                ("current", "서울중앙지방법원"): 10,  # 급감
                ("current", "제주지방법원"): 0,  # 기준 미달이라 무시
            }
        )

        self.assertEqual(len(second), 2)
        self.assertTrue(any("서울중앙지방법원" in w and "급감" in w for w in second))
        self.assertTrue(any("부산지방법원" in w and "누락" in w for w in second))
        self.assertFalse(any("제주지방법원" in w for w in second))


class MigrationTests(unittest.TestCase):
    def _insert_legacy_row(self, conn, item_key, values, *, last_seen, lat=None, lng=None):
        raw = json.dumps(values, ensure_ascii=False, sort_keys=True)
        conn.execute(
            """
            INSERT INTO auction_items(
                item_key, source, case_no, item_no, court, address, category,
                appraisal, minimum_bid, sale_date, status, detail_url,
                lat, lng, raw_json, detail_json, content_hash, list_hash,
                first_seen_at, last_seen_at, is_active, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, ?, ?, ?, 1, ?)
            """,
            (
                item_key,
                values.get("수집구분", "진행"),
                values.get("사건번호", ""),
                values.get("물건번호", ""),
                "",
                values.get("소재지", ""),
                "",
                "",
                values.get("최저매각가격", ""),
                values.get("매각기일", ""),
                values.get("진행상태", ""),
                "",
                lat,
                lng,
                raw,
                f"legacy-{item_key}",
                f"legacy-{item_key}",
                last_seen,
                last_seen,
                last_seen,
            ),
        )
        conn.execute(
            "INSERT INTO auction_events(item_key, event_type, new_json, created_at) VALUES(?, 'created', ?, ?)",
            (item_key, raw, last_seen),
        )

    def test_v2_migration_merges_scheduled_twin_and_rekeys_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "auction.sqlite3"
            AuctionStore(db_path)  # 새 스키마 생성

            conn = sqlite3.connect(db_path)
            current_values = {
                "수집구분": "진행",
                "사건번호": "홍성지원 2024타경22026",
                "물건번호": "1",
                "소재지": "충청남도 홍성군 홍성읍",
                "매각기일": "2026.06.23",
                "진행상태": "유찰 2회",
            }
            scheduled_values = {
                "수집구분": "예정",
                "사건번호": "홍성지원 2024타경22026",
                "물건번호": "1",
                "소재지": "충청남도 홍성군 홍성읍",
                "매각기일": "2026.07.28",
                "진행상태": "유찰 3회",
            }
            self._insert_legacy_row(
                conn,
                "auction:홍성지원 2024타경22026:1",
                current_values,
                last_seen="2026-06-20T00:00:00+00:00",
                lat=36.6,
                lng=126.66,
            )
            self._insert_legacy_row(
                conn,
                "auction:scheduled:홍성지원 2024타경22026:1",
                scheduled_values,
                last_seen="2026-07-01T00:00:00+00:00",
            )
            conn.execute("PRAGMA user_version = 0")
            conn.commit()
            conn.close()

            store = AuctionStore(db_path)  # 마이그레이션 트리거
            item = store.get_item("auction:홍성지원:2024타경22026:1")
            payload = store.list_items()

            self.assertEqual(payload["total"], 1)
            self.assertIsNotNone(item)
            self.assertEqual(item["sale_date"], "2026.07.28")
            self.assertEqual(item["status"], "유찰 3회")
            self.assertEqual(item["source"], "진행")
            self.assertEqual(item["lat"], 36.6)
            self.assertEqual(item["first_seen_at"], "2026-06-20T00:00:00+00:00")
            self.assertEqual(item["last_seen_at"], "2026-07-01T00:00:00+00:00")
            self.assertEqual(len(item["events"]), 2)

            conn = sqlite3.connect(db_path)
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            conn.close()
            self.assertEqual(version, SCHEMA_VERSION)


class AddressLabelTests(unittest.TestCase):
    """부동산이 아닌 물건은 목록 표 구조가 달라 주소 앞에 항목 라벨이 붙어 들어온다."""

    def test_leading_labels_are_stripped(self):
        self.assertEqual(strip_address_label("사용본거지 : 부산광역시 사하구"), "부산광역시 사하구")
        self.assertEqual(strip_address_label("선적항 : 동해면 검포항"), "동해면 검포항")
        self.assertEqual(strip_address_label("소재지 : 경상북도 경산시"), "경상북도 경산시")
        self.assertEqual(strip_address_label("어장의위치 : 완도군"), "완도군")

    def test_colon_inside_a_real_address_is_kept(self):
        # 통째로 자르면 멀쩡한 주소가 잘린다.
        kept = "제주특별자치도 제주시 애월읍 곽지리 378-10 (현장표시 : 곽지리 산1)"
        self.assertEqual(strip_address_label(kept), kept)

        kept2 = "충청북도 충주시 호암토성2로 1 [집합건물 건물의번호 : 제101동]"
        self.assertEqual(strip_address_label(kept2), kept2)

    def test_normal_address_is_untouched(self):
        self.assertEqual(
            strip_address_label("서울특별시 중구 세종대로 110"), "서울특별시 중구 세종대로 110"
        )

    def test_extracted_field_uses_the_stripped_form(self):
        fields = extract_common_fields(
            {"사건번호": "부산지방법원 2025타경1", "물건번호": "1", "소재지": "사용본거지 : 부산광역시 사하구"}
        )

        self.assertEqual(fields["address"], "부산광역시 사하구")


class DetailPreservationTests(unittest.TestCase):
    """목록 갱신이 상세 크롤링 결과를 덮으면, 물건이 바뀔 때마다 상세가 껍데기로
    되돌아가고 그 상태가 API와 웹으로 그대로 나간다."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AuctionStore(Path(self.tmp.name) / "auction.sqlite3")
        self.item_key = "auction:서울중앙지방법원:2026타경100:1"

    def tearDown(self):
        self.tmp.cleanup()

    def _sighting(self, minimum_bid: str) -> AuctionItem:
        return AuctionItem(
            {
                "사건번호": "서울중앙지방법원 2026타경100",
                "물건번호": "1",
                "소재지": "서울특별시 중구 세종대로 110",
                "최저매각가격": minimum_bid,
                "담당계": "경매1계",
            }
        )

    def test_list_update_keeps_crawled_detail(self):
        self.store.upsert_items([self._sighting("100,000,000원")])
        self.store.save_item_detail(
            self.item_key, {"sections": [{"title": "물건 상세"}], "tables": [{"caption": "기일내역"}]}
        )

        self.store.upsert_items([self._sighting("90,000,000원")])
        item = self.store.get_item(self.item_key)

        self.assertIn("sections", item["detail"])
        self.assertIn("tables", item["detail"])
        self.assertEqual(item["minimum_bid"], "90,000,000원")

    def test_list_fields_still_refresh_on_update(self):
        self.store.upsert_items([self._sighting("100,000,000원")])
        self.store.save_item_detail(self.item_key, {"sections": [{"title": "물건 상세"}]})

        changed = AuctionItem(
            {
                "사건번호": "서울중앙지방법원 2026타경100",
                "물건번호": "1",
                "소재지": "서울특별시 중구 세종대로 110",
                "최저매각가격": "80,000,000원",
                "담당계": "경매9계",
            }
        )
        self.store.upsert_items([changed])
        item = self.store.get_item(self.item_key)

        self.assertEqual(item["detail"]["담당계"], "경매9계")
        self.assertIn("sections", item["detail"])

    def test_merge_survives_broken_existing_json(self):
        self.assertEqual(json.loads(merge_detail_json("{깨진", {"담당계": "경매1계"})), {"담당계": "경매1계"})
        self.assertEqual(json.loads(merge_detail_json(None, {"담당계": "경매1계"})), {"담당계": "경매1계"})
        self.assertEqual(json.loads(merge_detail_json("[1,2]", {"담당계": "경매1계"})), {"담당계": "경매1계"})


class IntegrityCheckTests(unittest.TestCase):
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
                        "소재지": "서울특별시 중구",
                        "최저매각가격": "100,000,000원",
                    }
                )
            ]
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_healthy_db_reports_no_problems(self):
        self.assertEqual(self.store.integrity_check(), [])

    def test_repair_indexes_rebuilds_without_losing_rows(self):
        self.store.repair_indexes()

        self.assertEqual(self.store.integrity_check(), [])
        self.assertIsNotNone(self.store.get_item("auction:서울중앙지방법원:2026타경100:1"))

    def test_repair_indexes_targets_named_index_and_skips_unknown(self):
        # integrity_check 출력에서 온 이름이라 실재하지 않을 수 있다. 조용히 건너뛴다.
        self.store.repair_indexes(["idx_auction_items_detail_due", "존재하지_않는_인덱스"])

        self.assertEqual(self.store.integrity_check(), [])


class IndexProblemClassificationTests(unittest.TestCase):
    def test_index_only_problems_yield_index_names(self):
        problems = [
            "row 27839 missing from index idx_auction_items_detail_due",
            "row 43363 missing from index idx_auction_items_detail_due",
            "wrong # of entries in index idx_auction_items_source",
        ]

        self.assertEqual(
            index_problems(problems),
            ["idx_auction_items_detail_due", "idx_auction_items_source"],
        )

    def test_table_level_damage_is_never_auto_repairable(self):
        # 페이지·테이블 손상이 한 줄이라도 섞이면 REINDEX로 고칠 수 없다.
        problems = [
            "row 27839 missing from index idx_auction_items_detail_due",
            "Page 4213 is never used",
        ]

        self.assertIsNone(index_problems(problems))

    def test_empty_problem_list_is_index_only_with_nothing_to_do(self):
        self.assertEqual(index_problems([]), [])


if __name__ == "__main__":
    unittest.main()
