import unittest
from urllib.error import HTTPError

from court_auction_crawler import building_registry as bld_mod
from court_auction_crawler.building_registry import (
    _num,
    _pick_recap,
    _pick_title,
    _request_bld,
    pnu_parts,
)
from court_auction_crawler.common import RateLimitError, is_rate_limited
from court_auction_crawler.land_use import pick_primary_zone
from court_auction_crawler.official_price import _pick_apart_unit
from court_auction_crawler import transactions as tx_mod
from court_auction_crawler.transactions import (
    _building_name,
    _deal_date,
    _name_matches,
    _recent_months,
    _request_cached,
    _summarize,
    classify_transaction_kind,
)


class BuildingRegistryTests(unittest.TestCase):
    def test_pnu_parts_splits_19_digits(self):
        # 시군구5 / 법정동5 / (산구분1) / 번4 / 지4
        self.assertEqual(
            pnu_parts("2635010800109470000"),
            ("26350", "10800", "0947", "0000"),
        )

    def test_pnu_parts_rejects_bad_input(self):
        self.assertIsNone(pnu_parts(""))
        self.assertIsNone(pnu_parts("123"))
        self.assertIsNone(pnu_parts("263501080010947000X"))

    def test_pick_recap_prefers_row_with_plat_area(self):
        rows = [{"platArea": "0"}, {"platArea": "858.7", "bldNm": "본동"}]
        self.assertEqual(_pick_recap(rows)["bldNm"], "본동")

    def test_pick_title_picks_largest_floor_area_main_building(self):
        rows = [
            {"totArea": "120", "etcPurps": "지하주차장"},
            {"totArea": "5000", "bldNm": "101동"},
            {"totArea": "80", "regstrKindCdNm": "부속"},
        ]
        self.assertEqual(_pick_title(rows)["bldNm"], "101동")

    def test_num_handles_commas_and_junk(self):
        self.assertEqual(_num("21,851.4"), 21851.4)
        self.assertEqual(_num(None), 0.0)
        self.assertEqual(_num("없음"), 0.0)


class SharedHelperTests(unittest.TestCase):
    def test_api_modules_share_one_ssl_context(self):
        """정부 API 모듈이 ssl_context 사본을 다시 만들면 이 테스트가 깨진다.

        전에는 다섯 곳에 복붙돼 있었고 land_use 판본에만 GEOCODER_INSECURE_SSL
        탈출구가 빠져 있었다. 스위치를 켜도 토지이용계획만 조용히 계속 실패한다."""
        from court_auction_crawler import (
            building_registry,
            geocoder,
            land_use,
            official_price,
            transactions,
        )

        for module in (building_registry, land_use, official_price, transactions):
            self.assertIs(module.ssl_context, geocoder.ssl_context, module.__name__)

    def test_case_no_re_has_one_definition(self):
        """store 와 detail_crawler 가 사건번호 규칙을 각자 갖고 있으면 깨진다.

        따로 두면 한쪽만 고쳐 버그가 반만 낫는다(실제로 그랬다)."""
        from court_auction_crawler import common, detail_crawler, store

        self.assertIs(store.CASE_NO_RE, common.CASE_NO_RE)
        self.assertIs(detail_crawler.CASE_NO_RE, common.CASE_NO_RE)


class RateLimitTests(unittest.TestCase):
    def test_is_rate_limited_detects_known_signatures(self):
        self.assertTrue(is_rate_limited("...LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR..."))
        self.assertTrue(is_rate_limited("<returnReasonCode>22</returnReasonCode>"))
        self.assertTrue(is_rate_limited('{"resultCode":"22"}'))
        self.assertFalse(is_rate_limited("NORMAL SERVICE"))

    def test_request_bld_raises_on_http_429(self):
        # 일일 한도 초과(HTTP 429)를 miss로 삼키지 않고 RateLimitError로 올린다.
        def boom(*a, **k):
            raise HTTPError("u", 429, "Too Many Requests", {}, None)

        original = bld_mod.urlopen
        bld_mod.urlopen = boom
        try:
            with self.assertRaises(RateLimitError):
                _request_bld("key", "getBrTitleInfo", "11680", "10800", "0947", "0000")
        finally:
            bld_mod.urlopen = original

    def test_request_bld_other_http_error_returns_empty(self):
        def boom(*a, **k):
            raise HTTPError("u", 500, "Server Error", {}, None)

        original = bld_mod.urlopen
        bld_mod.urlopen = boom
        try:
            self.assertEqual(
                _request_bld("key", "getBrTitleInfo", "11680", "10800", "0947", "0000"), []
            )
        finally:
            bld_mod.urlopen = original


