import tempfile
import unittest
from pathlib import Path
from unittest import mock

from court_auction_crawler.building_registry import BuildingRegistry
from court_auction_crawler.cli import (
    build_snapshot_payload,
    run_enrich_buildings,
    run_geocode_missing,
)
from court_auction_crawler.geocoder import GeocodeResult
from court_auction_crawler.models import AuctionItem
from court_auction_crawler.store import AuctionStore


class GeocodeBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AuctionStore(Path(self.tmp.name) / "auction.sqlite3")
        self.store.upsert_items(
            [
                AuctionItem(
                    {
                        "사건번호": "서울중앙지방법원 2026타경1",
                        "물건번호": "1",
                        "소재지": "서울특별시 중구 세종대로 110",
                        "용도": "아파트",
                        "매각기일": "2026.08.01",
                    }
                ),
                AuctionItem(
                    {
                        "사건번호": "서울중앙지방법원 2026타경2",
                        "물건번호": "1",
                        "소재지": "경기도 성남시 분당구",
                        "용도": "토지",
                        "매각기일": "2026.08.01",
                    }
                ),
                AuctionItem(
                    {
                        "사건번호": "서울중앙지방법원 2026타경3",
                        "물건번호": "1",
                        "소재지": "사용본거지 : 서울 도봉구 방학로2길 27",
                        "용도": "승용자동차",
                        "매각기일": "2026.08.01",
                    }
                ),
            ]
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_key_touches_nothing(self):
        with mock.patch("court_auction_crawler.cli.env_value", return_value=""):
            result = run_geocode_missing(self.store, quiet=True)

        self.assertTrue(result["no_key"])
        self.assertEqual(len(self.store.list_missing_coordinates()), 3)

    def test_batch_updates_marks_and_excludes(self):
        def fake_geocode(address):
            if "세종대로" in address:
                return GeocodeResult(
                    lat=37.5647,
                    lng=126.9770,
                    pnu="1114010300101230000",
                    normalized_address="서울특별시 중구 세종대로 110",
                    query="서울특별시 중구 세종대로 110",
                )
            return None

        with mock.patch("court_auction_crawler.cli.env_value", return_value="dummy"), mock.patch(
            "court_auction_crawler.cli.geocode_address", side_effect=fake_geocode
        ):
            result = run_geocode_missing(self.store, quiet=True)

        self.assertEqual(result["targets"], 3)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["missing"], 1)
        self.assertEqual(result["excluded"], 1)

        located = self.store.get_item("auction:서울중앙지방법원:2026타경1:1")
        failed = self.store.get_item("auction:서울중앙지방법원:2026타경2:1")
        vehicle = self.store.get_item("auction:서울중앙지방법원:2026타경3:1")
        self.assertEqual(located["coordinate_quality"], "verified")
        self.assertEqual(located["pnu"], "1114010300101230000")
        self.assertEqual(failed["coordinate_quality"], "missing")
        self.assertEqual(vehicle["coordinate_quality"], "not_applicable")

        # 성공은 좌표 보유, 실패는 재시도 유예, 차량은 영구 제외 → 재실행 대상 없음
        with mock.patch("court_auction_crawler.cli.env_value", return_value="dummy"), mock.patch(
            "court_auction_crawler.cli.geocode_address", side_effect=fake_geocode
        ):
            rerun = run_geocode_missing(self.store, quiet=True)
        self.assertEqual(rerun["targets"], 0)


