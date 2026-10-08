"""신속통합기획 재개발 구역 경계를 다시 만든다 → `reference/redevelopment_sites.geojson`.

입력은 손으로 관리하는 `reference/redevelopment_sites.tsv` 다. 정보몽땅 화면의 구역 이름은
자치구가 빠지거나('용답동 15일대') 같은 구역이 두 번 나오는 등 들쭉날쭉해서, 대표지번은 사람이
확정해 표에 적는다. 이 스크립트는 화면을 받아 **표에 없는 새 구역을 알려주고**, 표의 구역마다
브이월드에서 경계를 받는다. 서울시가 두세 달마다 후보지를 더하므로 그때 손으로 돌린다.

경계는 이 순서로 고른다(2026-10-08 일회성 조사와 같은 규칙).
  1. 정비구역(UPIS 지구단위계획, 이름에 재개발·정비) 중 표의 이름키가 들어 있고 면적비 0.5~1.45 인 것
  2. 대표지번을 덮는 토지거래허가구역 중 면적이 발표 면적 ±35% 안인 것 — 서울시가 후보지마다 지정한다
  3. 둘 다 없으면 대표점과 면적만 둔다. 웹 푸시는 반경 sqrt(면적/π) 로 '추정' 판정한다

    .venv/bin/python scripts/build_redevelopment_sites.py            # 새 구역 확인 + 경계 다시 받기
    .venv/bin/python scripts/build_redevelopment_sites.py --check    # 새 구역 확인만

`.env` 의 VWORLD_API_KEY·VWORLD_API_DOMAIN 을 읽으므로 원본 폴더에서 돌린다. DB 는 안 건드린다.
"""
from __future__ import annotations

import argparse
import html
import json
import math
import re
import sys
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from court_auction_crawler.geocoder import env_value, geocode_address, ssl_context, vworld_ned_get  # noqa: E402

SEED = ROOT / "reference" / "redevelopment_sites.tsv"
OUT = ROOT / "reference" / "redevelopment_sites.geojson"
PAGE_URL = "https://cleanup.seoul.go.kr/cleanup/view/publicIntgrPlanArea.do"
DATA_URL = "https://api.vworld.kr/req/data"
SEOUL_BOX = "BOX(126.76,37.42,127.19,37.71)"


def read_seed() -> list[dict]:
    sites = []
    for line in SEED.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        selected, name, address, area, key = (line.split("\t") + [""])[:5]
        sites.append({"selected": selected, "site": name, "address": address, "area_m2": float(area), "key": key})
    return sites


def _titles_and_areas(text: str) -> list[tuple[str, float | None]]:
    entries: list[list] = []
    for m in re.finditer(r'<div class="title">(.*?)</div>|구역면적\s*:\s*([\d,.]+)', text, re.S):
        if m.group(1):
            entries.append([re.sub(r"\s+", " ", html.unescape(m.group(1))).strip(), None])
        elif entries:
            try:
                entries[-1][1] = float(m.group(2).replace(",", ""))
            except ValueError:   # '15,.345' 같은 오타
                pass
    return [(title, area) for title, area in entries]


def page_entries() -> list[tuple[str, float | None]]:
    """정보몽땅 화면의 (구역 제목, 면적). 취소된 구역은 뺀다 — 선정 탭에도 그대로 남아 있다."""
    request = Request(PAGE_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=30, context=ssl_context()) as response:
        text = response.read().decode("utf-8", "replace")
    selected, _, cancelled = text.partition('id="area4"')
    cancelled_areas = [area for _, area in _titles_and_areas(cancelled) if area]
    return [(title, area) for title, area in _titles_and_areas(selected)
            if not area or all(abs(area - c) >= 1 for c in cancelled_areas)]


def report_new(sites: list[dict]) -> None:
    def squash(s: str) -> str:
        return re.sub(r"\s|일대|\([^)]*\)", "", s)

    entries = page_entries()
    if not entries:   # 화면 구조가 바뀌면 조용히 '새 구역 없음' 이 된다
        raise RuntimeError("정보몽땅 화면에서 구역을 하나도 못 읽었다 — 화면 구조를 확인한다")
    for title, area in entries:
        known = any((area and abs(area - s["area_m2"]) < 1) or squash(s["site"]) in squash(title) for s in sites)
        if not known:
            print(f"표에 없는 구역: {title} ({area}㎡) — 대표지번을 확인해 {SEED.name} 에 더한다")
    print(f"화면 {len(entries)}건 확인(중복 포함). 표 {len(sites)}곳.")


