# 법원 경매 정보 자동 수집 프로그램

YouTube 영상의 흐름처럼 법원경매정보 사이트에서 검색한 물건 목록을 자동으로 읽고 Excel 파일로 저장하는 Python CLI입니다.

공식 사이트: https://www.courtauction.go.kr/

## 설치

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install setuptools wheel
pip install -e .
PLAYWRIGHT_BROWSERS_PATH=.playwright-browsers playwright install chromium
```

만약 `ModuleNotFoundError: No module named 'court_auction_crawler'`가 나오면 현재 터미널에서 아래처럼 실행하세요.

```bash
export PYTHONPATH=src
```

## 가장 안정적인 사용법

사이트 구조나 보안 확인이 바뀔 수 있으므로 처음에는 반자동 모드를 추천합니다.

```bash
PYTHONPATH=src python -m court_auction_crawler.cli collect --headful --output outputs/auction_items.xlsx
```

브라우저가 열리면 법원경매정보 사이트에서 원하는 조건으로 검색합니다. 결과 목록 화면이 보이면 터미널로 돌아와 Enter를 누르세요. 프로그램이 현재 결과 표와 다음 페이지들을 읽어 Excel로 저장합니다.

## 자동 검색 시도

검색 조건을 CLI로 넘겨 사이트 입력을 자동화해볼 수도 있습니다.

```bash
PYTHONPATH=src python -m court_auction_crawler.cli collect \
  --headful \
  --auto-search \
  --court "서울중앙지방법원" \
  --keyword "아파트" \
  --start-date 2026-06-18 \
  --end-date 2026-07-18 \
  --output outputs/auction_items.xlsx
```

사이트의 입력 이름이 바뀌면 자동 검색은 실패할 수 있습니다. 그럴 때는 반자동 모드로 사용하면 됩니다.

## 웹 대시보드로 보기

수집한 물건은 SQLite DB에 누적 저장하고, 웹에서 목록과 상세 정보를 볼 수 있습니다.

```bash
PYTHONPATH=src python -m court_auction_crawler.cli import-html samples/sample_result.html --db data/auction.sqlite3
PYTHONPATH=src python -m court_auction_crawler.cli serve --db data/auction.sqlite3 --host 127.0.0.1 --port 8000
```

브라우저에서 http://127.0.0.1:8000 을 열면 물건 목록, 검색, 상태 필터, 상세 정보, 변경 이력을 볼 수 있습니다.

## API로 조회하기

웹 서버를 실행하면 같은 포트에서 외부 연동용 JSON API도 제공합니다.

```bash
PYTHONPATH=src python -m court_auction_crawler.cli serve --db data/auction.sqlite3 --host 127.0.0.1 --port 8000
```

주요 엔드포인트:

```bash
curl http://127.0.0.1:8000/api/v1/health
curl "http://127.0.0.1:8000/api/v1/auctions?q=아파트&region=서울&active=true&limit=20"
curl "http://127.0.0.1:8000/api/v1/auctions?source=예정&sale_date_from=2026-07-01&sale_date_to=2026-07-31&sort=sale_date_asc"
curl http://127.0.0.1:8000/api/v1/regions
curl http://127.0.0.1:8000/api/v1/stats
curl http://127.0.0.1:8000/api/v1/openapi.json
```

목록 API는 `q`/`query`, `status`, `source`, `region`, `sale_date_from`, `sale_date_to`, `active`, `sort`, `limit`, `offset` 파라미터를 지원합니다. `limit`은 최대 500개까지 반환합니다.

CORS 허용 origin은 환경변수로 바꿀 수 있습니다.

```bash
AUCTION_API_ALLOWED_ORIGINS="http://localhost:3000,http://127.0.0.1:5173" \
  PYTHONPATH=src python -m court_auction_crawler.cli serve --db data/auction.sqlite3
