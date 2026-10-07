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

    def test_부존재확인_청구기각은_유치권이_인정된_것(self):
        # 성남 2023타경56618 — 채권자가 '유치권 없다' 고 낸 소송이 기각됐다. 확정이어도 남음이다.
        self.assertEqual(R.lien_status("유치권 행사중임[2023.11.6. 유치권신고, 2024가합205381 유치권 부존재 확인의 소 "
                                       "원고 청구기각 판결(2025.9.2. 선고, 2025.9.18. 확정)]"), "남음")

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

    def test_명세서_점유자_표에서_전입일을_읽는다(self):
        spec = ("록 신청일자\n김선명 주택임 2022.03.26 ~ 230,000,000 2022.03.25 2022.02.28\n"
                "이동학 현황조사 2009.10.22.\n박효범 미상 2019.02.10.~ 30,000,000 미상 2019.02.13 2022.05.27\n"
                "김복남 현황조사 입원 2023.02.17\n<비고>\n김선명: 주택임차권 등기일은 2024.04.03.임")
        got = [d.isoformat() for d in R.spec_moveins(spec)]
        self.assertEqual(got, ["2022-03-25", "2009-10-22", "2019-02-13"])   # 입원(요양원)·비고 날짜는 아니다

    def test_현황조사서엔_없고_명세서에만_있는_등기_임차인도_센다(self):
        # 표엔 지금 사는 사람(최선순위 뒤 전입)만, 명세서엔 이사 나간 임차권등기 세입자(앞 전입)가 있다.
        occ = [{"name": "홍길동", "role": "임차인", "전입일자": "2024.10.10"}]
        spec = "록 신청일자\n김철수 전부 2021.05.11. 160,000,000 2021.05.11. 2021.04.02.\n<비고>"
        got = R.opposability(occ, {"date": "2022-04-04"}, spec)
        self.assertEqual(got["summary"], "있음")

    def test_명세서_비고의_반환채권_포기도_포기다(self):
        spec = "록 신청일자\n<비고>\n주택도시보증공사: 잔존 임차보증금반환채권을 포기하고 주택임차권등기 말소에 동의한다는 취지의 확약서를 제출함"
        self.assertTrue(R.tenant_waived("", spec))
        self.assertFalse(R.tenant_waived("최선순위 지상권이 있으나 신청채권자로부터 말소동의서가 제출됨", ""))

    def test_떠안는_권리_칸은_종류마다_따로(self):
        def got(sec):
            return R.inherited_rights("효력이 소멸되지 아니하는 것 " + sec + " 매각에 따라 설정된 것으로 보는 지상권의 개요")
        self.assertEqual(got("1. 목록 1 을구 1번 지상권설정등기는 말소되지 않고 매수인이 인수함. 2. 갑구 17번 소유권이전등기청구권 "
                             "가등기는 말소되지 않고 매수인이 인수함."), {"지상권": "떠안음", "가등기": "떠안음"})
        self.assertEqual(got("을구 순위 3번 주택임차권등기(다만 주택도시보증공사의 말소동의 확약서가 제출됨)"), {"임차권": "해소"})
        # PDF 가 낱말 가운데 빈칸을 넣는다
        self.assertEqual(got("목록2 을구 2번 지상권 설정등기(2021. 11. 19.). 이에 대해 지상권자의 말소동 의서가 제출되어 있음."),
                         {"지상권": "해소"})
        self.assertEqual(got("갑구 15번 가처분 등기"), {"가처분": "떠안음"})    # 이 칸에 적혔다는 것 자체가 안 없어진다는 뜻
        self.assertEqual(got("해당사항없음"), {})
        # 구분 기호 없이 이어 적어도 확약서는 임차권 것만 (인천 2025타경513729)
        self.assertEqual(got("을구 순위 1번 주택임차권등기(다만 주택도시보증공사의 말소동의 확약서가 제출됨) "
                             "별도등기(대지권 목적토지1 을구1번 지상권설정등기)"), {"임차권": "해소", "지상권": "떠안음"})

    def test_명세서_지상권_개요와_비고란_딱지(self):
        def got(sup, rem):
            return R.spec_flags(f"지상권의 개요 {sup} 비고란 {rem} 1: 매각목적물에서 제외되는 미등기건물 등이 있을 경우")
        self.assertEqual(got("매각에서 제외되는 제시외 건물을 위한 법정지상권 성립 여지 있음", ""), ["법정지상권"])
        self.assertEqual(got("지상 분묘에 대한 분묘기지권 성립여부 불분명", ""), ["분묘기지권"])
        self.assertEqual(got("분묘를 위하여 분묘기지권 성립여지 있음.(성립여부는 불분명)", ""), ["분묘기지권"])  # 토막은 되풀이
        self.assertEqual(got("법정지상권 성립하지 않음", "해당사항없음"), [])
        self.assertEqual(got("", "지적도상 맹지임. 목록3 지분매각. 위반건축물 등재. 대지권 미등기"),
                         ["위반건축물", "맹지", "대지권미등기", "지분매각"])
        # 부정 — 필요 없다·완료됐다
        self.assertEqual(got("", "농지취득자격증명 없이 취득 가능"), [])
        self.assertEqual(got("", "개시결정 당시에는 대지권 미등기이나, 이후 대지권등기가 완료됨"), [])
        # 서식 안내문의 '미등기건물'·'가등기' 는 안 읽는다
        self.assertEqual(got("", ""), [])

    def test_대항할_수_있는_임차인_문장도_높음(self):
        from court_auction_crawler.enrichment import parse_special_rights
        stated = "매수인에게 대항할 수 있는 임차인 있음(보증금 1억). 배당에서 전액 변제되지 않으면 잔액을 매수인이 인수함"
        self.assertIn("대항력있는임차인", R.spec_flags(f"비고란 {stated} 1: 매각목적물에서 제외되는 미등기건물"))
        self.assertIn("대항력있는임차인", parse_special_rights(stated))
        self.assertNotIn("대항력있는임차인", R.spec_flags("비고란 매수인에게 대항할 수 있는 임차인 없음 1: 매각목적물에서 제외되는"))
        waived = stated + " 주택도시보증공사가 대항력을 포기하는 확약서 제출"
        self.assertNotIn("대항력있는임차인", R.spec_flags(f"비고란 {waived} 1: 매각목적물에서 제외되는"))
        self.assertEqual(parse_special_rights(waived), ["대항력포기"])

    def test_전입일_칸의_글자(self):
        senior = {"date": "2020-01-01"}
        def got(value, use):
            return R.opposability([{"name": "홍길동", "role": "임차인", "용도": use, "전입일자": value}], senior)["summary"]
        self.assertEqual(got("미전입", "주거"), "없음")
        self.assertEqual(got("해당없음.", "주거"), "없음")
        self.assertEqual(got("미전입", "점포"), "모름")          # 상가는 사업자등록으로 대항력 — 이 칸만으론 모른다
        self.assertEqual(got("미등록", "기타 - 사무실"), "모름")
        self.assertEqual(got("미상", "주거"), "모름")
        self.assertEqual(got("확정일자", "주거"), "모름")        # 칸이 밀려 머리글이 들어온 것 — 빈칸이다
        self.assertEqual(got("2019년10월7일", "주거"), "있음")
        self.assertEqual(got("2021년 10월 7일", "점포"), "없음")
        self.assertEqual(R.opposability([{"role": "임차인", "용도": "주거", "전입일자": "미전입"}], None)["summary"], "모름")

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
        waived = json_waived = dict(item)
        r = __import__("json").loads(item["rights_json"]); r["waived"] = True
        waived = {**item, "rights_json": __import__("json").dumps(r, ensure_ascii=False)}
        flags = public_auction_summary(waived)["auction"]["special_rights"]
        self.assertIn("대항력포기", flags)
        self.assertNotIn("대항력가능", flags)

    def test_떠안는_가등기는_높음_지상권은_보통(self):
        from court_auction_crawler.enrichment import build_screening
        item = self._item(active=True, jev_seen=True)
        r = __import__("json").loads(item["rights_json"]); r["inherited"] = {"가등기": "떠안음", "지상권": "떠안음", "가처분": "해소"}
        flags = public_auction_summary({**item, "rights_json": __import__("json").dumps(r, ensure_ascii=False)})["auction"]["special_rights"]
        self.assertIn("선순위가등기", flags)
        self.assertIn("지상권인수", flags)
        self.assertNotIn("선순위가처분", flags)
        self.assertEqual(build_screening(["선순위가등기"])["risk_level"], "높음")
        self.assertEqual(build_screening(["지상권인수"])["risk_level"], "보통")

    def test_목록_스냅샷의_요약_칸만으로도_딱지가_붙는다(self):
        # 스냅샷 행엔 rights_json 이 없다 — store 가 json_extract 한 칸(글자)만 온다
        item = {**self._item(active=True, jev_seen=True), "rights_json": "",
                "rights_spec_flags": '["법정지상권", "대지권미등기"]', "rights_inherited": '{"가등기": "떠안음"}'}
        flags = public_auction_summary(item)["auction"]["special_rights"]
        for label in ("법정지상권", "대지권미등기", "선순위가등기"):
            self.assertIn(label, flags)

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
