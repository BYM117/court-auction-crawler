"""법원 공식 매각통계와 우리 집계를 대조한다. (G11, 세션 C)

우리 검증은 전부 '이전 우리 값'과 비교한다. 처음부터 적게 긁고 있었으면 영원히 모른다.
2026-09-16에 찾은 rowspan 버그가 정확히 그랬다 — 페이지마다 첫 물건이 조용히
사라졌는데 예외도 로그도 없었다. **바깥 기준선이 있어야 조용한 손실이 드러난다.**

    python3 scripts/baseline_compare.py --fetch 202608   # 법원 통계 받아 저장
    python3 scripts/baseline_compare.py 202608           # 대조
    python3 scripts/baseline_compare.py --check          # 자가 점검

받는 쪽은 브라우저가 필요하다. curl은 WAF가 막는다(실측 2026-09-17).
통계는 **다음 달 1일에 확정**되므로 9월치는 10월 1일부터 받을 수 있다.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATS_PATH = ROOT / "data" / "official_stats.json"
DB_PATH = ROOT / "data" / "auction.sqlite3"

STATS_URL = "https://www.courtauction.go.kr/pgj/index.on?w2xPath=/pgj/ui/pgj100/PGJ164M01.xml"

# 페이지 안에서 부르는 JSON API. 화면을 60번 검색하는 것보다 훨씬 싸다.
FETCH_JS = """
async ([codes, ym]) => {
  const out = {};
  for (const c of codes) {
    const r = await fetch('/pgj/pgj164/selectRletCortDspslStats.on', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'SubmissionType': 'ajax'},
      body: JSON.stringify({dma_search: {searchType: '01', cortOfcCd: c.cortOfcCd,
                                         adongSdCd: '', adongSggCd: '',
                                         startDate: ym, endDate: ym}}),
    });
    const j = await r.json();
    const rows = (j.data && j.data.rletCortDspslStats) || [];
    const all = rows.find(x => x.lclDspslGdsLstUsgNm === '전체');
    out[c.cortOfcNm] = all
      ? {auction: all.auctnNum, sold: all.dspslNum,
         appraisal: all.aeeEvlGrsAmt, sold_amount: all.dspslGrsAmt, as_of: all.frstInptDt}
      : null;
    await new Promise(r => setTimeout(r, 250));
  }
  return out;
}
"""


async def fetch(ym: str) -> dict:
    sys.path.insert(0, str(ROOT / "src"))
    from playwright.async_api import async_playwright

    from court_auction_crawler.crawler import _prefer_local_browser_cache

    _prefer_local_browser_cache()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 1000})
        await page.goto(STATS_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(4000)
        codes = await page.evaluate(
            """async () => {
                const r = await fetch('/pgj/pgjComm/selectCortOfcCdLst.on', {
                  method: 'POST',
                  headers: {'Content-Type':'application/json','SubmissionType':'ajax'},
                  body: '{}'});
                return (await r.json()).data.result;
            }"""
        )
        print(f"법원 {len(codes)}개, {ym} 통계 받는 중…")
        stats = await page.evaluate(FETCH_JS, [codes, ym])
        await browser.close()

    got = {k: v for k, v in stats.items() if v}
    STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
    saved = json.loads(STATS_PATH.read_text()) if STATS_PATH.exists() else {}
    saved[ym] = got
    STATS_PATH.write_text(json.dumps(saved, ensure_ascii=False, indent=1))
    print(f"{len(got)}개 법원 저장 → {STATS_PATH}")
    return got


def ours(ym: str) -> dict[str, dict]:
    """우리 쪽 집계. 법원 정의에 맞춰 '변경' 기일은 뺀다(입찰이 없었다)."""
    prefix = f"{ym[:4]}.{ym[4:]}"
    with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT court,
                   COUNT(*)                                        AS auction,
                   SUM(result LIKE '매각%')                        AS sold,
                   SUM(CASE WHEN result LIKE '매각%' THEN sale_amount END) AS sold_amount,
                   COUNT(DISTINCT sale_date)                       AS days
            FROM auction_sale_results
            WHERE sale_date LIKE ? AND result IN ('유찰', '매각')
            GROUP BY court
            """,
            (prefix + "%",),
        ).fetchall()
    return {r["court"]: dict(r) for r in rows}


def coverage_note(ym: str) -> str | None:
    """그 달을 처음부터 봤는가. 못 봤으면 비율 비교 자체가 의미를 잃는다.

    매각결과 화면은 기일 다음날부터 이레만 보여준다. 수집을 달 중간에 시작했으면
    앞부분 기일이 통째로 없고, 그러면 법원마다 '기일이 언제 잡혔는가'가 비율을
    좌우한다. 실측 202608: 깃발 8개가 전부 기일 날짜 분포로 설명됐다.
    """
    with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True) as conn:
        first = conn.execute(
            "SELECT MIN(collected_at) FROM auction_sale_results").fetchone()[0]
    if not first:
        return "기일 데이터가 없다"
    # 이레 창이 있으므로 수집 시작 7일 전 기일까지는 받을 수 있다
    reach = f"{first[:4]}.{first[5:7]}.{max(1, int(first[8:10]) - 7):02d}"
    month_start = f"{ym[:4]}.{ym[4:]}.01"
    if reach > month_start:
        return (f"이 달을 처음부터 보지 못했다 (수집 시작 {first[:10]}, 기일은 {reach}부터).\n"
                f"  비율이 낮은 법원은 대부분 '기일이 그 전에 있었던' 법원이다.\n"
                f"  **깃발을 누락으로 읽지 말 것.** 온전한 달로 봐야 한다.")
    return None


