"""권리 판정 규칙을 못 박는다 (rights.py) — 2026-09-23 정답지로 세운 규칙들.

문장은 실제 비고·현황조사서·매각물건명세서의 **모양**만 따왔고 이름은 가상이다.
정답지 자체(실데이터 키·정답)는 `jev_golden/` 에 있고 `scripts/jev_eval.py` 가 DB 로 잰다.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from court_auction_crawler import rights as R  # noqa: E402
from court_auction_crawler.enrichment import public_auction_detail, public_auction_summary  # noqa: E402

HUG = ("주택도시보증공사로부터 '임차보증금에 대하여 우선변제권만 주장하고 대항력은 포기하며, 전액을 "
       "변제받지 못하더라도 임차권등기를 말소하는 것에 동의한다.'는 내용의 확약서가 제출됨")


class 대항력포기(unittest.TestCase):
    def test_같은_임차인을_되풀이한_것은_포기로_안전(self):
        # '대항력 있음 … 단, 포기' — 포기를 지워도 '대항력' 이 남지만 같은 임차인이다.
        text = "대항력 있는 임차인 있음(배당에서 보증금이 전액 변제되지 않으면 잔액을 매수인이 인수함). 단, " + HUG
        self.assertFalse(R.waiver_leaves_other_tenant(text))

    def test_포기와_별개인_미상_임차인은_위험(self):
        self.assertTrue(R.waiver_leaves_other_tenant("1. " + HUG + " 2. 대항력 여지 있는 임대차관계 미상의 임차인 있음."))

    def test_임대차관계_불분명도_다른_임차인이다(self):
        self.assertTrue(R.waiver_leaves_other_tenant("1.임대차관계 불분명(전입인 홍길동) 2." + HUG))

    def test_있을_수_있음도_위험(self):
        self.assertTrue(R.waiver_leaves_other_tenant("- 대항력 있는 임차인 있을 수 있음. - " + HUG))


class 유치권(unittest.TestCase):
    def test_언급_없음(self):
        self.assertIsNone(R.lien_status("일괄매각. 제시외 건물 포함"))

    def test_불분명은_남음(self):
        self.assertEqual(R.lien_status("홍길동이 2025.1.2. 유치권신고를 하였으나 그 성립여부는 불분명함"), "남음")

    def test_취하는_해소(self):
        self.assertEqual(R.lien_status("1. 홍길동이 유치권신고를 하였으나 불분명함 2. 유치권자 홍길동이 "
                                       "2025.01.16에 유치권취하(포기)서를 제출함"), "해소")

    def test_부존재_확정은_해소(self):
        self.assertEqual(R.lien_status("유치권 신고가 있으나, 채권자로부터 유치권이 부존재하다는 확정판결이 제출됨"), "해소")

    def test_원고_패소는_유치권이_인정된_것(self):
        # '부존재' 글자만 보면 정반대로 틀리는 가장 위험한 유형
        self.assertEqual(R.lien_status("유치권부존재확인등 소송 사건에서 원고패소판결 및 유치권 권리신고서 제출"), "남음")

    def test_일부_승소는_남음(self):
        self.assertEqual(R.lien_status("유치권부존재확인의소를 제기하여 일부승소판결을 받음(401호는 유치권 성립 인정)"), "남음")

    def test_존재확인_판결은_남음(self):
        self.assertEqual(R.lien_status("유치권 신고서가 제출되었고 유치권 존재확인판결이 제출됨"), "남음")

    def test_확정_안_된_승소는_아직이다(self):
        # 2026-09-28 엇갈림 검토: 규칙이 틀린 9건이 전부 이 무리였고 전부 위험한 쪽이었다
        for text in ("유치권신고가 있으나 불분명함. 신청채권자가 유치권부존재확인소송을 제기하여 1심 승소함.",
                     "유치권부존재확인 소송 1심에서 신청채권자가 승소하였으나 현재 미확정 상태임.",
                     "유치권신고를 하였고 원고승소판결을 제출하였으나 유치권자가 추완 항소장을 접수하였음",
                     "유치권 부존재 관련 소명자료가 제출되었음(유치권의 성립 여부는 최종 확정되지 아니하였으므로 사전 확인 요함)"):
            self.assertEqual(R.lien_status(text), "남음", text)

    def test_존재한다는_확정판결은_유치권이_인정된_것(self):
        self.assertEqual(R.lien_status("유치권 부존재 확인의 소에 의하여 위 금원에 대하여 유치권이 존재한다는 확정판결 있음"), "남음")

    def test_한_명은_확정이어도_다른_소송이_진행_중이면_남음(self):
        text = ("가나건설의 유치권 부존재를 확인하는 화해권고결정이 확정되고, 홍길동에 대한 소송은 진행 중임.")
        self.assertEqual(R.lien_status(text), "남음")

    def test_타_채권자의_유치권_행사는_남음(self):
        text = ("309호는 타 채권자의 유치권 행사 중으로 점유하고 있음. 가나냉각기의 유치권이 존재하지 않는다는 판결이 "
                "있었고 상고심에서 상고기각 판결이 있었음")
        self.assertEqual(R.lien_status(text), "남음")

    def test_항소기각_상고기각까지_간_부존재는_해소(self):
        text = "유치권신고서가 제출되었으나 유치권이 존재하지 않는다는 판결이 있었고 항소기각, 상고심에서 상고기각 판결이 있었음"
        self.assertEqual(R.lien_status(text), "해소")

    def test_일부만_해소되고_새_신고가_남으면_남음(self):
        text = ("- 신고인 홍길동의 유치권 신고가 있으나 유치권부존재확인 사건의 확정 판결이 제출됨. "
                "- 2026. 4. 28.자 가나건설 주식회사로부터 유치권권리신고서가 제출되었으나 그 성립여부는 불분명함.")
        self.assertEqual(R.lien_status(text), "남음")


class 점유확인(unittest.TestCase):
    def test_만나서_들음(self):
        memo = "부동산의 점유관계 점유관계 임차인(별지)점유 기타 현장에서 임차인을 만나 문의한 바, 운영하고 있다고 함."
        self.assertIs(R.occupancy_check(memo)["confirmed"], True)

    def test_폐문부재는_못_만남_그리고_표를_믿으면_안_됨(self):
        memo = "부동산의 점유관계 점유관계 임차인(별지)점유 기타 ① 폐문부재로 점유 및 임대차 관계 알 수 없음 ② 전입세대확인서에 홍길동 세대가 전입"
        got = R.occupancy_check(memo)
        self.assertIs(got["confirmed"], False)
        self.assertTrue(got["label_unverified"])

    def test_관리사무소에만_물은_것은_만난_게_아님(self):
        memo = ("부동산의 점유관계 점유관계 미상 기타 폐문부재로 아무도 만나지 못하여 연락이 없음. "
                "관리사무소 직원에게 문의한 바, 현재 공실이라고 함.")
        self.assertIs(R.occupancy_check(memo)["confirmed"], False)

    def test_띄어쓰기_없이_붙은_문장도_가른다(self):
        memo = "부동산의 점유관계 점유관계 임차인(별지)점유 기타 임차인 홍길동에게 유선으로 문의함.관리사무소에 문의한바 미납 관리비는 없다고함."
        self.assertIs(R.occupancy_check(memo)["confirmed"], True)


class 최선순위와_대항력(unittest.TestCase):
    def test_변형_표기(self):
        for text, want in (("최선순위 / 별지 기재와 같음 2023.07.19. (근저당권) 배당요구종기", "2023-07-19"),
                           ("최선순위 / 별지 기재와 같음 21. 2. 8. 근저당권 배당요구종기 2026. 3. 3.", "2021-02-08"),
                           ("최선순위 / 별지 기재와 같음 2022. 2. 10. 근정당권 배당요구종기", "2022-02-10"),
                           ("최선순위 2025. 4. 30. 강제경매개 / 별지 기재와 같음 배당요구종기", "2025-04-30")):
            self.assertEqual((R.senior_right(text) or {}).get("date"), want, text)

    def test_찢어진_줄은_규칙이_포기하고_후보를_낸다(self):
        text = "사건 2025타경1 1 2026. 3. 31. / 최선순위 2025. 9. 1. / 별지 기재와 같음 배당요구종기 2025. 12. 2. / 강제경매개시결정"
        self.assertIsNone(R.senior_right(text))
        self.assertIn("2025. 9. 1", R.senior_candidates(text)[1])

    def test_전입이_빠르면_있음_같은_날이면_없음(self):
        senior = {"date": "2023-08-14"}
        got = R.opposability([{"name": "홍길동", "role": "임차인", "전입일자": "2023.06.21"},
                              {"name": "김철수", "role": "임차인", "전입일자": "2023.08.14"}], senior)
        self.assertEqual([t["opposable"] for t in got["tenants"]], ["있음", "없음"])
        self.assertEqual(got["summary"], "있음")

    def test_남의_호실_임차인은_빼고_판정한다(self):
        occ = [{"name": "홍길동", "role": "임차인", "전입일자": "2019.01.01", "소재지": "1. 서울 강서구 화곡동 1-2, 2층201호"},
               {"name": "김철수", "role": "임차인", "전입일자": "2022.01.01", "소재지": "2. 서울 강서구 화곡동 1-2, 2층202호"}]
        got = R.opposability(occ, {"date": "2020-01-01"}, address="서울 강서구 화곡동 1-2 2층202호")
        self.assertEqual([t["name"] for t in got["tenants"]], ["김철수"])
        self.assertEqual(got["summary"], "없음")   # 옆 호실(201) 임차인 때문에 '있음' 이 되면 안 된다

    def test_일괄매각은_거르지_않는다(self):
        occ = [{"name": "홍길동", "role": "임차인", "전입일자": "2019.01.01", "소재지": "10. 서울 강서구 화곡동 1-2, 2층203호"}]
        got = R.opposability(occ, {"date": "2020-01-01"}, address="서울 강서구 화곡동 1-2 2층201호", bulk=True)
        self.assertEqual(got["summary"], "있음")

    def test_주소가_하나도_안_맞으면_다_쓴다(self):
        occ = [{"name": "홍길동", "role": "임차인", "전입일자": "2019.01.01", "소재지": "전혀 다른 표기"}]
        self.assertEqual(R.opposability(occ, {"date": "2020-01-01"}, address="서울 강서구 화곡동 1-2")["summary"], "있음")

    def test_소유자는_임차인이_아니다(self):
        got = R.opposability([{"name": "홍길동", "role": "채무자겸소유자", "전입일자": "2010.01.01"}], {"date": "2020-01-01"})
        self.assertEqual(got["summary"], "임차인 없음")

    def test_명세서에만_임차인이_있으면_모름(self):
        self.assertEqual(R.opposability([], {"date": "2020-01-01"}, "주거 전유부분 주택임차 권자 2019.01.01")["summary"], "모름")
        self.assertEqual(R.opposability([], {"date": "2020-01-01"}, "조사된 임차내역없음")["summary"], "임차인 없음")


class 실명(unittest.TestCase):
    def test_후보는_넉넉히_회사는_뺀다(self):
        got = R.name_candidates("1. 홍길동이 유치권신고 2. 유치권자 홍길동이 취하. 김철수(가나공사)가 신고. 주식회사 다라건설이 신고")
        self.assertIn("홍길동", got)
        self.assertIn("김철수", got)
        self.assertNotIn("홍길동이", got)

    def test_두_글자_이름은_다른_낱말_속에서_안_가린다(self):
        self.assertEqual(R.mask_text("정원이 신고. 정원수 식재. 정원(임차인)", ["정원"]), "정○이 신고. 정원수 식재. 정○(임차인)")

    def test_문장_속_이름은_Jev가_확인해야_쓴다(self):
        survey = "소유자 김정년 세대만 전입. 채권자 이며 소유자 명의로 등기"
        base = dict(spec_text="", survey_text=survey, note="", occupants=[])
        self.assertEqual(R.compute_rights(**base)["names"], {})
        self.assertIn("김정년", R.compute_rights(**base)["name_candidates"])
        self.assertNotIn("이며", R.compute_rights(**base)["name_candidates"])
        self.assertEqual(R.compute_rights(**base, jev={"names": ["김정년"]})["names"], {"소유자": ["김정년"]})

    def test_표_칸_이름도_낱말은_거른다(self):
        got = R.harvest_names([], [{"name": "조사불가", "role": "임차인"}, {"name": "주거", "role": "임차인"},
                                   {"name": "홍길동", "role": "임차인"}])
        self.assertEqual(got["table"], {"임차인": ["홍길동"]})

    def _item(self, active: bool, jev_seen: bool) -> dict:
        rights = R.compute_rights(
            spec_text="최선순위 / 별지 기재와 같음 2020.1.1. 근저당권 배당요구종기",
            survey_text="부동산의 점유관계 점유관계 임차인(별지)점유 기타 현장에서 임차인 홍길동을 만나 문의한 바, "
                        "유치권자 박영희가 공사대금을 주장한다고 함. 임대차관계 조사서",
            note="박영희로부터 유치권신고가 있으나 불분명함",
            occupants=[{"name": "홍길동", "role": "임차인", "전입일자": "2019.01.01"}],
            jev={"names": ["박영희"]} if jev_seen else None)
        rights["jev"] = {"names": {"fp": "x", "found": ["박영희"]}} if jev_seen else {}
        return {"item_key": "t", "case_no": "2025타경1", "court": "가나지원", "address": "서울",
                "is_active": 1 if active else 0, "status": "" if active else "매각",
                "rights_json": __import__("json").dumps(rights, ensure_ascii=False),
                "detail": {"물건비고": "박영희로부터 유치권신고"}, "raw": {},
                "documents": [{"id": 1, "document_type": "현황조사서", "status": "collected",
                               "metadata": {"text": "유치권자 박영희가 공사대금을 주장. 임차인 홍길동 면담"}}]}

    def test_진행_중이면_실명_그대로(self):
        detail = public_auction_detail(self._item(active=True, jev_seen=True))
        self.assertIn("박영희", str(detail["documents"]))
        got = detail["rights"]
        self.assertEqual(got["tenants"][0]["name"], "홍길동")
        self.assertEqual(got["opposable"], "있음")
        self.assertIn("박영희", got["survey"]["memo"])

    def test_끝나면_가린다_문장_속_이름과_사건_화면까지(self):
        detail = public_auction_detail(self._item(active=False, jev_seen=True))
        got = detail["rights"]
        self.assertEqual(got["tenants"][0]["name"], "홍○○")
        self.assertNotIn("박영희", got["survey"]["memo"])
        self.assertNotIn("박영희", str(detail["detail"]))
        self.assertNotIn("박영희", str(detail["documents"]))
        self.assertNotIn("홍길동", str(detail["documents"]))

    def test_끝났는데_Jev가_아직이면_메모를_아예_안_싣는다(self):
        got = public_auction_detail(self._item(active=False, jev_seen=False))["rights"]
        self.assertEqual(got["survey"]["memo"], "")

    def test_계산된_대항력은_보통_법원이_쓴_것은_높음(self):
        from court_auction_crawler.enrichment import build_screening
        item = self._item(active=True, jev_seen=True)     # 계산상 '있음'(전입 2019 < 최선순위 2020)
        flags = public_auction_summary(item)["auction"]["special_rights"]
        self.assertIn("대항력가능", flags)
        self.assertEqual(build_screening(["대항력가능"])["risk_level"], "보통")
        stated = {**item, "raw": {"비고": "대항력 있는 임차인 있음. 배당에서 전액 변제되지 않으면 매수인이 인수함"}}
        flags = public_auction_summary(stated)["auction"]["special_rights"]
        self.assertIn("대항력있는임차인", flags)
        self.assertNotIn("대항력가능", flags)             # 같은 위험을 두 번 세지 않는다

    def test_목록에도_요약이_간다(self):
        got = public_auction_summary(self._item(active=True, jev_seen=True))["rights"]
        self.assertEqual(got["opposable"], "있음")
        self.assertEqual(got["lien"], "남음")



class 백필루프(unittest.TestCase):
    """2026-09-24: 6만 건을 한꺼번에 읽다 첫 저장이 잠금에 걸려 한 건도 못 쓰고 멈췄다."""

    def test_잠긴_건만_미루고_나머지는_끝까지(self):
        import sqlite3
        from unittest import mock
        from court_auction_crawler import cli

        class Fake:
            def __init__(self):
                self.saved = {}

            def list_rights_targets(self, *, version, limit, jev_missing=False, exclude=None):
                self.asked = max(getattr(self, "asked", 0), limit)
                if jev_missing:
                    return []
                keys = [f"k{n}" for n in range(1200) if f"k{n}" not in self.saved and f"k{n}" not in (exclude or set())]
                return [{"item_key": k, "item_note": "", "raw_json": "{}", "rights_json": "", "is_active": 1,
                         "documents": []} for k in keys[:limit]]

            def update_rights(self, key, value):
                if key == "k7":
                    raise sqlite3.OperationalError("database is locked")
                self.saved[key] = value

        store = Fake()
        with mock.patch.object(cli.time, "sleep"):
            got = cli.run_enrich_rights(store, limit=0, jev_budget=0, quiet=True)
        self.assertEqual(len(store.saved), 1199)
        self.assertEqual(got["locked_skip"], 1)
        self.assertEqual(got["targets"], 1200)
        # 한 번에 500 + (잠긴 1건) 만 가져온다 — 처리한 것만큼 불어나면 안 된다
        self.assertLessEqual(store.asked, 501)


if __name__ == "__main__":
    unittest.main()