```

## 배포

Python 웹 서비스를 지원하는 Railway, Render, Fly.io 등에 올릴 수 있습니다. 루트의 `Procfile`은 배포 플랫폼에서 아래 명령을 실행합니다.

```bash
PYTHONPATH=src python -m court_auction_crawler.cli serve --db ${AUCTION_DB_PATH:-data/auction.sqlite3} --host 0.0.0.0 --port ${PORT:-8000}
```

운영 환경에서는 SQLite DB가 필요합니다. 빠른 데모는 `data/auction.sqlite3`를 같이 올릴 수 있지만, 갱신 내용을 유지하려면 플랫폼의 persistent volume 또는 별도 DB로 옮기는 편이 좋습니다.

Vercel의 꽁지맵에서 이 API를 쓰려면 `COURT_AUCTION_API_URL`에 배포된 API 주소를 넣고, 이 서버에는 `AUCTION_API_ALLOWED_ORIGINS`로 Vercel 도메인을 허용하세요.

## 자동 동기화와 웹 대시보드 함께 실행

```bash
PYTHONPATH=src python -m court_auction_crawler.cli serve \
  --db data/auction.sqlite3 \
  --watch \
  --interval-minutes 30 \
  --auto-search \
  --court "서울중앙지방법원" \
  --keyword "아파트" \
  --details \
  --headful
```

`--watch`는 지정한 간격으로 새 검색을 실행하고 DB를 갱신합니다. 웹 화면은 5초마다 API를 폴링해 최신 목록과 동기화 상태를 반영합니다.

## 백그라운드 전체 수집

긴 전체 수집은 터미널에 붙어 있을 필요 없이 백그라운드로 실행하세요.

```bash
mkdir -p logs data
PYTHONUNBUFFERED=1 PLAYWRIGHT_BROWSERS_PATH=.playwright-browsers PYTHONPATH=src \
  nohup .venv/bin/python -m court_auction_crawler.cli collect-all \
  --db data/auction.sqlite3 \
  --start-date 2026-06-18 \
  --end-date 2026-07-18 \
  --date-chunk-days 14 \
  --max-pages 50 \
  --delay 2.0 \
  > logs/collect-all.log 2>&1 &
echo $! > data/collect-all.pid
```

웹 서버도 백그라운드로 실행할 수 있습니다.

```bash
PYTHONUNBUFFERED=1 PYTHONPATH=src \
  nohup .venv/bin/python -m court_auction_crawler.cli serve \
  --db data/auction.sqlite3 \
  --host 127.0.0.1 \
  --port 8000 \
  > logs/server.log 2>&1 &
echo $! > data/server.pid
```

진행 확인:

```bash
tail -f logs/collect-all.log
cat data/collect-all.pid
cat data/server.pid
```

## 공시기준가 채우기 (공시지가·공동주택가격·개별주택가격)

지오코딩으로 확보한 PNU를 이용해 물건별 공시기준가를 미리 계산해 DB에 저장합니다. 이렇게 하면 꽁지맵은 런타임 조회 없이 즉시 공시기준가를 표시합니다.

- `geocode-missing`을 실행하면 좌표를 새로 찾는 물건은 그 자리에서 공시기준가도 함께 채웁니다.
- 이미 좌표·PNU가 있는 물건은 아래 백필 커맨드로 채웁니다(재지오코딩 불필요).

```bash
PYTHONPATH=src .venv/bin/python -m court_auction_crawler.cli enrich-prices \
  --db data/auction.sqlite3 \
  --limit 500          # 하루 VWorld API 한도에 맞춰 나눠 실행
```

- 지오코딩과 같은 `VWORLD_API_KEY`를 씁니다(추가 승인 불필요).
- 토지·아파트·빌라·다세대·단독주택을 다룹니다. 오피스텔·상가는 국세청 기준시가(로컬 파일)라 꽁지맵이 자체 인덱스로 처리합니다.
- 조회 실패/미시도만 골라 처리하므로 여러 번 나눠 실행해도 안전합니다. `--limit`로 하루 한도에 맞추세요.

## HTML 파일에서 Excel 만들기

이미 저장해둔 결과 페이지 HTML이 있다면 브라우저 없이 변환할 수 있습니다.

```bash
PYTHONPATH=src python -m court_auction_crawler.cli parse-html samples/result.html --output outputs/auction_items.xlsx
```

## 주의

- 이 도구는 공개 검색 결과를 개인 업무 자동화 목적으로 정리합니다.
- 보안 문자나 접근 제한을 우회하지 않습니다.
- 대량 요청으로 사이트에 부담을 주지 않도록 `--delay` 값을 충분히 두세요.