def compare(ym: str) -> None:
    if not STATS_PATH.exists():
        sys.exit(f"{STATS_PATH} 가 없다. 먼저 --fetch {ym}")
    official = json.loads(STATS_PATH.read_text()).get(ym)
    if not official:
        sys.exit(f"{ym} 통계가 없다. 먼저 --fetch {ym}")
    mine = ours(ym)

    rows = []
    for court, off in official.items():
        if not off["auction"]:
            continue                      # 그 달에 기일이 없던 법원
        m = mine.get(court, {})
        rows.append({
            "court": court,
            "off": off["auction"],
            "our": m.get("auction", 0),
            "ratio": m.get("auction", 0) / off["auction"],
            "off_sold": off["sold"],
            "our_sold": m.get("sold", 0) or 0,
            "days": m.get("days", 0),
        })
    if not rows:
        sys.exit("대조할 것이 없다")

    med = statistics.median(r["ratio"] for r in rows)
    tot_off = sum(r["off"] for r in rows)
    tot_our = sum(r["our"] for r in rows)

    print(f"=== {ym} 법원 공식 통계 대조 ({len(rows)}개 법원) ===")
    warn = coverage_note(ym)
    if warn:
        print(f"\n  !! {warn}\n")
    print(f"법원 경매건수 {tot_off:,} / 우리 기일행 {tot_our:,} = {tot_our/tot_off*100:.1f}%")
    print(f"법원별 비율 중앙값 {med*100:.1f}%\n")
    print("  온전한 달이라면 **100~102%가 정상**이다(202608 실측: 기일을 다 받은 법원 5곳이")
    print("  100·101·101·102·98%). 법원 통계가 우리보다 조금 적은 것은 취하 등으로 빠지기")
    print("  때문이다. **95% 아래로 내려가면 그 법원에서 조용히 새고 있다.**\n")

    print(f"{'법원':<16}{'법원통계':>8}{'우리':>8}{'비율':>8}{'중앙값대비':>11}{'기일수':>7}")
    flagged = []
    for r in sorted(rows, key=lambda r: r["ratio"]):
        rel = r["ratio"] / med if med else 0
        mark = ""
        if rel < 0.5:
            mark = "  ← 확인"
            flagged.append(r)
        elif rel > 1.5:
            mark = "  ← 과다"
            flagged.append(r)
        print(f"{r['court']:<16}{r['off']:>8,}{r['our']:>8,}"
              f"{r['ratio']*100:>7.0f}%{rel:>10.2f}x{r['days']:>7}{mark}")

    print(f"\n중앙값에서 크게 벗어난 법원 {len(flagged)}개")
    if flagged:
        print("  " + ", ".join(r["court"] for r in flagged))


def selfcheck() -> None:
    """비율 비교가 '전체가 부족한 것'과 '한 법원만 부족한 것'을 가르는지 본다.

    우리 수집이 늦게 시작해 모든 법원이 절반만 있어도 그건 신호가 아니다.
    남들이 다 50%인데 혼자 10%인 법원이 신호다. 그 구분이 이 도구의 전부다.
    """
    import tempfile

    global STATS_PATH, DB_PATH
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        STATS_PATH = root / "s.json"
        DB_PATH = root / "d.sqlite3"
        # 법원 넷 다 공식 100건. 우리는 셋이 50건(정상적 부분수집), 하나만 5건(진짜 구멍)
        STATS_PATH.write_text(json.dumps({"209901": {
            c: {"auction": 100, "sold": 10, "appraisal": 0, "sold_amount": 0, "as_of": ""}
            for c in ["가법원", "나법원", "다법원", "라법원"]}}, ensure_ascii=False))
        conn = sqlite3.connect(DB_PATH)
        conn.execute("CREATE TABLE auction_sale_results"
                     "(court TEXT, sale_date TEXT, result TEXT, sale_amount INT)")
        for court, n in [("가법원", 50), ("나법원", 50), ("다법원", 50), ("라법원", 5)]:
            conn.executemany(
                "INSERT INTO auction_sale_results VALUES (?, '2099.01.05', '유찰', NULL)",
                [(court,)] * n)
        conn.commit()
        conn.close()

        got = ours("209901")
        assert got["가법원"]["auction"] == 50, got
        ratios = {c: got[c]["auction"] / 100 for c in got}
        med = statistics.median(ratios.values())
        assert abs(med - 0.5) < 1e-9, med                  # 중앙값은 50%
        assert ratios["라법원"] / med < 0.5, ratios        # 라법원만 걸린다
        assert all(ratios[c] / med >= 0.5 for c in ("가법원", "나법원", "다법원")), ratios
    print("자가 점검 통과: 전체 부족은 안 걸리고, 한 법원만 부족하면 걸린다")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("ym", nargs="?", help="대조할 연월 (예: 202608)")
    ap.add_argument("--fetch", metavar="YYYYMM", help="법원 통계를 받아 저장한다")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    if a.check:
        selfcheck()
    elif a.fetch:
        asyncio.run(fetch(a.fetch))
    elif a.ym:
        compare(a.ym)
    else:
        ap.print_help()
