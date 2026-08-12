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
curl "http://127.0.0.1:8000/api/v1/auctions/auction%3A서울중앙지방법원%3A2026타경100%3A1"
curl http://127.0.0.1:8000/api/v1/regions
curl http://127.0.0.1:8000/api/v1/stats
curl http://127.0.0.1:8000/api/v1/openapi.json
```

목록 API는 `q`/`query`, `status`, `source`, `region`, `sale_date_from`, `sale_date_to`, `active`, `sort`, `limit`, `offset` 파라미터를 지원합니다. `limit`은 최대 500개까지 반환합니다.

물건 상세 API는 사건·기일·문건/송달 내역, 물건 목록, 감정평가 요약, 사진, 매각물건명세서·현황조사서·감정평가서의 수집 상태를 반환합니다. `assets[].url`은 사진 API, 수집이 끝난 `documents[].url`은 문서 파일 API입니다. 서버 내부 파일 경로는 외부에 공개하지 않습니다.

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

## 전체 상세정보와 법원 문서 수집

목록 수집이 끝난 DB를 사건번호로 다시 조회해 가능한 정보를 모두 채웁니다. 중간에 종료해도 완료 건은 건너뛰고 실패·미공개 문서는 예약된 시각 이후 다시 시도합니다.

```bash
PYTHONUNBUFFERED=1 PLAYWRIGHT_BROWSERS_PATH=.playwright-browsers PYTHONPATH=src \
  .venv/bin/python -m court_auction_crawler.cli collect-details \
  --db data/auction.sqlite3 \
  --asset-dir data/auction-assets \
  --delay 2.0
```

자산 경로를 기본값이 아닌 곳으로 바꾸면 웹 서버에도 같은 절대 경로를 `AUCTION_ASSET_DIR` 환경변수로 지정합니다.

기본 실행은 상세 본문·표·사진과 감정평가서 원본 URL을 저장합니다. 대용량 PDF 원본까지 로컬에 복제하려면 여유 공간을 확인한 뒤 `--download-document-files`를 추가합니다. Cloud Storage 같은 외부 객체 저장소를 붙이기 전에는 전체 3만 건 실행에서 이 옵션을 사용하지 않는 편이 안전합니다.

빠른 검증은 `--limit 10`, 이미 완료한 물건 재수집은 `--force`를 붙입니다. 특정 물건 복구는 `--item-key 'auction:법원:사건번호:물건번호' --force`로 실행합니다. 자동 전체 수집기는 목록 갱신을 마칠 때마다 상세 대상 1,000개를 이어서 처리합니다.

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

## 웹으로 데이터 올리기

수집은 이 맥에서 계속하고, 바뀐 것만 객체 스토리지로 밀어 올립니다. 맥이 꺼져 있어도
웹은 마지막으로 올라간 내용으로 계속 서비스됩니다.

계정 없이 파이프라인 전체를 확인하려면 로컬 디렉터리로 내보냅니다.

```bash
PYTHONPATH=src .venv/bin/python -m court_auction_crawler.cli push-web \
  --db data/auction.sqlite3 \
  --dest local://outputs/web-push \
  --item-limit 200 --asset-limit 200
```

R2로 올릴 때는 `.env`에 `R2_ENDPOINT_URL`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`를
넣고 대상을 버킷으로 바꿉니다(`pip install boto3` 필요).

```bash
PYTHONPATH=src .venv/bin/python -m court_auction_crawler.cli push-web \
  --db data/auction.sqlite3 --dest s3://court-auction
```

올리는 것은 세 가지입니다.

- `v1/snapshot.json.gz` — 지도·목록용 요약(활성+좌표). 매번 통째로 교체합니다.
- `v1/items/<해시>.json` — 물건별 상세(v1 공개 스키마). 페이로드 해시가 바뀐 것만 올립니다.
- `v1/assets/<sha256>.<확장자>` — 사진 원본. 내용 해시가 경로라 같은 사진은 한 번만 올라갑니다.

무엇을 어떤 내용으로 올렸는지는 `web_sync` 테이블이 기억합니다. 중간에 멈춰도 다음 실행이
남은 것부터 이어받습니다. 진행 상황은 `--status`, 실제로 올리지 않고 세어만 보려면
`--dry-run`을 씁니다.

### 자동 푸시

목록 수집 데몬이 사이클을 마칠 때마다 바뀐 것만 올립니다. `scripts/run_collect_loop.sh`가
`--push-dest s3://court-auction`을 넘기고, `.env`에 R2 키가 없으면 조용히 건너뜁니다.
대상을 바꾸려면 `COURT_AUCTION_PUSH_DEST` 환경변수를 쓰세요.

사이클당 올릴 양에 상한이 있습니다(물건 1만, 사진 3만). 스키마가 바뀌어 전량 재업로드가
걸려도 다음 수집이 몇 시간씩 밀리지 않게 하려는 것이고, 상한에 걸려도 몇 사이클이면
따라잡습니다.

**맥은 내보내기만 합니다.** 외부에서 맥으로 들어오는 연결은 없고, 포트를 열거나 고정 IP를
둘 필요도 없습니다. 맥이 꺼져 있어도 웹은 마지막으로 올라간 데이터로 계속 서비스됩니다.

## DB 무결성 점검

아침 상태확인에 넣어 쓰는 커맨드입니다. 손상이 있으면 종료코드 1로 알립니다.

```bash
PYTHONPATH=src .venv/bin/python -m court_auction_crawler.cli db-check --db data/auction.sqlite3
```

인덱스 항목 누락처럼 **인덱스에 한정된 손상**이면 `--repair`로 되살립니다. 인덱스는 테이블
데이터에서 다시 만들어지므로 데이터 손실이 없습니다.

```bash
PYTHONPATH=src .venv/bin/python -m court_auction_crawler.cli db-check --db data/auction.sqlite3 --repair
```

페이지·테이블 손상이 섞여 있으면 자동 복구하지 않고 그대로 보고합니다. 그때는 백업 복구나
`sqlite3 .recover`가 필요합니다.

`quick_check`가 아니라 `integrity_check`를 씁니다. 실측으로, 인덱스 항목이 빠진 손상을
`quick_check`는 ok로 통과시켰고 그 상태로 수집기가 `database disk image is malformed`로
죽었습니다. 1.9G DB에서 1~2초라 아낄 이유가 없습니다.

목록 수집 데몬(`collect-loop`)은 사이클마다 이 점검을 먼저 돌리고, 인덱스 한정 손상이면
스스로 복구한 뒤 수집을 계속합니다.

## HTML 파일에서 Excel 만들기

이미 저장해둔 결과 페이지 HTML이 있다면 브라우저 없이 변환할 수 있습니다.

```bash
PYTHONPATH=src python -m court_auction_crawler.cli parse-html samples/result.html --output outputs/auction_items.xlsx
```

## 주의

- 이 도구는 공개 검색 결과를 개인 업무 자동화 목적으로 정리합니다.
- 보안 문자나 접근 제한을 우회하지 않습니다.
- 대량 요청으로 사이트에 부담을 주지 않도록 `--delay` 값을 충분히 두세요.