def vworld_features(params: dict[str, str]) -> list[dict]:
    payload = vworld_ned_get(DATA_URL, {
        "service": "data", "request": "GetFeature", "key": env_value("VWORLD_API_KEY"),
        "format": "json", "geometry": "true", "attribute": "true", "crs": "EPSG:4326", **params,
    }, "response", timeout=30)
    response = payload["response"]
    if response.get("status") == "NOT_FOUND":
        return []
    if response.get("status") != "OK":   # 거절을 '경계 없음' 으로 읽으면 전부 추정으로 떨어진다
        raise RuntimeError(f"브이월드 거절: {response.get('error')}")
    return response["result"]["featureCollection"]["features"]


def upis_redevelopment() -> list[dict]:
    """서울 전역 정비구역 경계(이름에 재개발·정비)."""
    found: dict[str, dict] = {}
    for word in ("재개발", "정비"):
        page = 1
        while True:
            features = vworld_features({"data": "LT_C_UPISUQ161", "geomFilter": SEOUL_BOX,
                                        "attrFilter": f"dgm_nm:like:{word}", "size": "1000", "page": str(page)})
            for f in features:
                found[f["properties"]["present_sn"]] = f
            if len(features) < 1000:
                break
            page += 1
    return list(found.values())


def area_m2(geometry: dict) -> float:
    """경위도 다각형 넓이(㎡). 구역 하나 크기에선 평면 근사로 충분하다."""
    polygons = geometry["coordinates"] if geometry["type"] == "MultiPolygon" else [geometry["coordinates"]]
    total = 0.0
    for polygon in polygons:
        for i, ring in enumerate(polygon):
            kx, ky = 111320 * math.cos(math.radians(ring[0][1])), 110540
            a = abs(sum(x1 * kx * y2 * ky - x2 * kx * y1 * ky
                        for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]))) / 2
            total += a if i == 0 else -a
    return total


def rounded(coords):
    return [rounded(c) for c in coords] if isinstance(coords[0], list) else [round(c, 6) for c in coords]


def build(sites: list[dict]) -> dict:
    upis = upis_redevelopment()
    print(f"정비구역 경계 {len(upis)}개")
    features = []
    for s in sites:
        point = geocode_address(f"서울특별시 {s['address']}")
        if point is None or point.quality != "verified":
            raise RuntimeError(f"대표지번 좌표 실패: {s['address']}")
        geometry, source = None, None
        if s["key"]:
            for f in upis:
                if s["key"] in f["properties"]["dgm_nm"] and 0.5 < float(f["properties"]["dgm_ar"]) / s["area_m2"] < 1.45:
                    geometry, source = f["geometry"], "정비구역"
                    break
        if geometry is None:
            near = vworld_features({"data": "LT_C_UQ141", "geomFilter": f"POINT({point.lng} {point.lat})",
                                    "attrFilter": "uname:like:토지거래", "size": "100"})
            fits = sorted((abs(math.log(area_m2(f["geometry"]) / s["area_m2"])), i) for i, f in enumerate(near))
            if fits and fits[0][0] < math.log(1.35):
                geometry, source = near[fits[0][1]]["geometry"], "토지거래허가구역"
        if geometry is None:
            geometry = {"type": "Point", "coordinates": [point.lng, point.lat]}
        geometry = {"type": geometry["type"], "coordinates": rounded(geometry["coordinates"])}
        features.append({"type": "Feature", "geometry": geometry, "properties": {
            "program": "신속통합기획", "site": s["site"], "selected": s["selected"], "address": s["address"],
            "area_m2": s["area_m2"], "boundary_source": source,
        }})
        print(f"{s['selected']:<16} {s['site']:<20} {source or '경계 없음(반경 추정)'}")
    return {"type": "FeatureCollection", "features": features}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--check", action="store_true", help="새 구역 확인만 하고 경계는 안 받는다")
    args = parser.parse_args()
    sites = read_seed()
    report_new(sites)
    if args.check:
        return
    collection = build(sites)
    OUT.write_text(json.dumps(collection, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    counts: dict[str, int] = {}
    for f in collection["features"]:
        source = f["properties"]["boundary_source"] or "반경 추정"
        counts[source] = counts.get(source, 0) + 1
    print(f"→ {OUT.relative_to(ROOT)} {counts}")


if __name__ == "__main__":
    main()
