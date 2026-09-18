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
