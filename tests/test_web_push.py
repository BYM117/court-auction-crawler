import gzip
import json
import tempfile
import unittest
from pathlib import Path

from court_auction_crawler.models import AuctionItem
from court_auction_crawler.store import AuctionStore
from court_auction_crawler.web_push import (
    LocalDirUploader,
    SNAPSHOT_KEY,
    asset_object_key,
    build_uploader,
    item_object_key,
    payload_digest,
    push_once,
)


class ObjectKeyTests(unittest.TestCase):
    def test_item_key_is_hashed_so_korean_and_colons_never_reach_storage(self):
        key = item_object_key("auction:서울중앙지방법원:2026타경100:1")

        self.assertTrue(key.startswith("v1/items/"))
        self.assertTrue(key.endswith(".json"))
        self.assertNotIn(":", key.removeprefix("v1/items/"))
        self.assertTrue(key.removeprefix("v1/items/").removesuffix(".json").isalnum())

    def test_same_item_always_maps_to_the_same_object(self):
        self.assertEqual(
            item_object_key("auction:부산지방법원:2025타경1:1"),
            item_object_key("auction:부산지방법원:2025타경1:1"),
        )

    def test_asset_key_uses_content_hash_so_identical_photos_upload_once(self):
        key = asset_object_key({"sha256": "abc123", "file_path": "/tmp/사진/01-x.PNG"})

        self.assertEqual(key, "v1/assets/abc123.png")

    def test_payload_digest_ignores_key_order(self):
        self.assertEqual(
            payload_digest({"a": 1, "b": [1, 2]}),
            payload_digest({"b": [1, 2], "a": 1}),
        )


class UploaderSelectionTests(unittest.TestCase):
    def test_local_scheme_writes_to_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            uploader = build_uploader(f"local://{tmp}")
            uploader.put("v1/x/y.json", b"hello", "application/json")

            self.assertEqual((Path(tmp) / "v1/x/y.json").read_bytes(), b"hello")

    def test_unknown_scheme_is_rejected(self):
        with self.assertRaises(ValueError):
            build_uploader("ftp://nope")


class PushPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = AuctionStore(self.root / "auction.sqlite3")
        self.store.upsert_items(
            [
                AuctionItem(
                    {
                        "수집구분": "진행",
                        "사건번호": "서울중앙지방법원 2026타경100",
                        "물건번호": "1",
                        "소재지": "서울특별시 중구 세종대로 110",
                        "용도": "아파트",
                        "감정평가액": "200,000,000원",
                        "최저매각가격": "100,000,000원",
                        "매각기일": "2026.09.10",
                        "진행상태": "유찰 1회",
                    }
                )
            ]
        )
        self.item_key = "auction:서울중앙지방법원:2026타경100:1"
        self.dest = self.root / "push"
        self.uploader = LocalDirUploader(self.dest)

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_push_uploads_snapshot_and_item(self):
        summary = push_once(self.store, self.uploader, skip_assets=True)

        self.assertTrue(summary.snapshot_pushed)
        self.assertEqual(summary.items_pushed, 1)
        self.assertTrue((self.dest / item_object_key(self.item_key)).is_file())

    def test_second_push_uploads_nothing_when_data_is_unchanged(self):
        push_once(self.store, self.uploader, skip_assets=True)
        summary = push_once(self.store, self.uploader, skip_assets=True)

        # 같은 초에 다시 돌면 후보로는 잡힐 수 있다(시각이 초 단위라). 보증해야 할 것은
        # 그 경우에도 재업로드가 일어나지 않는다는 것.
        self.assertEqual(summary.items_pushed, 0)

    def test_detail_change_triggers_a_new_push(self):
        push_once(self.store, self.uploader, skip_assets=True)
        # content_hash는 목록 필드만 덮으므로, 상세만 채워도 다시 올라가야 한다.
        self.store.save_item_detail(self.item_key, {"sections": [{"title": "물건 상세"}]})

        summary = push_once(self.store, self.uploader, skip_assets=True)

        self.assertEqual(summary.items_pushed, 1)

    def test_touched_but_identical_item_is_recorded_without_reuploading(self):
        push_once(self.store, self.uploader, skip_assets=True)
        target = self.dest / item_object_key(self.item_key)
        target.unlink()
        # 내용은 그대로인데 시각만 밀린 상황을 만든다.
        with self.store.connect() as conn:
            conn.execute("UPDATE web_sync SET pushed_at = '2000-01-01T00:00:00+00:00' WHERE kind='item'")

        summary = push_once(self.store, self.uploader, skip_assets=True)

        self.assertEqual(summary.items_skipped, 1)
        self.assertEqual(summary.items_pushed, 0)
        self.assertFalse(target.exists())

    def test_snapshot_is_gzip_and_holds_mapped_items(self):
        self.store.update_coordinates(self.item_key, lat=37.5, lng=127.0, pnu="1114010300")

        push_once(self.store, self.uploader, skip_assets=True)
        payload = json.loads(gzip.decompress((self.dest / SNAPSHOT_KEY).read_bytes()).decode("utf-8"))

        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["items"][0]["id"], self.item_key)

    def test_photo_is_uploaded_once_and_skipped_afterwards(self):
        photo = self.root / "photo.png"
        photo.write_bytes(b"\x89PNG fake")
        self.store.save_asset(
            self.item_key,
            kind="photo",
            label="전경도_1",
            file_path=str(photo),
            content_type="image/png",
            file_size=photo.stat().st_size,
            sha256="deadbeef",
        )

        first = push_once(self.store, self.uploader)
        second = push_once(self.store, self.uploader)

        self.assertEqual(first.assets_pushed, 1)
        self.assertEqual(second.assets_pushed, 0)
        self.assertEqual((self.dest / "v1/assets/deadbeef.png").read_bytes(), b"\x89PNG fake")

    def test_missing_photo_file_does_not_fail_the_run(self):
        self.store.save_asset(
            self.item_key,
            kind="photo",
            label="전경도_1",
            file_path=str(self.root / "없는파일.png"),
            content_type="image/png",
            file_size=10,
            sha256="nofile",
        )

        summary = push_once(self.store, self.uploader)

        self.assertEqual(summary.errors, [])
        self.assertEqual(summary.assets_pushed, 0)

    def test_limit_zero_means_everything_not_nothing(self):
        # LIMIT 0으로 나가면 전량 업로드가 조용히 0건으로 끝난다(실측).
        self.assertEqual(len(self.store.pending_item_pushes(limit=0)), 1)
        self.assertEqual(len(self.store.pending_item_pushes(limit=-1)), 1)
        self.assertEqual(len(self.store.pending_item_pushes(limit=1)), 1)

    def test_asset_limit_zero_means_everything(self):
        photo = self.root / "photo.png"
        photo.write_bytes(b"\x89PNG fake")
        self.store.save_asset(
            self.item_key,
            kind="photo",
            label="전경도_1",
            file_path=str(photo),
            content_type="image/png",
            file_size=photo.stat().st_size,
            sha256="deadbeef",
        )

        self.assertEqual(len(self.store.pending_asset_pushes(limit=0)), 1)

    def test_dry_run_writes_nothing(self):
        summary = push_once(self.store, self.uploader, skip_assets=True, dry_run=True)

        self.assertEqual(summary.items_pushed, 1)
        self.assertFalse(self.dest.exists())


if __name__ == "__main__":
    unittest.main()