class TransactionClassifyTests(unittest.TestCase):
    def test_classify_by_category_and_address(self):
        self.assertEqual(classify_transaction_kind("아파트", ""), "apart")
        self.assertEqual(classify_transaction_kind("", "... 오피스텔 ..."), "officetel")
        self.assertEqual(classify_transaction_kind("다세대주택", ""), "villa")
        self.assertEqual(classify_transaction_kind("대지", "토지 임야"), "land")
        self.assertEqual(classify_transaction_kind("기타", ""), "")

    def test_building_name_extracts_complex_from_parentheses(self):
        addr = "서울특별시 관악구 남부순환로234길 57-10 (봉천동,샤롯캐슬) [집합건물 29.28㎡]"
        self.assertEqual(_building_name(addr), "샤롯캐슬")

    def test_building_name_skips_bare_dong(self):
        # 괄호 안이 '봉천동' 하나뿐이면 단지명이 아니므로 비운다.
        self.assertEqual(_building_name("서울 관악구 (봉천동)"), "")

    def test_name_matches_normalizes_and_substring(self):
        row = {"aptNm": "해운대 송정 우림필유"}
        self.assertTrue(_name_matches(row, ("aptNm",), "해운대송정우림필유아파트"))
        self.assertFalse(_name_matches(row, ("aptNm",), "다른단지"))


class TransactionSummaryTests(unittest.TestCase):
    def test_summarize_computes_stats_and_recent_sorted(self):
        rows = [
            {"aptNm": "가", "excluUseAr": "84.9", "dealAmount": "40,500",
             "floor": "17", "dealYear": "2026", "dealMonth": "6", "dealDay": "22"},
            {"aptNm": "가", "excluUseAr": "59.9", "dealAmount": "28,000",
             "floor": "3", "dealYear": "2026", "dealMonth": "7", "dealDay": "25"},
            {"aptNm": "가", "excluUseAr": "59.9", "dealAmount": "",  # 금액 없는 행은 제외
             "dealYear": "2026", "dealMonth": "5", "dealDay": "1"},
        ]
        summary = _summarize((rows, True), "sales", max_recent=12)
        self.assertTrue(summary["matched"])
        self.assertEqual(summary["count"], 2)
        self.assertEqual(summary["min"], 28000)
        self.assertEqual(summary["max"], 40500)
        self.assertEqual(summary["avg"], 34250)
        # 최근 계약일 먼저
        self.assertEqual(summary["recent"][0]["date"], "2026-07-25")

    def test_summarize_rent_uses_deposit(self):
        rows = [{"deposit": "2,000", "monthlyRent": "50", "dealYear": "2026", "dealMonth": "6", "dealDay": "1"}]
        summary = _summarize((rows, False), "rent", max_recent=12)
        self.assertFalse(summary["matched"])
        self.assertEqual(summary["min"], 2000)
        self.assertEqual(summary["recent"][0]["monthly"], 50)

    def test_deal_date_formats_parts(self):
        self.assertEqual(_deal_date({"dealYear": "2026", "dealMonth": "6", "dealDay": "3"}), "2026-06-03")
        self.assertEqual(_deal_date({"dealYear": "2026", "dealMonth": "6"}), "2026-06")
        self.assertEqual(_deal_date({}), "")

    def test_recent_months_wraps_year_boundary(self):
        months = _recent_months(3)
        self.assertEqual(len(months), 3)
        # 모두 YYYYMM 6자리
        self.assertTrue(all(len(m) == 6 and m.isdigit() for m in months))


