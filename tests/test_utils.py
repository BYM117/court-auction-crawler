from datetime import date
import unittest

from court_auction_crawler.crawler import build_partitions, is_safe_detail_url
from court_auction_crawler.utils import parse_date, parse_money, unique_headers


class UtilsTests(unittest.TestCase):
    def test_parse_date_accepts_common_formats(self):
        self.assertEqual(parse_date("2026-06-18"), date(2026, 6, 18))
        self.assertEqual(parse_date("2026.06.18"), date(2026, 6, 18))
        self.assertEqual(parse_date("2026/06/18"), date(2026, 6, 18))

    def test_parse_money_keeps_digits_only(self):
        self.assertEqual(parse_money("100,000,000원"), 100000000)
        self.assertIsNone(parse_money(""))

    def test_unique_headers_names_blanks_and_duplicates(self):
        self.assertEqual(
            unique_headers(["사건번호", "사건번호", ""]),
            ["사건번호", "사건번호_2", "컬럼3"],
        )

    def test_build_partitions_splits_by_court_and_date(self):
        partitions = build_partitions(
            ["서울중앙지방법원", "부산지방법원"],
            date(2026, 6, 1),
            date(2026, 6, 5),
            3,
        )

        self.assertEqual(len(partitions), 4)
        self.assertEqual(partitions[0].label(), "서울중앙지방법원 2026-06-01~2026-06-03")
        self.assertEqual(partitions[1].label(), "서울중앙지방법원 2026-06-04~2026-06-05")

    def test_safe_detail_url_only_allows_http_urls(self):
        self.assertTrue(is_safe_detail_url("https://www.courtauction.go.kr/detail"))
        self.assertTrue(is_safe_detail_url("http://www.courtauction.go.kr/detail"))
        self.assertFalse(is_safe_detail_url("javascript:alert(1)"))
        self.assertFalse(is_safe_detail_url("/relative/path"))


if __name__ == "__main__":
    unittest.main()
