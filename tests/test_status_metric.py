"""법정동 비교 규칙을 못 박는다 — `geocoder.same_place` 와 그것을 쓰는 status 도구.

이 규칙은 두 곳에서 쓰인다. 수집기는 **브이월드 응답을 거를 때**(다른 리의 좌표를
verified 로 박지 않게), status 도구는 **이미 박힌 것을 셀 때**. 둘이 어긋나면
지표가 코드를 못 따라가므로 규칙은 `geocoder` 한 곳에만 둔다.

이 지표는 2026-09-17에 **네 번** 고쳤다. 고칠 때마다 숫자가 크게 흔들렸다.

    176건 → 570건 → 103건 → 85건 → 104건

틀린 원인이 매번 달랐다.
  1) `[가-힣]{1,2}동` 으로 건물 동을 거르려다 `가좌동`·`강제동` 같은 진짜 법정동까지 걸렀다
  2) 도로명주소의 면(面)만 잡고 정규화의 리(里)와 맞대 멀쩡한 것을 틀렸다고 했다
  3) `족동2길` 같은 도로명을 첫 숫자 앞에서 잘라 `족동` 을 법정동으로 오인했다
  4) **원본 주소와 댔다.** 수집기는 깎은 쿼리와 대는데(함정 ⑤) 지표만 원본과 댔다.
     `양천로 400-12 (가양동, 더리브골드타워)` 를 `양천로 400-12` 로 묻고 브이월드가
     `양천로 400-12 (등촌동)` 이라 **정확히** 답한 것까지 틀렸다고 셌다. 다시
     물어봐도 같은 답이 오니 교정도 안 된다 — 도구가 제 지표와 싸웠다.
  5) 괄호를 통째로 뒤져 건물명 조각을 집었다 — `(연동,신제주연동트리플시티)` 에서
     `신제주연동`. 괄호는 `(법정동, 건물명)` 순서이므로 쉼표 앞만 본다.
  6) 첫 숫자에서 문자열을 잘라 **없던 단어**를 만들었다 — `루원시티공동2블록` →
     `루원시티공동`. 길이로는 못 거른다(진짜 법정동에도 `등억알프스리` 6자가 있다).
     숫자가 든 토막은 통째로 버린다.

**틀린 지표는 없는 지표보다 나쁘다.** 초록불이 거짓이 되기 때문이다.
그래서 판정 규칙을 여기 고정한다.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "status.py"
sys.path.insert(0, str(ROOT / "src"))


def _load():
    spec = importlib.util.spec_from_file_location("status_tool", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LegalDongTest(unittest.TestCase):
    def setUp(self):
        from court_auction_crawler import geocoder
        self.m = geocoder

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

    def test_숫자가_든_토막은_통째로_버린다(self):
        # 첫 숫자에서 자르면 `루원시티공동2블록` 이 `루원시티공동` 이 되어 뽑힌다
        self.assertEqual(self.m.legal_dong(
            "인천광역시 서구 가정동 루원시티공동2블록 포레나루원시티 207동 15층1503호"), "가정동")

    def test_괄호는_쉼표_앞만_본다(self):
        # 통째로 뒤지면 건물명 조각 `신제주연동`·`김해내외대동` 을 집는다
        self.assertEqual(self.m.legal_dong(
            "제주특별자치도 제주시 연북로 106 4층103동418호 (연동,신제주연동트리플시티)"), "연동")
        self.assertEqual(self.m.legal_dong(
            "경상남도 김해시 함박로 101 112동 2층201호 (외동,김해내외대동한마음타운)"), "외동")

    def test_못_고르면_None(self):
        self.assertIsNone(self.m.legal_dong(""))
        self.assertIsNone(self.m.legal_dong("서울특별시 강남구"))


class SamePlaceTest(unittest.TestCase):
    def setUp(self):
        from court_auction_crawler import geocoder
        self.m = geocoder

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

    def test_수집기가_다른_리의_응답을_거른다(self):
        """가드가 실제로 거르는지. 이게 없어 55건이 verified 로 박혔다."""
        from court_auction_crawler.geocoder import _matches_region

        self.assertFalse(_matches_region(
            "경상남도 하동군 고전면 명교리 182-9", ["경상남도 하동군 고전면 고하리 1-1"]))
        self.assertTrue(_matches_region(
            "경상남도 하동군 고전면 명교리 182-9", ["경상남도 하동군 고전면 명교리 182"]))

    def test_거부는_항목_단위다(self):
        """줄마다 따로 보면 동을 말하지 않는 줄 하나가 항목 전체를 통과시킨다.

        장소검색 결과의 road 가 비어 있어 `서구 영동빌라` 가 가좌동 대신
        석남동에 찍힌 채 verified 로 들어왔다."""
        from court_auction_crawler.geocoder import _matches_region

        self.assertFalse(_matches_region(
            "인천광역시 서구 가좌동 146-44 영동빌라 1동",
            ["영동빌라", "", "인천광역시 서구 심곡동 329-9대"]))

    def test_면까지만_물으면_거르지_않는다(self):
        """근사 핀(_try_coarse_address)은 일부러 면까지만 묻는다. 막으면 안 된다."""
        from court_auction_crawler.geocoder import _matches_region

        self.assertTrue(_matches_region(
            "경상남도 하동군 고전면", ["경상남도 하동군 고전면 고하리 1-1"]))


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


class 무엇과대조하나Test(unittest.TestCase):
    """지표와 교정 도구가 **수집기와 같은 것**을 대조하는지.

    이게 어긋나면 지표는 코드로 막을 수 없는 것을 고발하고, 교정 도구는
    고쳐지지 않는 것을 영원히 다시 물어본다."""

    도로명 = {
        "address": "서울특별시 강서구 양천로 400-12 제1층 제101호 (가양동, 더리브골드타워)",
        "normalized_address": "등촌동 849",
        "geocode_query": "서울특별시 강서구 양천로 400-12",
        "coordinate_source": "address",
    }
    건물명 = {
        "address": "인천광역시 서구 가정로137번길 23 9동 지하층1호 (가좌동,삼우빌라)",
        "normalized_address": "인천광역시 서구 석남동 525-9대",
        "geocode_query": "인천광역시 서구 삼우빌라",
        "coordinate_source": "building",
    }

    @staticmethod
    def _판정(row):
        from court_auction_crawler.geocoder import same_place
        물은것 = row["geocode_query"] if row["coordinate_source"] == "address" else row["address"]
        return same_place(물은것 or "", row["normalized_address"]) is False

    def test_도로명을_정확히_푼_것은_틀린_게_아니다(self):
        self.assertFalse(self._판정(self.도로명))

    def test_다른_동의_동명이건물은_틀린_것이다(self):
        self.assertTrue(self._판정(self.건물명))

    def test_수집기도_동명이건물을_거른다(self):
        from court_auction_crawler.geocoder import _same_region
        self.assertFalse(_same_region(self.건물명["address"], {
            "address": {"parcel": self.건물명["normalized_address"]},
            "point": {"x": "126.6", "y": "37.5"}}))

    def test_지표와_교정도구가_같은_수를_센다(self):
        """둘이 갈라지면 교정해도 지표가 안 줄어든다. 실제로 세어 맞춰본다."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.sqlite3"
            con = sqlite3.connect(db)
            con.execute(
                "CREATE TABLE auction_items (item_key TEXT, address TEXT, "
                "normalized_address TEXT, geocode_query TEXT, coordinate_source TEXT, "
                "coordinate_quality TEXT, lat REAL, lng REAL, geocoded_at TEXT, is_active INT)")
            for i, row in enumerate((self.도로명, self.건물명)):
                con.execute("INSERT INTO auction_items VALUES (?,?,?,?,?,?,?,?,?,1)", (
                    f"k{i}", row["address"], row["normalized_address"], row["geocode_query"],
                    row["coordinate_source"], "verified", 37.5, 126.9, "2026-09-17T00:00:00+00:00"))
            con.commit()
            con.close()

            tool = _load()
            tool.DB = db
            self.assertEqual(tool.wrong_place(), 1, "status 가 도로명 정답까지 셌다")

            spec = importlib.util.spec_from_file_location(
                "g12", ROOT / "scripts" / "g12_refix_coords.py")
            g12 = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(g12)
            rows = g12.wrong_place_rows(db, include_inactive=True)
            self.assertEqual([r["item_key"] for r in rows], ["k1"],
                             "교정 도구가 status 와 다른 것을 잡는다")


if __name__ == "__main__":
    unittest.main()
