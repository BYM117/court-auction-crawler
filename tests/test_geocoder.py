import unittest

from court_auction_crawler.geocoder import (
    _candidate_queries,
    _extract_building_hint,
    apply_swapped_eup_myeon,
    _lot_main_only,
    _district_only,
    _pnu_from_structure,
    _same_region,
    is_mappable_property,
    normalize_sido,
)


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

    def test_same_region_normalizes_sido_aliases(self):
        renamed = {"address": {"parcel": "강원특별자치도 홍천군 화촌면 야시대리 694-2", "road": ""}}
        abbreviated = {"address": {"parcel": "서울특별시 도봉구 방학동 720", "road": ""}}
        other_region = {"address": {"parcel": "경상북도 경주시 천군동 266", "road": ""}}

        self.assertTrue(_same_region("강원도 홍천군 화촌면 야시대리 694-2", renamed))
        self.assertTrue(_same_region("서울 도봉구 방학로2길 27", abbreviated))
        self.assertFalse(_same_region("강원도 홍천군 화촌면 야시대리 694-2", other_region))

    def test_normalize_sido_maps_old_and_short_names(self):
        self.assertEqual(normalize_sido("강원도"), "강원")
        self.assertEqual(normalize_sido("강원특별자치도"), "강원")
        self.assertEqual(normalize_sido("전라북도"), "전북")
        self.assertEqual(normalize_sido("전북특별자치도"), "전북")
        self.assertEqual(normalize_sido("서울"), "서울")
        self.assertEqual(normalize_sido("서울특별시"), "서울")

    def test_candidate_queries_support_numbered_ga_dong(self):
        candidates = _candidate_queries("서울특별시 종로구 명륜3가 50-5 2층203호 [집합건물 29.98㎡]")

        self.assertIn("서울특별시 종로구 명륜3가 50-5", candidates)

    def test_pnu_from_structure_builds_19_digits(self):
        self.assertEqual(
            _pnu_from_structure({"level4LC": "5117034023", "level5": "694-2"}),
            "5117034023" + "1" + "0694" + "0002",
        )
        self.assertEqual(
            _pnu_from_structure({"level4LC": "4713025329", "level5": "산 266"}),
            "4713025329" + "2" + "0266" + "0000",
        )
        self.assertEqual(_pnu_from_structure({"level4LC": "", "level5": "1-2"}), "")
        self.assertEqual(_pnu_from_structure({"level4LC": "5117034023", "level5": ""}), "")

    def test_swap_eup_myeon_covers_promoted_names(self):
        # 법원은 승격 전 이름을, 지도는 승격 후 이름을 쓴다. 둘 다 던져야 지번이 잡힌다.
        self.assertEqual(
            apply_swapped_eup_myeon("충청북도 음성군 대소면 성본리 577-2"),
            "충청북도 음성군 대소읍 성본리 577-2",
        )
        self.assertEqual(
            apply_swapped_eup_myeon("전라남도 영암군 삼호읍 용당리 789"),
            "전라남도 영암군 삼호면 용당리 789",
        )
        # 읍면이 없는 주소는 건드리지 않는다
        self.assertEqual(apply_swapped_eup_myeon("서울특별시 도봉구 방학동 1-2"), "")
        # 도로명·리 이름은 바꾸지 않는다
        self.assertEqual(apply_swapped_eup_myeon("충청북도 청주시 서원구 산남동 1"), "")

    def test_candidate_queries_include_eup_myeon_swap(self):
        queries = _candidate_queries("충청북도 음성군 대소면 성본리 577-2 [토지 전 1809㎡]")
        # 상한(GEOCODER_MAX_QUERIES=4)에 잘리면 안 된다
        self.assertIn("충청북도 음성군 대소읍 성본리 577-2", queries[:4])

    def test_lot_main_only_drops_sub_number(self):
        # 새로 갈라진 필지는 지도에 없지만 본번은 있다
        self.assertEqual(
            _lot_main_only("경상북도 김천시 삼락동 891-171 [토지 전 1㎡]"),
            "경상북도 김천시 삼락동 891",
        )
        # 부번이 없으면 넓힐 것이 없다
        self.assertEqual(_lot_main_only("경상북도 김천시 삼락동 891"), "")

    def test_district_only_keeps_up_to_ri_or_dong(self):
        # 어업권은 지번이 아예 없다. 리 단위가 이 물건이 가질 수 있는 최선이다.
        self.assertEqual(
            _district_only("전라남도 완도군 고금면 상정리 상정 지선 [어업권 어류등양식]"),
            "전라남도 완도군 고금면 상정리",
        )
        self.assertEqual(
            _district_only("경상북도 김천시 삼락동 891-171"), "경상북도 김천시 삼락동"
        )
        # 리·동이 없으면 넓히지 않는다 — 시군구 단위 핀은 거짓말이다
        self.assertEqual(_district_only("서울특별시 도봉구 방학로2길 27"), "")

    def test_extract_building_hint(self):
        self.assertEqual(
            _extract_building_hint("인천광역시 중구 영종대로 196-26 1층105호 (운서동,응답하라1976)"),
            "응답하라1976",
        )
        self.assertEqual(
            _extract_building_hint(
                "인천광역시 서구 경서동 경서3구역도시개발지구25블록2로트 청라로데오시티포레안 16층1612호"
            ),
            "청라로데오시티포레안",
        )
        # 괄호가 법정동명뿐이면 건물명이 아니다
        self.assertEqual(_extract_building_hint("서울 도봉구 방학로2길 27 (방학동)"), "")
        # 괄호가 읍·면·리여도 마찬가지다
        self.assertEqual(_extract_building_hint("경기도 김포시 대곶면 대곶로 123 (석정리)"), "")
        # G12: 건물명이 없는 순수 토지에서 시도명이 새어나가면 같은 읍면의 모든 토지가
        # 한 점에 뭉친다. 빈 문자열이어야 폴백 자체를 포기한다.
        self.assertEqual(
            _extract_building_hint("전라남도 여수시 삼산면 덕촌리 1069 [토지 전 3498㎡]"), ""
        )
        self.assertEqual(
            _extract_building_hint("충청북도 음성군 대소면 삼호리 790 [토지 답 1058㎡]"), ""
        )

    def test_is_mappable_property_excludes_vehicles_and_ships(self):
        self.assertFalse(is_mappable_property("사용본거지 : 서울 도봉구 방학로2길 27", ""))
        self.assertFalse(is_mappable_property("서울 강남구 역삼동 123", "승용자동차"))
        self.assertFalse(is_mappable_property("경기 화성시 우정읍", "건설기계(덤프트럭)"))
        self.assertFalse(is_mappable_property("선적항 : 전라남도 완도군 완도읍 . [선박동력선 진솔호]", ""))
        self.assertFalse(is_mappable_property("소재지 : 전라남도 완도군 보길면 . [기타 동력선 제2화양호]", "기타"))
        self.assertTrue(is_mappable_property("서울특별시 중구 세종대로 110", "아파트"))


if __name__ == "__main__":
    unittest.main()
