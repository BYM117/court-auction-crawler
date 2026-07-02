import unittest

from court_auction_crawler.geocoder import _candidate_queries, _same_region


class GeocoderTests(unittest.TestCase):
    def test_candidate_queries_keep_specific_road_address(self):
        candidates = _candidate_queries(
            "서울특별시 관악구 남부순환로234길 57-10 2층202호 "
            "(봉천동,샤롯캐슬) [집합건물 철근콘크리트구조 29.28㎡]"
        )

        self.assertIn("서울특별시 관악구 남부순환로234길 57-10", candidates)
        self.assertNotIn("서울특별시 관악구 남부순환로234길", candidates)

    def test_candidate_queries_keep_specific_lot_address(self):
        candidates = _candidate_queries("경상북도 경주시 천군동 산266 [토지 임야 32992㎡]")

        self.assertEqual(candidates, ["경상북도 경주시 천군동 산266"])

    def test_candidate_queries_keep_numbered_dong_lot_address(self):
        candidates = _candidate_queries("부산광역시 서구 동대신동3가 446-4 [토지 대 66㎡]")

        self.assertEqual(candidates, ["부산광역시 서구 동대신동3가 446-4"])

    def test_candidate_queries_drop_administrative_area_only(self):
        self.assertEqual(_candidate_queries("경기도 성남시 분당구"), [])

    def test_same_region_accepts_road_result_when_parcel_is_short(self):
        item = {
            "address": {
                "parcel": "봉천동 1622-23",
                "road": "서울특별시 관악구 남부순환로234길 57-10 (봉천동,샤롯캐슬)",
            }
        }

        self.assertTrue(_same_region("서울특별시 관악구 남부순환로234길 57-10 2층202호", item))


if __name__ == "__main__":
    unittest.main()
