from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import time
import re
from typing import Any

from .building_registry import fetch_building_registry
from .common import RateLimitError, index_problems, self_restart, singleton_lock
from .crawler import collect_all_sync, collect_sync
from .detail_crawler import collect_details_sync
from .transactions import classify_transaction_kind, fetch_transactions
from .web_push import build_uploader, push_once
from .excel import save_items_to_excel
from .geocoder import env_value, geocode_address, is_mappable_property, normalize_auction_address
from .models import SearchOptions
from .official_price import classify_official_kind, fetch_official_price
from .parser import parse_items_from_html
from .store import AuctionStore
from .utils import parse_date
from .web import CollectorControlRunner, WatchRunner, public_auction_summary, run_server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="법원경매정보 검색 결과를 Excel로 저장합니다.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect = subparsers.add_parser("collect", help="브라우저에서 경매 검색 결과를 수집합니다.")
    collect.add_argument("--court", help="자동 검색에 사용할 법원명")
    collect.add_argument("--keyword", help="자동 검색에 사용할 검색어 또는 소재지")
    collect.add_argument("--start-date", type=parse_date, help="매각기일 시작일, 예: 2026-06-18")
    collect.add_argument("--end-date", type=parse_date, help="매각기일 종료일, 예: 2026-07-18")
    collect.add_argument("--auto-search", action="store_true", help="가능하면 검색 조건 입력과 조회를 자동으로 시도합니다.")
    collect.add_argument("--headful", action="store_true", help="브라우저 창을 표시합니다.")
    collect.add_argument("--max-pages", type=int, default=20, help="수집할 최대 페이지 수")
    collect.add_argument("--max-items", type=int, help="수집할 최대 물건 수")
    collect.add_argument("--delay", type=float, default=1.0, help="페이지 사이 대기 시간, 초 단위")
    collect.add_argument("--details", action="store_true", help="DB 저장 후 사건번호로 상세정보와 법원 문서를 수집합니다.")
    collect.add_argument("--db", help="수집 결과를 저장할 SQLite DB 경로")
    collect.add_argument("--output", default="outputs/auction_items.xlsx", help="저장할 Excel 파일 경로")

    parse_html = subparsers.add_parser("parse-html", help="저장된 HTML 파일의 결과 표를 Excel로 변환합니다.")
    parse_html.add_argument("html_file", help="결과 페이지 HTML 파일")
    parse_html.add_argument("--output", default="outputs/auction_items.xlsx", help="저장할 Excel 파일 경로")

    import_html = subparsers.add_parser("import-html", help="저장된 HTML 결과를 웹 대시보드용 DB에 넣습니다.")
    import_html.add_argument("html_file", help="결과 페이지 HTML 파일")
    import_html.add_argument("--db", default="data/auction.sqlite3", help="SQLite DB 경로")

    serve = subparsers.add_parser("serve", help="웹 대시보드를 실행합니다.")
    serve.add_argument("--db", default="data/auction.sqlite3", help="SQLite DB 경로")
    serve.add_argument("--host", default="127.0.0.1", help="서버 호스트")
    serve.add_argument("--port", type=int, default=8000, help="서버 포트")
    serve.add_argument("--watch", action="store_true", help="서버와 함께 자동 동기화를 실행합니다.")
    serve.add_argument("--interval-minutes", type=float, default=30.0, help="자동 동기화 간격")
    serve.add_argument("--court", help="자동 검색에 사용할 법원명")
    serve.add_argument("--keyword", help="자동 검색에 사용할 검색어 또는 소재지")
    serve.add_argument("--start-date", type=parse_date, help="매각기일 시작일")
    serve.add_argument("--end-date", type=parse_date, help="매각기일 종료일")
    serve.add_argument("--auto-search", action="store_true", help="검색 조건 입력과 조회를 자동으로 시도합니다.")
    serve.add_argument("--headful", action="store_true", help="동기화 때 브라우저 창을 표시합니다.")
    serve.add_argument("--details", action="store_true", help="상세 링크가 있으면 상세 페이지 정보도 수집합니다.")
    serve.add_argument("--max-pages", type=int, default=20, help="동기화마다 수집할 최대 페이지 수")
    serve.add_argument("--max-items", type=int, help="동기화마다 수집할 최대 물건 수")
    serve.add_argument("--delay", type=float, default=1.0, help="페이지 사이 대기 시간, 초 단위")

    sync_once = subparsers.add_parser("sync-once", help="한 번 수집해서 DB에 저장합니다.")
    sync_once.add_argument("--db", default="data/auction.sqlite3", help="SQLite DB 경로")
    sync_once.add_argument("--court", help="자동 검색에 사용할 법원명")
    sync_once.add_argument("--keyword", help="자동 검색에 사용할 검색어 또는 소재지")
    sync_once.add_argument("--start-date", type=parse_date, help="매각기일 시작일")
    sync_once.add_argument("--end-date", type=parse_date, help="매각기일 종료일")
    sync_once.add_argument("--auto-search", action="store_true", help="검색 조건 입력과 조회를 자동으로 시도합니다.")
    sync_once.add_argument("--headful", action="store_true", help="브라우저 창을 표시합니다.")
    sync_once.add_argument("--details", action="store_true", help="목록 저장 후 사건번호로 상세정보와 법원 문서를 수집합니다.")
    sync_once.add_argument("--max-pages", type=int, default=20, help="수집할 최대 페이지 수")
    sync_once.add_argument("--max-items", type=int, help="수집할 최대 물건 수")
    sync_once.add_argument("--delay", type=float, default=1.0, help="페이지 사이 대기 시간, 초 단위")

    collect_all = subparsers.add_parser("collect-all", help="전국 법원 물건을 법원/기간 단위로 나눠 전체 수집합니다.")
    collect_all.add_argument("--db", default="data/auction.sqlite3", help="SQLite DB 경로")
    collect_all.add_argument(
        "--mode",
        choices=["current", "scheduled", "both"],
        default="both",
        help="수집 범위: current=현재 진행 물건, scheduled=매각예정물건, both=둘 다",
    )
    collect_all.add_argument("--start-date", type=parse_date, help="매각기일 시작일")
    collect_all.add_argument("--end-date", type=parse_date, help="매각기일 종료일")
    collect_all.add_argument("--days-ahead", type=int, help="end-date가 없을 때 오늘부터 며칠 뒤까지 수집할지")
    collect_all.add_argument("--current-start-date", type=parse_date, help="진행 물건 매각기일 시작일")
    collect_all.add_argument("--current-end-date", type=parse_date, help="진행 물건 매각기일 종료일")
    collect_all.add_argument("--current-days-before", type=int, default=0, help="진행 물건 시작일 기본값: 오늘부터 N일 전")
    collect_all.add_argument(
        "--current-days-ahead",
        type=int,
        default=13,
        help="진행 물건 종료일 기본값: 오늘부터 N일 뒤 (사이트가 2주 후 기일까지만 검색 허용)",
    )
    collect_all.add_argument("--scheduled-start-date", type=parse_date, help="예정 물건 매각기일 시작일")
    collect_all.add_argument("--scheduled-end-date", type=parse_date, help="예정 물건 매각기일 종료일")
    collect_all.add_argument("--scheduled-days-ahead", type=int, default=180, help="예정 물건 종료일 기본값: 오늘부터 N일 뒤")
    collect_all.add_argument("--court", help="특정 법원명만 수집")
    collect_all.add_argument("--court-start", help="지정한 법원명부터 이어서 수집")
    collect_all.add_argument("--court-limit", type=int, help="테스트용으로 앞에서 N개 법원만 수집")
    collect_all.add_argument("--date-chunk-days", type=int, default=31, help="날짜 구간 분할 크기")
    collect_all.add_argument("--headful", action="store_true", help="브라우저 창을 표시합니다.")
    collect_all.add_argument("--details", action="store_true", help="목록 저장 후 사건번호로 상세정보와 법원 문서를 수집합니다.")
    collect_all.add_argument("--detail-limit", type=int, default=0, help="이번 실행의 상세 수집 최대 물건 수, 0이면 전체")
    collect_all.add_argument("--asset-dir", default="data/auction-assets", help="사진과 법원 문서 저장 디렉터리")
    collect_all.add_argument("--skip-documents", action="store_true", help="상세정보만 수집하고 법원 문서는 건너뜁니다.")
    collect_all.add_argument("--download-document-files", action="store_true", help="대용량 법원 문서 원본도 로컬에 저장합니다.")
    collect_all.add_argument("--max-pages", type=int, default=50, help="각 파티션에서 수집할 최대 페이지 수")
    collect_all.add_argument("--max-items", type=int, help="전체 수집할 최대 물건 수")
    collect_all.add_argument("--delay", type=float, default=1.5, help="페이지 사이 대기 시간, 초 단위")
    collect_all.add_argument("--output", help="수집 후 Excel 파일도 저장할 경로")
    collect_all.add_argument(
        "--geocode-limit",
        type=int,
        default=500,
        help="수집 후 좌표 변환할 최대 물건 수, 0이면 지오코딩을 건너뜁니다",
    )

    export_snapshot = subparsers.add_parser(
        "export-snapshot",
        help="활성+좌표 보유 물건을 꽁지맵 스냅샷(JSON, v1 스키마)으로 내보냅니다.",
    )
    export_snapshot.add_argument("--db", default="data/auction.sqlite3", help="SQLite DB 경로")
    export_snapshot.add_argument(
        "--output",
        default="outputs/court-auctions.snapshot.json.gz",
        help="스냅샷 저장 경로(.gz면 gzip 압축, .json이면 평문)",
    )

    collect_loop = subparsers.add_parser(
        "collect-loop",
        help="목록 수집을 주기적으로 반복하는 데몬(collector.enabled 존중, 단일 실행, 자가복구).",
    )
    collect_loop.add_argument("--db", default="data/auction.sqlite3", help="SQLite DB 경로")
    collect_loop.add_argument("--geocode-limit", type=int, default=2000, help="사이클마다 좌표 변환할 최대 물건 수")
    collect_loop.add_argument("--idle-minutes", type=float, default=15.0, help="수집이 꺼져 있을 때 재확인 간격(분)")
    collect_loop.add_argument("--push-dest", default="", help="사이클마다 웹으로 올릴 대상(예: s3://court-auction). 비우면 푸시하지 않습니다.")
    collect_loop.add_argument("--push-item-limit", type=int, default=10000, help="사이클당 올릴 최대 물건 수")
    collect_loop.add_argument("--push-asset-limit", type=int, default=30000, help="사이클당 올릴 최대 사진 수")
    collect_loop.add_argument("--push-concurrency", type=int, default=12, help="동시 업로드 수")

    push_web = subparsers.add_parser(
        "push-web",
        help="바뀐 물건·사진만 웹(객체 스토리지)으로 올립니다. 중단해도 다음 실행이 이어받습니다.",
    )
    push_web.add_argument("--db", default="data/auction.sqlite3", help="SQLite DB 경로")
    push_web.add_argument(
        "--dest",
        default="local://outputs/web-push",
        help="업로드 대상. local://경로 또는 s3://버킷 (R2는 s3://, --endpoint-url 필요)",
    )
    push_web.add_argument("--endpoint-url", default="", help="S3 호환 엔드포인트(R2 계정 엔드포인트)")
    push_web.add_argument("--item-limit", type=int, default=500, help="이번 실행에서 올릴 최대 물건 수")
    push_web.add_argument("--asset-limit", type=int, default=500, help="이번 실행에서 올릴 최대 사진 수")
    push_web.add_argument("--active-only", action="store_true", help="활성 물건만 올립니다.")
    push_web.add_argument("--skip-snapshot", action="store_true", help="스냅샷을 건너뜁니다.")
    push_web.add_argument("--skip-assets", action="store_true", help="사진을 건너뜁니다.")
    push_web.add_argument("--concurrency", type=int, default=12, help="동시 업로드 수(왕복 지연이 커서 이게 속도를 좌우합니다)")
    push_web.add_argument("--dry-run", action="store_true", help="실제로 올리지 않고 대상만 셉니다.")
    push_web.add_argument("--status", action="store_true", help="지금까지 올린 현황만 출력합니다.")

    db_check = subparsers.add_parser(
        "db-check",
        help="DB 무결성을 점검합니다(아침 상태확인용). 인덱스 한정 손상은 --repair로 복구합니다.",
    )
    db_check.add_argument("--db", default="data/auction.sqlite3", help="SQLite DB 경로")
    db_check.add_argument("--repair", action="store_true", help="인덱스 한정 손상이면 REINDEX로 복구합니다.")

    lifecycle = subparsers.add_parser("lifecycle", help="매각기일이 지나고 새 기일이 없는 물건을 종결 처리합니다.")
    lifecycle.add_argument("--db", default="data/auction.sqlite3", help="SQLite DB 경로")
    lifecycle.add_argument("--past-grace-days", type=int, default=3, help="기일 경과 후 종결 처리까지 유예일")
    lifecycle.add_argument("--unseen-days", type=int, default=21, help="기일 없는 물건의 미목격 종결 기준일")

    geocode_missing = subparsers.add_parser("geocode-missing", help="좌표가 없는 DB 물건의 주소를 미리 좌표 변환합니다.")
    geocode_missing.add_argument("--db", default="data/auction.sqlite3", help="SQLite DB 경로")
    geocode_missing.add_argument("--limit", type=int, default=200, help="이번 실행에서 처리할 최대 물건 수")
    geocode_missing.add_argument("--retry-days", type=int, default=7, help="실패했던 주소를 며칠 뒤 재시도할지, 0이면 즉시 재시도")
    geocode_missing.add_argument("--include-inactive", action="store_true", help="종결/비활성 물건도 좌표 변환합니다.")
    geocode_missing.add_argument("--dry-run", action="store_true", help="DB 저장 없이 변환 가능 여부만 출력합니다.")
    geocode_missing.add_argument("--quiet", action="store_true", help="개별 물건 로그를 출력하지 않습니다.")

    enrich_prices = subparsers.add_parser("enrich-prices", help="PNU가 있는 물건의 공시기준가(공시지가/공동주택가격/개별주택가격)를 채웁니다.")
    enrich_prices.add_argument("--db", default="data/auction.sqlite3", help="SQLite DB 경로")
    enrich_prices.add_argument("--limit", type=int, default=500, help="이번 실행에서 처리할 최대 물건 수(하루 API 한도에 맞춰 나눠 실행)")
    enrich_prices.add_argument("--retry-days", type=int, default=14, help="조회 실패한 물건을 며칠 뒤 재시도할지")
    enrich_prices.add_argument("--include-inactive", action="store_true", help="종결/비활성 물건도 채웁니다.")
    enrich_prices.add_argument("--quiet", action="store_true", help="개별 물건 로그를 출력하지 않습니다.")

    enrich_buildings = subparsers.add_parser("enrich-buildings", help="PNU가 있는 물건의 건축물대장(대지/건폐율/용적률/구조/사용승인)을 채웁니다.")
    enrich_buildings.add_argument("--db", default="data/auction.sqlite3", help="SQLite DB 경로")
    enrich_buildings.add_argument("--limit", type=int, default=500, help="이번 실행에서 처리할 최대 물건 수")
    enrich_buildings.add_argument("--retry-days", type=int, default=30, help="조회 실패한 물건을 며칠 뒤 재시도할지")
    enrich_buildings.add_argument("--include-inactive", action="store_true", help="종결/비활성 물건도 채웁니다.")
    enrich_buildings.add_argument("--quiet", action="store_true", help="개별 물건 로그를 출력하지 않습니다.")

    enrich_transactions = subparsers.add_parser("enrich-transactions", help="PNU가 있는 물건의 국토부 실거래가(매매·전월세)를 채웁니다.")
    enrich_transactions.add_argument("--db", default="data/auction.sqlite3", help="SQLite DB 경로")
    enrich_transactions.add_argument("--limit", type=int, default=150, help="이번 실행에서 처리할 최대 물건 수(물건당 최대 12회 호출)")
    enrich_transactions.add_argument("--retry-days", type=int, default=30, help="조회 실패한 물건을 며칠 뒤 재시도할지")
    enrich_transactions.add_argument("--include-inactive", action="store_true", help="종결/비활성 물건도 채웁니다.")
    enrich_transactions.add_argument("--quiet", action="store_true", help="개별 물건 로그를 출력하지 않습니다.")

    collect_details = subparsers.add_parser(
        "collect-details",
        help="DB의 모든 물건을 사건번호로 다시 조회해 상세정보와 법원 문서를 수집합니다.",
    )
    collect_details.add_argument("--db", default="data/auction.sqlite3", help="SQLite DB 경로")
    collect_details.add_argument("--limit", type=int, default=0, help="이번 실행의 최대 물건 수, 0이면 전체")
    collect_details.add_argument("--include-inactive", action="store_true", help="종결/비활성 물건도 수집합니다.")
    collect_details.add_argument("--force", action="store_true", help="이미 완료한 물건도 다시 수집합니다.")
    collect_details.add_argument("--item-key", default="", help="특정 DB 물건 키 하나만 수집합니다.")
    collect_details.add_argument("--headful", action="store_true", help="브라우저 창을 표시합니다.")
    collect_details.add_argument("--delay", type=float, default=1.5, help="사건 사이 대기 시간, 초 단위")
    collect_details.add_argument("--workers", type=int, default=3, help="동시에 사건을 처리할 브라우저 컨텍스트 수")
    collect_details.add_argument(
        "--loop",
        action="store_true",
        help="한 패스가 끝나도 종료하지 않고 신건·재시도 도래분을 계속 수집합니다.",
    )
    collect_details.add_argument(
        "--idle-minutes",
        type=float,
        default=15.0,
        help="--loop에서 처리할 대상이 없을 때 다음 확인까지 대기 시간(분)",
    )
    collect_details.add_argument("--asset-dir", default="data/auction-assets", help="사진과 법원 문서 저장 디렉터리")
    collect_details.add_argument("--skip-documents", action="store_true", help="상세정보만 수집하고 법원 문서는 건너뜁니다.")
    collect_details.add_argument("--download-document-files", action="store_true", help="대용량 법원 문서 원본도 로컬에 저장합니다.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "collect":
        options = SearchOptions(
            court=args.court,
            keyword=args.keyword,
            start_date=args.start_date,
            end_date=args.end_date,
            auto_search=args.auto_search,
            max_pages=args.max_pages,
            max_items=args.max_items,
            delay=args.delay,
            headful=args.headful,
            collect_details=False,
        )
        items = collect_sync(options)
        if args.db:
            store = AuctionStore(args.db)
            result = store.upsert_items(items)
            print(f"DB 저장: 신규 {result.inserted}개, 변경 {result.updated}개, 동일 {result.unchanged}개")
            if args.details and items:
                # limit=0이 '전체 백로그'로 해석되지 않도록 수집분이 있을 때만 실행한다.
                summary = collect_details_sync(store, limit=len(items), delay=args.delay)
                print(f"상세 수집: 완료 {summary.collected}개, 실패 {summary.failed}개")
        elif args.details:
            raise ValueError("--details를 사용하려면 --db 경로가 필요합니다.")
        output = save_items_to_excel(items, args.output)
        print(f"{len(items)}개 물건을 저장했습니다: {output}")
        return 0

    if args.command == "parse-html":
        html = Path(args.html_file).read_text(encoding="utf-8")
        items = parse_items_from_html(html)
        output = save_items_to_excel(items, args.output)
        print(f"{len(items)}개 물건을 저장했습니다: {output}")
        return 0

    if args.command == "import-html":
        html = Path(args.html_file).read_text(encoding="utf-8")
        items = parse_items_from_html(html)
        result = AuctionStore(args.db).upsert_items(items)
        print(
            f"{len(items)}개 물건을 DB에 반영했습니다: "
            f"신규 {result.inserted}개, 변경 {result.updated}개, 동일 {result.unchanged}개"
        )
        return 0

    if args.command == "sync-once":
        options = _search_options_from_args(args)
        items = collect_sync(options)
        store = AuctionStore(args.db)
        result = store.upsert_items(items)
        print(
            f"{len(items)}개 물건을 DB에 반영했습니다: "
            f"신규 {result.inserted}개, 변경 {result.updated}개, 동일 {result.unchanged}개"
        )
        if args.details and items:
            summary = collect_details_sync(store, limit=len(items), delay=args.delay)
            print(f"상세 수집: 완료 {summary.collected}개, 실패 {summary.failed}개")
        return 0

    if args.command == "serve":
        store = AuctionStore(args.db)
        runner = None
        if args.watch:
            runner = WatchRunner(
                store,
                _search_options_from_args(args),
                interval_seconds=int(args.interval_minutes * 60),
                collect_details=args.details,
            )
        run_server(store, args.host, args.port, runner)
        return 0

    if args.command == "collect-all":
        today = date.today()
        start = args.start_date
        end = args.end_date
        if args.days_ahead is not None:
            start = start or today
            end = end or start + timedelta(days=args.days_ahead)
        current_start = args.current_start_date or start or today - timedelta(days=args.current_days_before)
        current_end = args.current_end_date or end or today + timedelta(days=args.current_days_ahead)
        scheduled_start = args.scheduled_start_date or start or today
        scheduled_end = args.scheduled_end_date or end or today + timedelta(days=args.scheduled_days_ahead)
        options = SearchOptions(
            court=args.court,
            start_date=start,
            end_date=end,
            auto_search=True,
            max_pages=args.max_pages,
            max_items=args.max_items,
            delay=args.delay,
            headful=args.headful,
            collect_details=False,
            court_limit=args.court_limit,
            court_start=args.court_start,
            date_chunk_days=args.date_chunk_days,
            collection_mode=args.mode,
            current_start_date=current_start,
            current_end_date=current_end,
            scheduled_start_date=scheduled_start,
            scheduled_end_date=scheduled_end,
        )
        store = AuctionStore(args.db)
        cycle = run_collect_cycle(store, options, geocode_limit=args.geocode_limit)
        items = cycle["items"]
        if args.details:
            detail_summary = collect_details_sync(
                store,
                limit=args.detail_limit or None,
                asset_dir=args.asset_dir,
                delay=args.delay,
                headful=args.headful,
                collect_documents=not args.skip_documents,
                download_document_files=args.download_document_files,
            )
            print(
                f"상세 수집: 대상 {detail_summary.targets}개, 완료 {detail_summary.collected}개, "
                f"실패 {detail_summary.failed}개, 조회불가 {detail_summary.unavailable}개, "
                f"문서 {detail_summary.documents_collected}개, 문서 대기 {detail_summary.documents_pending}개"
            )
        if args.output:
            output = save_items_to_excel(items, args.output)
            print(f"Excel 저장: {output}")
        return 0

    if args.command == "push-web":
        return run_push_web(args)

    if args.command == "db-check":
        return run_db_check(AuctionStore(args.db), repair=args.repair)

    if args.command == "collect-loop":
        return run_collect_loop(
            AuctionStore(args.db),
            geocode_limit=args.geocode_limit,
            idle_minutes=args.idle_minutes,
            push_dest=args.push_dest,
            push_item_limit=args.push_item_limit,
            push_asset_limit=args.push_asset_limit,
            push_concurrency=args.push_concurrency,
        )

    if args.command == "collect-details":
        store = AuctionStore(args.db)
        while True:
            summary = collect_details_sync(
                store,
                limit=args.limit or None,
                include_inactive=args.include_inactive,
                force=args.force,
                item_key=args.item_key,
                asset_dir=args.asset_dir,
                delay=args.delay,
                headful=args.headful,
                collect_documents=not args.skip_documents,
                download_document_files=args.download_document_files,
                workers=args.workers,
            )
            print(
                f"상세 수집 완료: 대상 {summary.targets}개, 사건 {summary.cases}건, "
                f"완료 {summary.collected}개, 실패 {summary.failed}개, 조회불가 {summary.unavailable}개, "
                f"문서 {summary.documents_collected}개, 문서 대기 {summary.documents_pending}개"
            )
            if not args.loop:
                return 0
            # 자가 복구로 중단된 패스는 새 브라우저로 곧바로 재개하고,
            # 대상을 처리했으면 그 사이 쌓인 신건을 바로 다시 확인하고,
            # 비어 있었으면 idle 간격만큼 쉬었다가 재시도 도래분을 확인한다.
            if summary.aborted:
                wait_minutes = 0.5
            elif summary.targets:
                wait_minutes = 1.0
            else:
                wait_minutes = args.idle_minutes
            print(f"다음 패스까지 {wait_minutes:g}분 대기 (--loop)")
            time.sleep(wait_minutes * 60)

    if args.command == "export-snapshot":
        store = AuctionStore(args.db)
        payload = build_snapshot_payload(store)
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(payload, ensure_ascii=False)
        # 스냅샷이 100MB에 육박해 GitHub 한도에 걸리므로 .gz 확장자면 gzip으로 저장한다.
        if output.suffix == ".gz":
            import gzip

            output.write_bytes(gzip.compress(data.encode("utf-8"), compresslevel=9))
        else:
            output.write_text(data, encoding="utf-8")
        print(f"스냅샷 저장: {output} (물건 {payload['total']}개, 생성 {payload['generated_at']})")
        return 0

    if args.command == "lifecycle":
        result = AuctionStore(args.db).apply_lifecycle(
            past_grace_days=args.past_grace_days,
            unseen_no_date_days=args.unseen_days,
        )
        print(f"생명주기 정리: 활성 {result['checked']}개 중 {result['deactivated']}개 종결 처리")
        return 0

    if args.command == "geocode-missing":
        store = AuctionStore(args.db)
        result = run_geocode_missing(
            store,
            limit=args.limit,
            include_inactive=args.include_inactive,
            dry_run=args.dry_run,
            quiet=args.quiet,
            retry_failed_after_days=args.retry_days,
        )
        if result.get("no_key"):
            print("VWORLD_API_KEY가 없어 지오코딩을 실행할 수 없습니다 (.env 또는 환경변수).")
            return 1
        print(
            f"좌표 변환 완료: 성공 {result['updated']}개, 실패 {result['missing']}개, "
            f"대상외 {result['excluded']}개, 대상 {result['targets']}개"
        )
        return 0

    if args.command == "enrich-prices":
        store = AuctionStore(args.db)
        result = run_enrich_prices(
            store,
            limit=args.limit,
            include_inactive=args.include_inactive,
            retry_days=args.retry_days,
            quiet=args.quiet,
        )
        if result.get("no_key"):
            print("VWORLD_API_KEY가 없어 공시기준가 조회를 실행할 수 없습니다 (.env 또는 환경변수).")
            return 1
        counts = result.get("counts", {})
        print(
            f"공시기준가 채움: 매칭 {result['priced']}개 / 대상 {result['targets']}개 "
            f"(실패 {counts.get('price_miss', 0)}, 대상외 {counts.get('skip', 0)})"
        )
        return 0

    if args.command == "enrich-buildings":
        store = AuctionStore(args.db)
        result = run_enrich_buildings(
            store, limit=args.limit, include_inactive=args.include_inactive,
            retry_days=args.retry_days, quiet=args.quiet,
        )
        if result.get("no_key"):
            print("PUBLIC_DATA_SERVICE_KEY가 없어 건축물대장 조회를 실행할 수 없습니다 (.env).")
            return 1
        counts = result.get("counts", {})
        print(
            f"건축물대장 채움: 확보 {result['ok']}개 / 대상 {result['targets']}개 "
            f"(없음 {counts.get('miss', 0)}, 오류 {counts.get('error', 0)})"
        )
        return 0

    if args.command == "enrich-transactions":
        store = AuctionStore(args.db)
        result = run_enrich_transactions(
            store, limit=args.limit, include_inactive=args.include_inactive,
            retry_days=args.retry_days, quiet=args.quiet,
        )
        if result.get("no_key"):
            print("PUBLIC_DATA_SERVICE_KEY가 없어 실거래가 조회를 실행할 수 없습니다 (.env).")
            return 1
        counts = result.get("counts", {})
        print(
            f"실거래가 채움: 확보 {result['ok']}개 / 대상 {result['targets']}개 "
            f"(없음 {counts.get('miss', 0)}, 미등록 {counts.get('unregistered', 0)}, 오류 {counts.get('error', 0)})"
        )
        return 0

    raise ValueError(f"지원하지 않는 명령입니다: {args.command}")


def run_collect_cycle(
    store: AuctionStore,
    options: SearchOptions,
    *,
    geocode_limit: int = 2000,
    price_limit: int = 500,
    coverage_min_days: int = 120,
) -> dict[str, Any]:
    """목록 수집 한 사이클: 수집→커버리지 기록→생명주기→지오코딩→공시기준가.
    collect-all(일회성)과 collect-loop(데몬)이 공유한다."""
    totals = {"inserted": 0, "updated": 0, "unchanged": 0}
    court_counts: dict[tuple[str, str], int] = {}

    def save_partition(partition, partition_items):
        result = store.upsert_items(partition_items)
        totals["inserted"] += result.inserted
        totals["updated"] += result.updated
        totals["unchanged"] += result.unchanged
        key = (partition.source_mode or "current", partition.court)
        court_counts[key] = court_counts.get(key, 0) + len(partition_items)
        print(
            f"  -> {len(partition_items)}개 반영 "
            f"(신규 {result.inserted}, 변경 {result.updated}, 동일 {result.unchanged})"
        )

    items = collect_all_sync(options, on_partition=save_partition)
    print(
        f"{len(items)}개 물건 수집 완료: "
        f"신규 {totals['inserted']}개, 변경 {totals['updated']}개, 동일 {totals['unchanged']}개"
    )
    # 커버리지 감시는 완주한 전체 수집(예정 윈도우가 넓은 실행)끼리만 비교해야 의미가 있다.
    scheduled_span = 0
    if options.scheduled_start_date and options.scheduled_end_date:
        scheduled_span = (options.scheduled_end_date - options.scheduled_start_date).days
    if scheduled_span >= coverage_min_days:
        coverage_warnings = store.record_court_stats(court_counts)
        print(f"법원별 수집 기록 저장: {len(court_counts)}개 (법원×구분)")
        for warning in coverage_warnings:
            print(f"!! 커버리지 경고: {warning}")
    lifecycle = store.apply_lifecycle()
    print(f"생명주기 정리: 활성 {lifecycle['checked']}개 중 {lifecycle['deactivated']}개 종결 처리")
    if geocode_limit > 0:
        geocoded = run_geocode_missing(store, limit=geocode_limit, quiet=True)
        if geocoded.get("no_key"):
            print("지오코딩 건너뜀: VWORLD_API_KEY가 없습니다 (.env 또는 환경변수).")
        else:
            print(
                f"지오코딩: 성공 {geocoded['updated']}개, 실패 {geocoded['missing']}개, "
                f"대상외 {geocoded['excluded']}개, 대상 {geocoded['targets']}개"
            )
    if price_limit > 0:
        # 지오코딩으로 새로 PNU를 확보한 물건의 공시기준가를 이어서 채운다.
        priced = run_enrich_prices(store, limit=price_limit, quiet=True)
        if priced.get("no_key"):
            print("공시기준가 건너뜀: VWORLD_API_KEY가 없습니다.")
        else:
            print(f"공시기준가: 매칭 {priced['priced']}개, 대상 {priced['targets']}개")
        # 건축물대장·실거래가(공공데이터포털)도 같은 PNU로 이어서 채운다.
        buildings = run_enrich_buildings(store, limit=price_limit, quiet=True)
        if not buildings.get("no_key"):
            print(f"건축물대장: 확보 {buildings['ok']}개, 대상 {buildings['targets']}개")
        deals = run_enrich_transactions(store, limit=max(price_limit // 3, 50), quiet=True)
        if not deals.get("no_key"):
            print(f"실거래가: 확보 {deals['ok']}개, 대상 {deals['targets']}개")
    return {"items": items, "totals": totals}


def build_push_uploader(dest: str, endpoint_url: str = "") -> tuple[Any, str]:
    """푸시 대상 업로더를 만든다. 자격증명은 .env에서 읽어 명령줄에 노출하지 않는다.
    준비가 안 됐으면 (None, 사유)를 돌려주고, 호출부가 조용히 건너뛴다."""
    if not dest:
        return None, "푸시 대상이 지정되지 않았습니다."
    credentials: dict[str, Any] = {}
    if dest.startswith(("s3://", "r2://")):
        credentials = {
            "endpoint_url": endpoint_url or env_value("R2_ENDPOINT_URL"),
            "access_key": env_value("R2_ACCESS_KEY_ID"),
            "secret_key": env_value("R2_SECRET_ACCESS_KEY"),
        }
        if not all(credentials.values()):
            return None, "R2 자격증명이 없습니다 (.env의 R2_ENDPOINT_URL, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY)."
    try:
        return build_uploader(dest, **credentials), ""
    except Exception as exc:  # noqa: BLE001
        return None, f"업로더 생성 실패: {str(exc)[:150]}"


def run_push_cycle(
    store: AuctionStore,
    dest: str,
    *,
    item_limit: int,
    asset_limit: int,
    concurrency: int,
    endpoint_url: str = "",
) -> None:
    """수집 사이클 끝에 붙는 웹 푸시. 실패해도 수집을 멈추지 않는다.

    한 사이클에서 올릴 양에 상한을 둔다. 스키마가 바뀌어 전량 재업로드가 걸리면
    상한 없이는 다음 수집이 몇 시간 밀린다. 상한을 둬도 몇 사이클이면 다 따라잡는다."""
    uploader, reason = build_push_uploader(dest, endpoint_url)
    if uploader is None:
        print(f"웹 푸시 건너뜀: {reason}", flush=True)
        return
    started = time.time()
    summary = push_once(
        store,
        uploader,
        item_limit=item_limit,
        asset_limit=asset_limit,
        concurrency=concurrency,
    )
    print(
        f"웹 푸시: 물건 {summary.items_pushed}건, 사진 {summary.assets_pushed}건, "
        f"{summary.bytes_pushed / 1024 / 1024:.1f}MB, {time.time() - started:.0f}초",
        flush=True,
    )
    for line in summary.errors[:5]:
        print(f"  !! 푸시 실패: {line}", flush=True)


def run_push_web(args: Any) -> int:
    """바뀐 것만 웹으로 올린다. 자격증명은 .env에서 읽어 명령줄에 노출하지 않는다."""
    store = AuctionStore(args.db)
    if args.status:
        stats = store.web_sync_stats()
        pushed = stats["pushed"]
        for kind in ("snapshot", "item", "asset"):
            entry = pushed.get(kind, {"count": 0, "bytes": 0})
            total = {"item": stats["items_total"], "asset": stats["assets_total"]}.get(kind)
            scope = f" / 전체 {total}" if total else ""
            print(f"{kind}: {entry['count']}건{scope}, {entry['bytes'] / 1024 / 1024:.1f}MB")
        print(f"마지막 푸시: {stats['last_pushed_at'] or '없음'}")
        return 0

    uploader, reason = build_push_uploader(args.dest, args.endpoint_url)
    if uploader is None:
        print(reason)
        return 1
    started = time.time()

    def on_progress(kind: str, index: int, total: int) -> None:
        if total and (index % 100 == 0 or index == total):
            print(f"  {kind} {index}/{total}", flush=True)

    summary = push_once(
        store,
        uploader,
        item_limit=args.item_limit,
        asset_limit=args.asset_limit,
        include_inactive=not args.active_only,
        skip_snapshot=args.skip_snapshot,
        skip_assets=args.skip_assets,
        dry_run=args.dry_run,
        concurrency=args.concurrency,
        on_progress=on_progress,
    )
    elapsed = time.time() - started
    mode = "(모의 실행)" if args.dry_run else ""
    print(
        f"웹 푸시 완료{mode}: 물건 {summary.items_pushed}건 올림 / {summary.items_skipped}건 변화없음, "
        f"사진 {summary.assets_pushed}건, {summary.bytes_pushed / 1024 / 1024:.1f}MB, {elapsed:.1f}초",
        flush=True,
    )
    if summary.errors:
        print(f"!! 실패 {len(summary.errors)}건:", flush=True)
        for line in summary.errors[:10]:
            print(f"   - {line}", flush=True)
    return 1 if summary.errors else 0


def run_db_check(store: AuctionStore, *, repair: bool = False) -> int:
    """DB 무결성을 점검하고, 인덱스 한정 손상이면(그리고 repair면) REINDEX로 되살린다.

    반환값은 종료 코드다. 0=정상, 1=손상이 남음. 아침 상태확인과 수집 데몬이 공유한다."""
    problems = store.integrity_check()
    if not problems:
        print("DB 무결성: 정상(integrity_check ok)", flush=True)
        return 0

    print(f"!! DB 손상 감지 {len(problems)}건:", flush=True)
    for line in problems[:20]:
        print(f"   - {line}", flush=True)
    if len(problems) > 20:
        print(f"   ... 외 {len(problems) - 20}건", flush=True)

    names = index_problems(problems)
    if names is None:
        print(
            "!! 인덱스 밖(페이지·테이블) 손상이라 자동 복구하지 않는다. "
            "백업 복구나 .recover가 필요하다.",
            flush=True,
        )
        return 1
    if not repair:
        print(f"   인덱스 한정 손상이다. --repair로 복구 가능: {', '.join(names)}", flush=True)
        return 1

    print(f"== 인덱스 재생성: {', '.join(names)}", flush=True)
    store.repair_indexes(names)
    remaining = store.integrity_check()
    if remaining:
        print(f"!! 복구 후에도 손상이 남았다 {len(remaining)}건", flush=True)
        return 1
    print("== 복구 완료(integrity_check ok)", flush=True)
    return 0


def run_collect_loop(
    store: AuctionStore,
    *,
    geocode_limit: int = 2000,
    idle_minutes: float = 15.0,
    max_consecutive_failures: int = 3,
    push_dest: str = "",
    push_item_limit: int = 10_000,
    push_asset_limit: int = 30_000,
    push_concurrency: int = 12,
) -> int:
    """목록 수집 상시 데몬. collector.enabled가 켜져 있을 때만 수집하고,
    3시간(quick)/24시간(full) 주기를 자동 판단한다. 연속 실패가 쌓이면 프로세스를
    종료해 launchd가 깨끗하게 되살린다(맥 잠자기 후 좀비 방어)."""
    controller = CollectorControlRunner(store)
    with singleton_lock(store.db_path.parent / "collect-all.pid") as acquired:
        if not acquired:
            print("목록 수집 데몬이 이미 실행 중이라 종료합니다 (data/collect-all.pid).", flush=True)
            return 0
        consecutive_failures = 0
        while True:
            if not controller.enabled:
                print(f"===== 자동 수집 꺼짐 - {idle_minutes:g}분 후 재확인 =====", flush=True)
                time.sleep(idle_minutes * 60)
                continue

            # 사이클마다 무결성을 먼저 본다. 인덱스 손상을 방치하면 수집 도중
            # 'database disk image is malformed'로 죽고 launchd 재시작만 반복한다(실측).
            # 1.9G DB에서 1~2초라 사이클 비용에 묻힌다. 점검 자체가 실패해도 수집은 계속한다.
            try:
                run_db_check(store, repair=True)
            except Exception as exc:  # noqa: BLE001
                print(f"!! DB 무결성 점검 건너뜀: {str(exc)[:150]}", flush=True)

            run_kind = controller.next_run_kind()
            window = controller.collection_window(run_kind)
            print(
                f"===== 자동 수집 시작 {time.strftime('%Y-%m-%d %H:%M:%S')} "
                f"mode={run_kind} current={window['current_start']}~{window['current_end']} "
                f"scheduled={window['scheduled_start']}~{window['scheduled_end']} =====",
                flush=True,
            )
            options = SearchOptions(
                auto_search=True,
                max_pages=50,
                delay=2.0,
                date_chunk_days=31,
                collection_mode="both",
                current_start_date=parse_date(window["current_start"]),
                current_end_date=parse_date(window["current_end"]),
                scheduled_start_date=parse_date(window["scheduled_start"]),
                scheduled_end_date=parse_date(window["scheduled_end"]),
            )
            try:
                run_collect_cycle(store, options, geocode_limit=geocode_limit)
                consecutive_failures = 0
                if run_kind == "full":
                    controller.record_full_run()
                if push_dest:
                    # 수집·보강이 끝난 뒤에 올려야 이번 사이클의 변경분이 함께 나간다.
                    # 푸시가 실패해도 수집 사이클은 성공으로 친다(수집과 배포는 별개다).
                    try:
                        run_push_cycle(
                            store,
                            push_dest,
                            item_limit=push_item_limit,
                            asset_limit=push_asset_limit,
                            concurrency=push_concurrency,
                        )
                    except Exception as exc:  # noqa: BLE001
                        print(f"!! 웹 푸시 실패(수집은 정상): {str(exc)[:200]}", flush=True)
            except Exception as exc:  # noqa: BLE001 - 데몬은 어떤 실패에도 죽지 않고 자가복구
                consecutive_failures += 1
                print(f"===== 자동 수집 실패({consecutive_failures}/{max_consecutive_failures}): {exc} =====", flush=True)
                if consecutive_failures >= max_consecutive_failures:
                    self_restart("===== 연속 실패 지속 -> 프로세스 종료(launchd 재시작) =====")

            interval = controller.interval_seconds
            print(
                f"===== 자동 수집 종료 {time.strftime('%Y-%m-%d %H:%M:%S')} exit=0; "
                f"{interval}초 후 재시작 =====",
                flush=True,
            )
            print(f"===== 다음 자동 수집까지 {interval}초 대기 =====", flush=True)
            waited = 0
            while waited < interval and controller.enabled:
                time.sleep(min(30, interval - waited))
                waited += 30


def build_snapshot_payload(store: AuctionStore) -> dict[str, Any]:
    """꽁지맵이 읽는 스냅샷을 v1 공개 스키마로 만든다.

    활성이고 좌표가 있는 물건만 담고, 차량·선박처럼 지도 대상이 아닌 물건은
    좌표가 남아 있어도 제외한다(필터 도입 전에 지오코딩된 것들).
    """
    items: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = store.list_items(active=True, require_coordinates=True, limit=500, offset=offset)
        rows = page["items"]
        if not rows:
            break
        for row in rows:
            if not is_mappable_property(row.get("address", ""), row.get("category", "")):
                continue
            items.append(public_auction_summary(row))
        offset += len(rows)
        if offset >= page["total"]:
            break
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total": len(items),
        "items": items,
    }


def run_geocode_missing(
    store: AuctionStore,
    *,
    limit: int = 200,
    include_inactive: bool = False,
    dry_run: bool = False,
    quiet: bool = False,
    retry_failed_after_days: int = 7,
) -> dict[str, Any]:
    """좌표 없는 활성 물건을 지오코딩한다. collect-all 마무리와 geocode-missing 커맨드가 공유."""
    if not (env_value("VWORLD_API_KEY") or env_value("PUBLIC_DATA_SERVICE_KEY")):
        # 키가 없는데 실행하면 전 대상이 실패로 기록돼 재시도 유예에 걸린다. 아무것도 건드리지 않는다.
        return {"no_key": True, "targets": 0, "updated": 0, "missing": 0, "excluded": 0}

    active = None if include_inactive else True
    rows = store.list_missing_coordinates(
        limit=limit,
        active=active,
        retry_failed_after_days=retry_failed_after_days,
    )
    updated = 0
    missing = 0
    excluded = 0
    for index, row in enumerate(rows, start=1):
        address = row.get("address", "")
        if not is_mappable_property(address, row.get("category", "")):
            excluded += 1
            if not dry_run:
                store.mark_coordinate_missing(
                    row["item_key"],
                    normalized_address=normalize_auction_address(address),
                    quality="not_applicable",
                )
            if not quiet:
                print(f"[{index}/{len(rows)}] 지도 대상 아님(차량/동산): {row['item_key']}")
            continue

        result = geocode_address(address)
        if result is None:
            missing += 1
            if not dry_run:
                store.mark_coordinate_missing(
                    row["item_key"],
                    normalized_address=normalize_auction_address(address),
                    geocode_query=normalize_auction_address(address),
                )
            if not quiet:
                print(f"[{index}/{len(rows)}] 좌표 없음: {row['item_key']} {address}")
            continue

        updated += 1
        if not dry_run:
            store.update_coordinates(
                row["item_key"],
                lat=result.lat,
                lng=result.lng,
                pnu=result.pnu,
                coordinate_source=result.source,
                coordinate_quality=result.quality,
                normalized_address=result.normalized_address,
                geocode_query=result.query,
            )
            # 좌표와 함께 PNU가 잡혔으면 공시기준가도 이어서 채운다.
            if result.pnu:
                enrich_official_price_for_row(
                    store,
                    {**row, "pnu": result.pnu},
                    quiet=quiet,
                )
        if not quiet:
            print(f"[{index}/{len(rows)}] 좌표 저장: {row['item_key']} {result.lat:.6f},{result.lng:.6f}")

    return {"no_key": False, "targets": len(rows), "updated": updated, "missing": missing, "excluded": excluded}


def enrich_official_price_for_row(store: AuctionStore, row: dict[str, Any], *, quiet: bool = True) -> str:
    """한 물건의 공시기준가를 조회해 저장한다. 결과 status를 돌려준다."""
    address = row.get("address", "")
    category = row.get("category", "")
    pnu = str(row.get("pnu") or "")
    kind = classify_official_kind(category, address)

    # 오피스텔·상가·기타는 크롤러가 다루지 않는다(오피스텔은 꽁지맵 로컬 인덱스가 처리).
    if kind not in ("land", "commonHousing", "detachedHousing"):
        store.update_official_price(row["item_key"], value=None, status="skip")
        return "skip"

    land_area = _parse_land_area(address)
    try:
        price = fetch_official_price(pnu=pnu, kind=kind, address=address, land_area=land_area)
    except Exception as error:  # noqa: BLE001 - 조회 실패는 status로만 남기고 진행
        if not quiet:
            print(f"  공시가 조회 오류: {row['item_key']} {error}")
        store.update_official_price(row["item_key"], value=None, status="error")
        return "error"

    if price is None:
        status = "price_miss" if pnu else "geocode_miss"
        store.update_official_price(row["item_key"], value=None, status=status)
        return status

    store.update_official_price(
        row["item_key"],
        value=price.value,
        price_type=price.price_type,
        year=price.year,
        detail=price.detail,
        status="priced",
    )
    if not quiet:
        print(f"  공시기준가 {int(price.value):,}원 ({price.price_type}) {row['item_key']}")
    return "priced"


def run_enrich_prices(
    store: AuctionStore,
    *,
    limit: int = 200,
    include_inactive: bool = False,
    retry_days: int = 14,
    quiet: bool = False,
) -> dict[str, Any]:
    """PNU는 있으나 공시기준가가 비어 있는 물건을 채운다(백필). 좌표/PNU가 이미 있어 재지오코딩 불필요."""
    if not (env_value("VWORLD_API_KEY") or env_value("PUBLIC_DATA_SERVICE_KEY")):
        return {"no_key": True, "targets": 0, "priced": 0}

    active = None if include_inactive else True
    rows = store.list_missing_official_price(limit=limit, active=active, retry_failed_after_days=retry_days)
    counts: dict[str, int] = {}
    for index, row in enumerate(rows, start=1):
        status = enrich_official_price_for_row(store, row, quiet=quiet)
        counts[status] = counts.get(status, 0) + 1
        if not quiet and index % 50 == 0:
            print(f"  진행 {index}/{len(rows)} · 매칭 {counts.get('priced', 0)}")
    return {"no_key": False, "targets": len(rows), "priced": counts.get("priced", 0), "counts": counts}


def run_enrich_buildings(
    store: AuctionStore,
    *,
    limit: int = 200,
    include_inactive: bool = False,
    retry_days: int = 30,
    quiet: bool = False,
) -> dict[str, Any]:
    """PNU는 있으나 건축물대장을 아직 못 채운 물건을 채운다(공공데이터포털)."""
    if not env_value("PUBLIC_DATA_SERVICE_KEY"):
        return {"no_key": True, "targets": 0, "ok": 0}
    active = None if include_inactive else True
    rows = store.list_missing_enrichment(
        "building", limit=limit, active=active, retry_failed_after_days=retry_days
    )
    counts: dict[str, int] = {}
    for index, row in enumerate(rows, start=1):
        try:
            reg = fetch_building_registry(str(row.get("pnu") or ""), row.get("category", ""))
        except RateLimitError:  # 한도 초과 → 이 물건은 건드리지 말고 즉시 중단(다음 실행 재개)
            print("건축물대장 일일 한도 초과 — 백필 중단(내일 이어서).")
            counts["rate_limited"] = 1
            break
        except Exception as error:  # noqa: BLE001 - 조회 실패는 status로만
            if not quiet:
                print(f"  건축물대장 오류: {row['item_key']} {error}")
            store.update_building(row["item_key"], detail=None, status="error")
            counts["error"] = counts.get("error", 0) + 1
            continue
        if reg is None:
            store.update_building(row["item_key"], detail=None, status="miss")
            counts["miss"] = counts.get("miss", 0) + 1
        else:
            store.update_building(row["item_key"], detail=asdict(reg), status="ok")
            counts["ok"] = counts.get("ok", 0) + 1
        if not quiet and index % 50 == 0:
            print(f"  진행 {index}/{len(rows)} · 확보 {counts.get('ok', 0)}")
    return {"no_key": False, "targets": len(rows), "ok": counts.get("ok", 0), "counts": counts}


def run_enrich_transactions(
    store: AuctionStore,
    *,
    limit: int = 150,
    include_inactive: bool = False,
    retry_days: int = 30,
    quiet: bool = False,
) -> dict[str, Any]:
    """PNU는 있으나 실거래가를 아직 못 채운 물건을 채운다(국토부 실거래가)."""
    if not env_value("PUBLIC_DATA_SERVICE_KEY"):
        return {"no_key": True, "targets": 0, "ok": 0}
    active = None if include_inactive else True
    rows = store.list_missing_enrichment(
        "transactions", limit=limit, active=active, retry_failed_after_days=retry_days
    )
    # 같은 법정동을 연달아 처리하면 (법정동+월) 캐시가 최대로 재사용된다.
    rows.sort(key=lambda r: str(r.get("pnu") or "")[:5])
    counts: dict[str, int] = {}
    cache: dict[tuple[str, str, str], Any] = {}
    current_lawd = None
    for index, row in enumerate(rows, start=1):
        lawd = str(row.get("pnu") or "")[:5]
        if lawd != current_lawd:  # 법정동이 바뀌면 이전 캐시는 필요 없다
            cache.clear()
            current_lawd = lawd
        try:
            result = fetch_transactions(
                str(row.get("pnu") or ""), row.get("category", ""), row.get("address", ""),
                cache=cache,
            )
        except RateLimitError:  # 한도 초과 → 즉시 중단(다음 실행 재개)
            print("실거래가 일일 한도 초과 — 백필 중단(내일 이어서).")
            counts["rate_limited"] = 1
            break
        except Exception as error:  # noqa: BLE001
            if not quiet:
                print(f"  실거래가 오류: {row['item_key']} {error}")
            store.update_transactions(row["item_key"], detail=None, status="error")
            counts["error"] = counts.get("error", 0) + 1
            continue
        if result is None:
            # 유형이 대상외(토지 외 매칭 불가 등)이거나 API 미등록.
            status = "unregistered" if classify_transaction_kind(
                row.get("category", ""), row.get("address", "")
            ) else "skip"
            store.update_transactions(row["item_key"], detail=None, status=status)
            counts[status] = counts.get(status, 0) + 1
        else:
            has_data = any(
                (result.get(k) or {}).get("count") for k in ("sales", "rent")
            )
            store.update_transactions(
                row["item_key"], detail=result, status="ok" if has_data else "miss"
            )
            counts["ok" if has_data else "miss"] = counts.get("ok" if has_data else "miss", 0) + 1
        if not quiet and index % 50 == 0:
            print(f"  진행 {index}/{len(rows)} · 확보 {counts.get('ok', 0)}")
    return {"no_key": False, "targets": len(rows), "ok": counts.get("ok", 0), "counts": counts}


def _parse_land_area(address: str) -> float:
    match = re.search(r"([\d,.]+)\s*㎡", str(address or ""))
    if not match:
        return 0.0
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return 0.0


def _search_options_from_args(args: argparse.Namespace) -> SearchOptions:
    return SearchOptions(
        court=args.court,
        keyword=args.keyword,
        start_date=args.start_date,
        end_date=args.end_date,
        auto_search=args.auto_search,
        max_pages=args.max_pages,
        max_items=args.max_items,
        delay=args.delay,
        headful=args.headful,
        collect_details=False,
    )


if __name__ == "__main__":
    raise SystemExit(main())