class TransactionCacheTests(unittest.TestCase):
    def test_request_cached_reuses_same_key(self):
        # 같은 (op, 법정동, 월)은 한 번만 실제 호출하고 이후 캐시를 쓴다.
        calls = []
        original = tx_mod._request_rtms
        tx_mod._request_rtms = lambda key, op, lawd, ymd: calls.append((op, lawd, ymd)) or [{"x": ymd}]
        try:
            cache: dict = {}
            a = _request_cached("k", "OP", "11680", "202607", cache)
            b = _request_cached("k", "OP", "11680", "202607", cache)
            c = _request_cached("k", "OP", "11680", "202606", cache)
        finally:
            tx_mod._request_rtms = original
        self.assertEqual(a, b)              # 동일 결과 재사용
        self.assertEqual(len(calls), 2)     # 202607 한 번 + 202606 한 번
        self.assertEqual(c, [{"x": "202606"}])

    def test_request_cached_without_cache_calls_every_time(self):
        calls = []
        original = tx_mod._request_rtms
        tx_mod._request_rtms = lambda key, op, lawd, ymd: calls.append(1) or []
        try:
            _request_cached("k", "OP", "11680", "202607", None)
            _request_cached("k", "OP", "11680", "202607", None)
        finally:
            tx_mod._request_rtms = original
        self.assertEqual(len(calls), 2)


class LandUseZoneTests(unittest.TestCase):
    """용도지역 대표값 고르기.

    틀려도 에러가 안 난다. 그냥 쓸모없는 값이 화면에 뜬다."""

    def test_specific_zone_wins_over_broad_category(self):
        # '도시지역'은 틀린 말은 아니지만 너무 뭉뚱그려서 아무 정보가 안 된다.
        self.assertEqual(
            pick_primary_zone(["도시지역", "제2종일반주거지역", "지구단위계획구역"]),
            "제2종일반주거지역",
        )

    def test_specific_zone_wins_even_when_listed_last(self):
        # 순서에 기대면 안 된다. 사이트가 순서를 바꿔 줘도 같은 답이 나와야 한다.
        self.assertEqual(
            pick_primary_zone(["도시지역", "지구단위계획구역", "일반상업지역"]),
            "일반상업지역",
        )

    def test_falls_back_to_first_when_nothing_specific(self):
        # 구체적인 게 하나도 없으면 빈칸보다 뭐라도 보여주는 편이 낫다.
        self.assertEqual(pick_primary_zone(["도시지역", "지구단위계획구역"]), "도시지역")

    def test_empty_list_gives_empty_string(self):
        self.assertEqual(pick_primary_zone([]), "")


class OfficialPriceUnitTests(unittest.TestCase):
    """공시가격 붙일 호수 고르기.

    잘못 고르면 옆집 공시가격이 이 집 것으로 조용히 표시된다."""

    ROWS = [
        {"dongNm": "101동", "hoNm": "201호", "pblntfPc": "300000000"},
        {"dongNm": "102동", "hoNm": "201호", "pblntfPc": "500000000"},
    ]

    def test_picks_the_matching_dong(self):
        row = _pick_apart_unit(self.ROWS, "101동", "201호")
        self.assertEqual(row["pblntfPc"], "300000000")

    def test_matches_ignoring_non_digits(self):
        # 사이트는 '제101동'·'101'·'101동'을 섞어 준다. 숫자만 보고 맞춘다.
        row = _pick_apart_unit(self.ROWS, "제101동", "201")
        self.assertEqual(row["pblntfPc"], "300000000")

    def test_refuses_to_guess_when_dong_unknown_and_prices_differ(self):
        # 여기서 아무거나 고르면 옆집 가격이 붙는다. 모르면 비워두는 게 맞다.
        self.assertIsNone(_pick_apart_unit(self.ROWS, "", "201호"))

    def test_accepts_when_dong_unknown_but_prices_agree(self):
        rows = [dict(r, pblntfPc="300000000") for r in self.ROWS]
        row = _pick_apart_unit(rows, "", "201호")
        self.assertEqual(row["pblntfPc"], "300000000")

    def test_no_ho_means_no_match(self):
        self.assertIsNone(_pick_apart_unit(self.ROWS, "101동", ""))


if __name__ == "__main__":
    unittest.main()
