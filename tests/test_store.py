import tempfile
import unittest
from pathlib import Path

from court_auction_crawler.models import AuctionItem
from court_auction_crawler.store import AuctionStore, build_item_key, extract_common_fields, is_valid_auction_item


class StoreTests(unittest.TestCase):
    def test_build_item_key_uses_case_and_item_number(self):
        key = build_item_key({"사건번호": "2025타경1234", "물건번호": "1"})

        self.assertEqual(key, "auction:2025타경1234:1")

    def test_upsert_tracks_insert_update_and_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AuctionStore(Path(tmp) / "auction.sqlite3")
            first = [AuctionItem({"사건번호": "2025타경1234", "물건번호": "1", "최저매각가격": "100원"})]
            second = [AuctionItem({"사건번호": "2025타경1234", "물건번호": "1", "최저매각가격": "90원"})]

            inserted = store.upsert_items(first)
            unchanged = store.upsert_items(first)
            updated = store.upsert_items(second)
            item = store.get_item("auction:2025타경1234:1")

            self.assertEqual(inserted.inserted, 1)
            self.assertEqual(unchanged.unchanged, 1)
            self.assertEqual(updated.updated, 1)
            self.assertEqual(item["minimum_bid"], "90원")
            self.assertEqual(len(item["events"]), 2)

    def test_extract_common_fields_infers_court_from_case_number(self):
        fields = extract_common_fields({"사건번호": "서울중앙지방법원 2023타경114490", "물건번호": "2"})

        self.assertEqual(fields["court"], "서울중앙지방법원")

    def test_is_valid_auction_item_requires_case_and_item_number(self):
        self.assertFalse(is_valid_auction_item({"소재지": "서울"}))
        self.assertTrue(is_valid_auction_item({"사건번호": "서울중앙지방법원 2023타경114490", "물건번호": "2"}))


if __name__ == "__main__":
    unittest.main()