class SnapshotExportTests(unittest.TestCase):
    def test_snapshot_contains_only_mappable_items_with_coordinates(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AuctionStore(Path(tmp) / "auction.sqlite3")
            store.upsert_items(
                [
                    AuctionItem(
                        {
                            "사건번호": "서울중앙지방법원 2026타경10",
                            "물건번호": "1",
                            "소재지": "서울특별시 중구 세종대로 110 [집합건물 59.87㎡]",
                            "용도": "아파트",
                            "최저매각가격": "100,000,000 (50%)",
                            "감정평가액": "200,000,000",
                            "매각기일": "2026.08.10",
                            "진행상태": "유찰 1회",
                        }
                    ),
                    AuctionItem(
                        {
                            "사건번호": "서울중앙지방법원 2026타경11",
                            "물건번호": "1",
                            "소재지": "서울특별시 종로구 평창동 425-2",
                            "용도": "임야",
                            "매각기일": "2026.08.10",
                        }
                    ),
                    AuctionItem(
                        {
                            "사건번호": "서울중앙지방법원 2026타경12",
                            "물건번호": "1",
                            "소재지": "사용본거지 : 서울 도봉구 방학로2길 27 [K7 승용차]",
                            "용도": "승용자동차",
                            "매각기일": "2026.08.10",
                        }
                    ),
                ]
            )
            store.update_coordinates("auction:서울중앙지방법원:2026타경10:1", lat=37.5647, lng=126.977, pnu="1" * 19)
            # 차량은 필터 도입 전에 좌표가 저장됐더라도 스냅샷에서 빠져야 한다
            store.update_coordinates("auction:서울중앙지방법원:2026타경12:1", lat=37.6, lng=127.0)

            payload = build_snapshot_payload(store)

            self.assertEqual(payload["total"], 1)
            item = payload["items"][0]
            self.assertEqual(item["id"], "auction:서울중앙지방법원:2026타경10:1")
            self.assertEqual(item["lat"], 37.5647)
            self.assertEqual(item["price"]["minimum_bid"], 100000000)
            self.assertEqual(item["case"]["case_no"], "2026타경10")
            self.assertIn("screening", item)
            self.assertIn("map", item)


if __name__ == "__main__":
    unittest.main()


class BuildingBackfillTests(unittest.TestCase):
    """건축물대장은 필지 단위다. 물건마다 물으면 같은 답을 몇십 번 받아온다."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AuctionStore(Path(self.tmp.name) / "auction.sqlite3")
        # 일괄매각 아파트 한 동: 한 필지에 물건 세 개.
        self.store.upsert_items(
            [
                AuctionItem(
                    {
                        "사건번호": "서울중앙지방법원 2026타경1",
                        "물건번호": str(no),
                        "소재지": f"서울특별시 중구 세종대로 110 10{no}호",
                        "용도": "아파트",
                        "매각기일": "2026.08.01",
                    }
                )
                for no in (1, 2, 3)
            ]
        )
        for no in (1, 2, 3):
            self.store.update_coordinates(
                f"auction:서울중앙지방법원:2026타경1:{no}", lat=37.5, lng=127.0, pnu="1114010300100010000"
            )

    def tearDown(self):
        self.tmp.cleanup()

    def test_one_lookup_fills_every_item_on_the_same_lot(self):
        registry = BuildingRegistry(bld_nm="세종빌딩", tot_area=1000.0, main_purpose="업무시설")
        with mock.patch("court_auction_crawler.cli.env_value", return_value="dummy"), mock.patch(
            "court_auction_crawler.cli.fetch_building_registry", return_value=registry
        ) as fetch:
            result = run_enrich_buildings(self.store, limit=10, quiet=True)

        self.assertEqual(fetch.call_count, 1)   # 세 건이 아니라 한 번만 물어본다
        self.assertEqual(result["queries"], 1)
        self.assertEqual(result["ok"], 3)       # 그런데 세 건 전부 채워진다
        rows = self.store.list_missing_enrichment("building", limit=10)
        self.assertEqual(rows, [])

    def test_land_miss_is_not_asked_again_for_a_long_time(self):
        # 밭에 건물이 생길 일은 없다. 30일마다 되묻는 대신 오래 쉰다.
        with self.store.connect() as conn:
            conn.execute("UPDATE auction_items SET category = '전답' WHERE item_no = '1'")
            conn.execute(
                "UPDATE auction_items SET building_status='miss', building_at=datetime('now','-40 day')"
            )

        asked = {row["item_key"] for row in self.store.list_missing_enrichment("building", limit=10)}

        self.assertNotIn("auction:서울중앙지방법원:2026타경1:1", asked)  # 전답
        self.assertIn("auction:서울중앙지방법원:2026타경1:2", asked)     # 아파트

    def test_inactive_item_is_not_asked_over_and_over(self):
        # 답을 필지에만 적으면 비활성 물건은 영원히 미채움으로 남아 매 실행 다시 묻는다.
        with self.store.connect() as conn:
            conn.execute("UPDATE auction_items SET is_active = 0")
        with mock.patch("court_auction_crawler.cli.env_value", return_value="dummy"), mock.patch(
            "court_auction_crawler.cli.fetch_building_registry", return_value=None
        ):
            run_enrich_buildings(self.store, limit=10, include_inactive=True, quiet=True)

        rows = self.store.list_missing_enrichment("building", limit=10, active=None)

        self.assertEqual(rows, [])
