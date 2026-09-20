"""요항표에서 **사실만** 뽑는 규칙을 못 박는다 (G06 · G14 재료).

감정평가서 PDF 원본은 협회 것이라 못 받고 링크도 금지지만, 법원이 화면에 주는
`감정평가요항표 요약` 은 받는다. 그 안의 **사실**(사용승인일·구조·형상·도로·설비)은
저작권 대상이 아니고, 무엇보다 검색·필터·비교가 된다 — 1,500자 문단은 안 읽힌다.

그리고 경매의 대표적 함정 셋이 이 글에 그대로 있다.
  · 제시목록 외의 물건 — 매각에서 빠지는 수목·구조물(소유자 미상이면 더 나쁘다)
  · 공부와의 차이     — 장부의 지목과 현황이 다름
  · 맹지             — 도로에 안 붙은 땅
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from court_auction_crawler.enrichment import (  # noqa: E402
    appraisal_facts, split_appraisal_summary)

구분건물 = (
    "1. 구분건물감정평가요항표 1) 위치 및 주위환경 대상물건은 경기도 고양시 식사동 소재 "
    "'고양국제고등학교' 북서측 인근에 위치하며, 주위는 다세대주택 등이 혼재함. "
    "2) 교통상황 차량 진출입이 가능하며 인근에 노선버스정류장이 소재함. "
    "3) 건물의 구조 철근콘크리트구조 평지붕 4층 건물 내 제103동 제2층 제201호로서, "
    "외 벽: 인조석붙임 마감 등 창 호: 샷시 창호 등으로 조사됨. 사용승인일: 2009.12.30 "
    "4) 이용상태 공부상 '다세대주택'임. "
    "5) 설비내역 위생설비 및 급·배수설비, 승강기설비, 도시가스에 의한 난방설비를 갖춤. "
    "6) 토지의 형상 및 이용상태 등고평탄한 세장형의 토지로서 '공동주택' 건부지로 이용 중임. "
    "7) 인접 도로상태등 동측으로 노폭 약 6m 내외의 포장도로와 접함. "
    "8) 토지이용계획 및 제한상태 계획관리지역, 과밀억제권역임. "
    "9) 공부와의 차이 ㅡ 10) 기타참고사항(임대관례 및 기타) 임대 미상임."
)

토지 = (
    "1. 토지감정요항표 1) 위치 및 주위환경 강원특별자치도 고성군 토성면 용암리 소재. "
    "2) 교통상황 차량진입이 가능함. 3) 형태 및 이용상태 자루형의 토지로서 토지임야 상태임. "
    "4) 인접 도로상태 맹지로서 별도의 진입로가 없음. "
    "5) 토지이용계획 및 제한상태 보전관리지역, 준보전산지임. "
    "6) 제시목록 외의 물건 소유자미상의 제시외수목(소나무 등) 수십여주가 식재되어 있음. "
    "7) 공부와의 차이 공부상 지목이 임야이나, 현황 토지임야 상태임. "
    "8) 기타참고사항(임대관례 및 기타) 임대관계는 미상임."
)


class 항목가르기Test(unittest.TestCase):
    def test_알려진_제목으로만_가른다(self):
        got = split_appraisal_summary(구분건물)
        self.assertIn("건물의 구조", got)
        self.assertIn("인접 도로상태등", got)
        self.assertTrue(got["교통상황"].startswith("차량 진출입"))

    def test_양식이_둘이라_토지도_가른다(self):
        got = split_appraisal_summary(토지)
        self.assertIn("형태 및 이용상태", got)
        self.assertIn("제시목록 외의 물건", got)
        self.assertNotIn("건물의 구조", got)


class 사실뽑기Test(unittest.TestCase):
    def test_구분건물에서_사실을_뽑는다(self):
        f = appraisal_facts(구분건물)
        self.assertEqual(f["사용승인일"], "2009-12-30")
        self.assertEqual(f["구조"], "철근콘크리트구조")
        self.assertEqual(f["토지형상"], "세장형")
        self.assertEqual(f["난방"], "도시가스")
        self.assertTrue(f["승강기"])
        self.assertTrue(f["도로"]["포장"])
        self.assertFalse(f["도로"]["맹지"])

    def test_맹지를_잡는다(self):
        self.assertTrue(appraisal_facts(토지)["도로"]["맹지"])

    def test_제시외물건과_공부차이를_잡는다(self):
        f = appraisal_facts(토지)
        self.assertTrue(f["제시외물건"])
        self.assertTrue(f["공부와_차이"])

    def test_비었다는_표기를_내용으로_세지_않는다(self):
        """실측에서 `없 음.`(띄어쓰기!)이 120건으로 1위였고, 그걸 못 걸러
        '공부와의 차이 있음'이 64%로 부풀었다. 고친 뒤 24%."""
        f = appraisal_facts(구분건물)          # 9) 공부와의 차이 = 'ㅡ'
        self.assertFalse(f["공부와_차이"])
        for 빈말 in ("없 음.", "없습니다.", "해당사항 없음.", "-", "-.", "ㅡ", "미상"):
            글 = f"1) 공부와의 차이 {빈말} 2) 기타참고사항 임대 미상임."
            self.assertFalse(appraisal_facts(글)["공부와_차이"], 빈말)

    def test_진짜_내용은_센다(self):
        글 = "1) 공부와의 차이 공부상지목은 '전' 이나, 현황은 '답' 으로 이용중임."
        self.assertTrue(appraisal_facts(글)["공부와_차이"])

    def test_빈_글은_빈_결과(self):
        f = appraisal_facts("")
        self.assertFalse(f["제시외물건"])
        self.assertFalse(f["공부와_차이"])
        self.assertNotIn("구조", f)


if __name__ == "__main__":
    unittest.main()


class 받아두고안꺼내던것Test(unittest.TestCase):
    """옥션원이 한 장에 싣는데 우리는 받아 두고도 안 꺼내던 것들 (G18).

    실측 활성 3,000건 기준 채워지는 비율:
      배당요구종기 100% · 사건접수 100% · 경매개시일 97% · 입찰방법 97% ·
      청구금액 94% · 임차인 42% · 가압류 63% · **지상권 17%**

    **새로 긁지 않는다.** 이미 상세 표에 있던 것을 뽑을 뿐이다.
    """

    상세 = {"tables": [{"caption": "물건기본정보", "rows": [
        ["사건접수", "2024.12.19", "경매개시일", "2024.12.23"],
        ["배당요구종기", "2025.03.06", "청구금액", "206,074,821원"],
    ]}, {"caption": "물건 기본정보", "rows": [
        ["입찰방법", "기일입찰", "물건번호", "1"],
    ]}]}

    def _뽑기(self, parties=None):
        from court_auction_crawler.enrichment import build_case_basics
        return build_case_basics(self.상세, parties)

    def test_표에서_날짜와_금액을_뽑는다(self):
        got = self._뽑기()
        self.assertEqual(got["dividend_deadline"], "2025.03.06")
        self.assertEqual(got["opened_at"], "2024.12.23")
        self.assertEqual(got["filed_at"], "2024.12.19")
        self.assertEqual(got["bid_method"], "기일입찰")
        self.assertEqual(got["claim_amount"], 206074821)

    def test_당사자는_counts_모양이다(self):
        """리스트가 아니라 {'counts': {...}} 다. 모양을 가정하면 전부 False 가 된다."""
        got = self._뽑기({"counts": {"임차인": 1, "가압류권자": 2, "지상권자": 1}})
        self.assertTrue(got["has_tenant"])
        self.assertTrue(got["has_seizure"])
        self.assertTrue(got["has_surface_right"])

    def test_없으면_False(self):
        got = self._뽑기({"counts": {"채권자": 1, "소유자": 1}})
        self.assertFalse(got["has_tenant"])
        self.assertFalse(got["has_seizure"])
        self.assertFalse(got["has_surface_right"])

    def test_당사자가_없어도_안_터진다(self):
        for 값 in (None, {}, [], "이상한값"):
            got = self._뽑기(값)
            self.assertFalse(got["has_tenant"])

    def test_이름은_싣지_않는다(self):
        """있고 없고만 판단에 필요하다. 실명은 마스킹 정책 대상이다."""
        got = self._뽑기({"counts": {"임차인": 1}, "names": ["김철수"]})
        self.assertNotIn("김철수", str(got))


class 미래기일예측Test(unittest.TestCase):
    """다음 기일·최저가 **추정** (G18).

    옥션원은 아직 안 잡힌 3차·4차를 계산해 보여준다. '얼마까지 기다릴까' 를
    판단하는 정보라 값은 크지만 **틀리면 비용도 크다.**

    실측 44,524쌍: 70% 체감이 75% · 80% 체감이 24%인데 **법원마다 갈린다**
    (인천·수원·부산 70% · 서울남부·광주 80%). 0.7 을 일괄 적용하면 전체의 24%에서
    틀린 금액을 사실처럼 보여준다. 기일 간격은 중앙 35일.
    """

    def _예측(self, **kw):
        from court_auction_crawler.enrichment import project_future_sales
        기본 = dict(minimum_bid=254_100_000, sale_date="2026.10.02", reduction_rate=70)
        기본.update(kw)
        return project_future_sales(**기본)

    def test_옥션원_표시값과_일치한다(self):
        """실제 샘플(수원 2025타경56189)의 3차·4차와 맞춘다."""
        got = self._예측(rounds=2)
        self.assertEqual(got[0]["minimum_bid"], 177_870_000)
        self.assertEqual(got[1]["minimum_bid"], 124_509_000)

    def test_정수로_계산한다(self):
        """실수로 하면 254,100,000 × 0.7 이 177,869,999 가 되어 1원씩 어긋난다."""
        self.assertEqual(self._예측(rounds=1)[0]["minimum_bid"] % 1000, 0)

    def test_법원마다_체감률이_다르다(self):
        칠십 = self._예측(reduction_rate=70, rounds=1)[0]["minimum_bid"]
        팔십 = self._예측(reduction_rate=80, rounds=1)[0]["minimum_bid"]
        self.assertNotEqual(칠십, 팔십)
        self.assertEqual(팔십, 203_280_000)

    def test_기일은_35일_간격이다(self):
        got = self._예측(rounds=2)
        self.assertEqual(got[0]["sale_date"], "2026.11.06")
        self.assertEqual(got[1]["sale_date"], "2026.12.11")

    def test_추정임을_반드시_밝힌다(self):
        """법원이 정한 값이 아니다. 화면이 '예상'으로 못 쓰면 거짓이 된다."""
        for 줄 in self._예측(rounds=3):
            self.assertTrue(줄["estimated"])
            self.assertIn("유찰 시", 줄["basis"])

    def test_재료가_없으면_안_만든다(self):
        self.assertEqual(self._예측(minimum_bid=0), [])
        self.assertEqual(self._예측(minimum_bid=None), [])
        self.assertEqual(self._예측(sale_date=""), [])
        self.assertEqual(self._예측(sale_date="날짜아님"), [])


class 점유인추출Test(unittest.TestCase):
    """현황조사서의 점유인(임차인) 내역 (G18).

    **전입일자·확정일자가 대항력 판단의 핵심이다.** 말소기준권리보다 전입이 빠르면
    낙찰자가 보증금을 떠안는다. 옥션원의 '세대열람내역서' 탭도 원본 문서가 아니라
    이것으로 보인다 — 원본은 주민등록법상 열람 자격이 제한된다.

    실측 1,500건: 42%에서 점유인 2,900명. 그중 전입일자 99% · 보증금 71%.
    """

    표 = {"caption": ",점유인,당사자구분,점유부분,용도,점유기간,보증(전세)금,차임,전입일자,확정일자", "rows": [
        ["[소재지] 1. 제주특별자치도 서귀포시"],
        ["1", "점유인", "김미성", "당사자구분", "임차인"],
        ["점유부분", "101동 2층201호", "용도", "주거"],
        ["점유기간", "미상"],
        ["보증(전세)금", "5,000만원", "차임", "미상"],
        ["전입일자", "2023.2.9.", "확정일자", "2023.2.10."],
        ["2", "점유인", "제주올래건설(주) 대표자 고태엽", "당사자구분", "임차인"],
        ["점유부분", "401호", "용도", "기타 - 사무실"],
        ["전입일자", "2017.2.1.", "확정일자", "미상"],
    ]}

    def _문서(self):
        return [{"document_type": "현황조사서",
                 "metadata_json": json.dumps({"tables": [self.표]}, ensure_ascii=False)}]

    def test_점유인을_한_명씩_묶는다(self):
        from court_auction_crawler.enrichment import parse_occupants
        got = parse_occupants(self._문서())
        self.assertEqual(len(got), 2)
        self.assertEqual(got[0]["name"], "김미성")
        self.assertEqual(got[0]["role"], "임차인")
        self.assertEqual(got[0]["전입일자"], "2023.2.9.")
        self.assertEqual(got[0]["확정일자"], "2023.2.10.")
        self.assertEqual(got[0]["보증(전세)금"], "5,000만원")

    def test_다음_사람의_값을_끌어오지_않는다(self):
        from court_auction_crawler.enrichment import parse_occupants
        got = parse_occupants(self._문서())
        self.assertEqual(got[1]["전입일자"], "2017.2.1.")
        self.assertNotEqual(got[1]["전입일자"], got[0]["전입일자"])

    def test_낙찰이_끝나면_이름을_가린다(self):
        """진행 중에는 누가 점유하는지가 입찰 판단에 필요하고, 끝나면 그 필요가
        사라진다. 중지·재매각으로 살아나면 `is_active` 가 1이 되어 다시 보인다."""
        from court_auction_crawler.enrichment import parse_occupants
        got = parse_occupants(self._문서(), mask_names=True)
        self.assertEqual(got[0]["name"], "김○○")
        self.assertEqual(got[0]["전입일자"], "2023.2.9.", "날짜까지 가리면 안 된다")

    def test_법인은_가리지_않는다(self):
        """개인정보가 아니고, 누가 점유하는지가 판단에 걸린다."""
        from court_auction_crawler.enrichment import parse_occupants
        got = parse_occupants(self._문서(), mask_names=True)
        self.assertIn("제주올래건설", got[1]["name"])

    def test_현황조사서가_아니면_안_본다(self):
        from court_auction_crawler.enrichment import parse_occupants
        문서 = [{"document_type": "감정평가서",
                "metadata_json": json.dumps({"tables": [self.표]}, ensure_ascii=False)}]
        self.assertEqual(parse_occupants(문서), [])

    def test_깨진_문서에_안_터진다(self):
        from court_auction_crawler.enrichment import parse_occupants
        for 나쁜값 in (None, [], [{}], [{"document_type": "현황조사서", "metadata_json": "{{"}]):
            self.assertEqual(parse_occupants(나쁜값), [])
