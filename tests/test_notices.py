"""배당요구종기공고 행 묶기 규칙을 못 박는다 (G15).

결과표는 **한 사건이 두 줄**이다. 6칸 행(사건번호·소재지·소유자·공고일·담당계·목록)과
3칸 행(채무자·경매개시결정일자·배당요구종기일)이 짝이다. 실제 화면에서 뜬 모양이다.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from court_auction_crawler.notices import parse_rows  # noqa: E402

실제 = [
    ["서울중앙지방법원 2026타경101311", "서울특별시 관악구 신림동 1667-10 2층204호",
     "조OOO OOOOO OOO OOO 외2명", "2026.07.14", "경매2계", "목록"],
    ["조OOO OOOOO OOO OOO 외 2명", "2026.07.02", "2026.09.22"],
    ["서울중앙지방법원 2026타경101363", "서울특별시 서초구 서초동 1445-4 래미안서초유니빌 4층411호",
     "김OO", "2026.07.14", "경매2계", "목록"],
    ["김OO", "2026.07.08", "2026.09.22"],
]


class 행묶기Test(unittest.TestCase):
    def test_두_줄을_한_건으로_묶는다(self):
        got = parse_rows(실제)
        self.assertEqual(len(got), 2)
        첫째 = got[0]
        self.assertEqual(첫째["court"], "서울중앙지방법원")
        self.assertEqual(첫째["case_no"], "서울중앙지방법원 2026타경101311")
        self.assertEqual(첫째["dept"], "경매2계")
        self.assertEqual(첫째["opened_at"], "2026.07.02")
        self.assertEqual(첫째["dividend_deadline"], "2026.09.22")
        self.assertEqual(got[1]["debtor"], "김OO")

    def test_짝이_없어도_사건을_버리지_않는다(self):
        """종기일을 못 읽었다고 사건번호까지 놓치는 쪽이 더 나쁘다."""
        got = parse_rows(실제[:1])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["dividend_deadline"], "")

    def test_다음_사건의_머리를_짝으로_삼지_않는다(self):
        붙임 = [실제[0], 실제[2]]          # 3칸 행 없이 6칸 행이 연달아 오는 경우
        got = parse_rows(붙임)
        self.assertEqual(len(got), 2)
        self.assertEqual(got[0]["opened_at"], "")

    def test_머리글_행은_건너뛴다(self):
        머리글 = [[], ["사건번호", "소재지", "소유자", "공고일", "담당계", "부동산등의 표시"]]
        self.assertEqual(parse_rows(머리글 + 실제), parse_rows(실제))

    def test_법원명을_사건번호에서_떼어낸다(self):
        got = parse_rows([["의정부지방법원 고양지원 2025타경12345", "주소", "소유",
                           "2026.01.01", "경매3계", "목록"]])
        self.assertEqual(got[0]["court"], "의정부지방법원 고양지원")


if __name__ == "__main__":
    unittest.main()
