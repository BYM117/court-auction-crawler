import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from court_auction_crawler.models import AuctionItem
from court_auction_crawler.store import (
    SCHEMA_VERSION,
    AuctionStore,
    build_item_key,
    extract_common_fields,
    is_valid_auction_item,
    representative_case_no,
)


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


if __name__ == "__main__":
    unittest.main()
