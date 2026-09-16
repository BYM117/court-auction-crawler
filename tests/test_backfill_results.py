"""사건 화면 기일내역으로 매각결과를 뒤늦게 메우는 길.

매각결과 화면은 기일 다음날부터 이레만 보여주지만 사건 화면에는 금액까지 영구히
남는다(2026-09-16 확인). 여기서 지켜야 할 선은 두 개다.
  - 이미 받아둔 행을 덮지 않는다. 기일내역은 '매각'인데 금액이 빠질 때가 있다.
  - 새 기일을 받아 되살아난 물건(대금미납 재매각)을 옛 낙찰로 목록에서 내리지 않는다.
"""

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from court_auction_crawler.detail_crawler import parse_case_schedule
from court_auction_crawler.models import AuctionItem
from court_auction_crawler.store import AuctionStore

COURT = "서울동부지방법원"
CASE = "2025타경51429"


def schedule(*rows: list[str]) -> dict:
    return {"schedule_tables": [{"caption": "기일 내역", "rows": [
        ["물건번호", "감정평가액", "기일", "기일종류", "기일장소", "최저매각가격", "기일결과"],
        *rows,
    ]}]}


class ParseCaseScheduleTests(unittest.TestCase):
    def test_금액까지_읽는다(self):
        rows = parse_case_schedule(schedule(
            ["1 물건상세조회", "304,000,000원", "2026.09.07(10:00)", "매각기일",
             "법정", "151,552,000원", "매각 (175,900,000원)"],
            ["1", "304,000,000원", "2026.09.14(16:00)", "매각결정기일", "법정", "", ""],
            ["2", "90,000,000원", "2026.09.07(10:00)", "매각기일", "법정", "90,000,000원", "유찰"],
            ["2", "90,000,000원", "2026.11.02(10:00)", "매각기일", "법정", "72,000,000원", ""],
        ), COURT, CASE)
        self.assertEqual(
            [(row["물건번호"], row["매각기일"], row["매각결과"]) for row in rows],
            [("1", "2026.09.07", "매각 (175,900,000원)"), ("2", "2026.09.07", "유찰")],
        )

    def test_기일내역이_없으면_빈_목록(self):
        self.assertEqual(parse_case_schedule({"schedule_tables": []}, COURT, CASE), [])
        self.assertEqual(parse_case_schedule(None, COURT, CASE), [])


class BackfillSaleResultsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AuctionStore(Path(self.tmp.name) / "auction.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def _item(self, item_no: str, sale_date: str) -> str:
        self.store.upsert_items([AuctionItem({
            "사건번호": f"{COURT} {CASE}",
            "물건번호": item_no,
            "소재지": "서울특별시 광진구 능동로 120",
            "최저매각가격": "151,552,000",
            "매각기일": sale_date,
        })])
        return f"auction:{COURT}:{CASE}:{item_no}"

    def _sold(self, item_key: str):
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT sold_amount, sold_date, is_active FROM auction_items WHERE item_key = ?",
                (item_key,),
            ).fetchone()
        return (row["sold_amount"], row["sold_date"], row["is_active"])

    def test_놓친_낙찰가를_메우고_목록에서_내린다(self):
        key = self._item("1", "2026.09.07")
        rows = parse_case_schedule(schedule(
            ["1", "304,000,000원", "2026.09.07(10:00)", "매각기일", "법정",
             "151,552,000원", "매각 (175,900,000원)"],
        ), COURT, CASE)
        result = self.store.backfill_sale_results(rows)
        self.assertEqual((result["inserted"], result["sold"]), (1, 1))
        self.assertEqual(self._sold(key), (175_900_000, "2026.09.07", 0))

    def test_이미_받아둔_행은_덮지_않는다(self):
        key = self._item("1", "2026.09.07")
        self.store.record_sale_results([{
            "법원": COURT, "사건번호": CASE, "물건번호": "1",
            "매각기일": "2026.09.07", "매각결과": "매각 175,900,000",
        }])
        # 사건 화면에는 같은 기일이 금액 없이 '매각'으로만 남아 있을 수 있다.
        rows = parse_case_schedule(schedule(
            ["1", "304,000,000원", "2026.09.07(10:00)", "매각기일", "법정", "151,552,000원", "매각"],
        ), COURT, CASE)
        self.assertEqual(self.store.backfill_sale_results(rows)["inserted"], 0)
        self.assertEqual(self._sold(key)[0], 175_900_000)

    def test_새_기일을_받은_물건은_옛_낙찰로_내리지_않는다(self):
        # 대금미납으로 재매각되는 물건. 목록 기일이 옛 낙찰보다 뒤에 있다.
        key = self._item("1", "2026.11.02")
        rows = parse_case_schedule(schedule(
            ["1", "304,000,000원", "2026.06.24(10:00)", "매각기일", "법정",
             "151,552,000원", "매각 (175,900,000원)"],
        ), COURT, CASE)
        result = self.store.backfill_sale_results(rows)
        self.assertEqual((result["inserted"], result["sold"]), (1, 0))
        self.assertEqual(self._sold(key), (None, "", 1))


class MissingResultTargetTests(unittest.TestCase):
    """이레 창이 아직 열려 있는 기일까지 사건 화면으로 들추면, 정상 경로가 곧
    가져갈 물건을 두 번 긁게 된다. 대기열 맨 앞이 통째로 그런 것들이 된다."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AuctionStore(Path(self.tmp.name) / "auction.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def _seed(self, item_no: str, sale_date: date) -> None:
        self.store.upsert_items([AuctionItem({
            "사건번호": f"{COURT} {CASE}",
            "물건번호": item_no,
            "소재지": "서울특별시 광진구 능동로 120",
            "최저매각가격": "151,552,000",
            "매각기일": sale_date.strftime("%Y.%m.%d"),
        })])

    def test_이레_창이_닫힌_기일만_대상이다(self):
        self._seed("1", date.today() - timedelta(days=2))    # 결과 화면에 아직 떠 있다
        self._seed("2", date.today() - timedelta(days=30))   # 창이 닫혔다
        self._seed("3", date.today() + timedelta(days=7))    # 아직 오지도 않았다
        targets = self.store.list_missing_result_targets()
        self.assertEqual([row["item_no"] for row in targets], ["2"])


if __name__ == "__main__":
    unittest.main()
