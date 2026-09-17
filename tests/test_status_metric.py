"""`scripts/status.py` 가 '엉뚱한 좌표'를 세는 방식이 맞는지 못 박는다.

이 지표는 2026-09-17에 **네 번** 고쳤다. 고칠 때마다 숫자가 크게 흔들렸다.

    176건 → 570건 → 103건 → 85건

틀린 원인이 매번 달랐다.
  1) `[가-힣]{1,2}동` 으로 건물 동을 거르려다 `가좌동`·`강제동` 같은 진짜 법정동까지 걸렀다
  2) 도로명주소의 면(面)만 잡고 정규화의 리(里)와 맞대 멀쩡한 것을 틀렸다고 했다
  3) `족동2길` 같은 도로명을 첫 숫자 앞에서 잘라 `족동` 을 법정동으로 오인했다

**틀린 지표는 없는 지표보다 나쁘다.** 초록불이 거짓이 되기 때문이다.
그래서 판정 규칙을 여기 고정한다.
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "status.py"


def _load():
    spec = importlib.util.spec_from_file_location("status_tool", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LegalDongTest(unittest.TestCase):
    def setUp(self):
        self.m = _load()

    def test_지번주소에서_법정동을_고른다(self):
        self.assertEqual(self.m.legal_dong("인천광역시 서구 가좌동 146-44 영동빌라 1동 2층201호"), "가좌동")
        self.assertEqual(self.m.legal_dong("경상남도 하동군 고전면 명교리 182-9"), "명교리")

    def test_건물_동은_법정동이_아니다(self):
        # '1동'은 지번 뒤에 온다. 위치로 갈린다.
        self.assertEqual(self.m.legal_dong("서울특별시 강남구 역삼동 1-1 래미안 101동 202호"), "역삼동")

    def test_도로명은_법정동이_아니다(self):
        # '족동2길'을 '족동'으로 자르면 안 된다
        self.assertEqual(self.m.legal_dong("충청북도 충주시 엄정면 족동2길 118-3"), "엄정면")
        self.assertEqual(self.m.legal_dong("경기도 이천시 대월면 대산로247번길 20-33"), "대월면")

    def test_도로명주소는_괄호에서_법정동을_찾는다(self):
        self.assertEqual(
            self.m.legal_dong("충청북도 제천시 장평천로 27-12(강제동) 에이동 1층108"), "강제동")

    def test_못_고르면_None(self):
        self.assertIsNone(self.m.legal_dong(""))
        self.assertIsNone(self.m.legal_dong("서울특별시 강남구"))


class SamePlaceTest(unittest.TestCase):
    def setUp(self):
        self.m = _load()

    def test_진짜_다른_동네는_False(self):
        for a, b in [
            ("경상남도 하동군 고전면 명교리 182-9", "경상남도 하동군 고전면 고하리 1-1"),
            ("인천광역시 서구 가좌동 146-44 영동빌라 1동", "인천광역시 서구 심곡동 329-9대"),
            ("충청남도 천안시 동남구 봉명동 109-26", "충청남도 천안시 동남구 광덕리 1"),
        ]:
            self.assertIs(self.m.same_place(a, b), False, f"{a} vs {b}")

    def test_읍면과_리는_상하관계라_비교하지_않는다(self):
        # 금성리가 곤명면 안에 있을 수 있다. 도로명주소에는 리가 없다.
        self.assertIsNone(self.m.same_place(
            "경상남도 사천시 곤명면 막골길 267-195", "경상남도 사천시 곤명면 금성리 12"))
        self.assertIsNone(self.m.same_place(
            "경기도 평택시 팽성읍 계양로 771-12", "경기도 평택시 팽성읍 노양리 3"))

    def test_접미만_다르면_같은_곳(self):
        self.assertIs(self.m.same_place(
            "경상남도 고성군 거류면 당동리 17", "경상남도 고성군 거류면 당동 1-6"), True)

    def test_같으면_True(self):
        self.assertIs(self.m.same_place(
            "서울특별시 강남구 역삼동 1-1", "서울특별시 강남구 역삼동 1-1"), True)

    def test_한쪽을_못_고르면_None(self):
        self.assertIsNone(self.m.same_place("서울특별시 강남구", "서울특별시 강남구 역삼동 1-1"))


class StatusToolShapeTest(unittest.TestCase):
    """도구가 깨지지 않았는지만 본다. 숫자는 DB에 따라 달라지므로 보지 않는다."""

    @classmethod
    def setUpClass(cls):
        # checks() 는 DB 전수 스캔을 한다(4GB). 테스트마다 부르면 분 단위가 된다.
        cls.m = _load()
        cls.rows = cls.m.checks()

    def test_판정에_필요한_열이_다_있다(self):
        rows = self.rows
        self.assertGreaterEqual(len(rows), 17)
        for row in rows:
            for key in ("no", "title", "owner", "state", "measure", "by"):
                self.assertIn(key, row, f"{row.get('no')} 에 {key} 가 없다")
            self.assertIn(row["state"],
                          (self.m.DONE, self.m.WIP, self.m.TODO, self.m.FIXED_LIMIT, self.m.UNKNOWN))

    def test_격차_문서가_다_있다(self):
        gaps = self.m.ROOT / "gaps"
        for row in self.rows:
            hits = list(gaps.glob(f"{row['no']}-*.md"))
            self.assertTrue(hits, f"{row['no']} 문서가 없다")

    def test_출력이_만들어진다(self):
        text = self.m.render(self.rows)
        self.assertIn("수집기 현황", text)
        self.assertIn("근거", text)


if __name__ == "__main__":
    unittest.main()
