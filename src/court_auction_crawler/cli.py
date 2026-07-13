from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import re
from typing import Any

from .crawler import collect_all_sync, collect_sync
from .detail_crawler import collect_details_sync
from .excel import save_items_to_excel
from .geocoder import env_value, geocode_address, is_mappable_property, normalize_auction_address
from .models import SearchOptions
from .official_price import classify_official_kind, fetch_official_price
from .parser import parse_items_from_html
from .store import AuctionStore
from .utils import parse_date
from .web import WatchRunner, public_auction_summary, run_server


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
        default="outputs/court-auctions.snapshot.json",
        help="스냅샷 JSON 저장 경로",
    )

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
        # 커버리지 감시는 완주한 전체 수집(full: 예정 윈도우가 넓은 실행)끼리만 비교해야 의미가 있다.
        if (scheduled_end - scheduled_start).days >= 120:
            coverage_warnings = store.record_court_stats(court_counts)
            print(f"법원별 수집 기록 저장: {len(court_counts)}개 (법원×구분)")
            for warning in coverage_warnings:
                print(f"!! 커버리지 경고: {warning}")
        lifecycle = store.apply_lifecycle()
        print(f"생명주기 정리: 활성 {lifecycle['checked']}개 중 {lifecycle['deactivated']}개 종결 처리")
        if args.geocode_limit > 0:
            geocoded = run_geocode_missing(store, limit=args.geocode_limit, quiet=True)
            if geocoded.get("no_key"):
                print("지오코딩 건너뜀: VWORLD_API_KEY가 없습니다 (.env 또는 환경변수).")
            else:
                print(
                    f"지오코딩: 성공 {geocoded['updated']}개, 실패 {geocoded['missing']}개, "
                    f"대상외 {geocoded['excluded']}개, 대상 {geocoded['targets']}개"
                )
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

    if args.command == "collect-details":
        summary = collect_details_sync(
            AuctionStore(args.db),
            limit=args.limit or None,
            include_inactive=args.include_inactive,
            force=args.force,
            item_key=args.item_key,
            asset_dir=args.asset_dir,
            delay=args.delay,
            headful=args.headful,
            collect_documents=not args.skip_documents,
            download_document_files=args.download_document_files,
        )
        print(
            f"상세 수집 완료: 대상 {summary.targets}개, 사건 {summary.cases}건, "
            f"완료 {summary.collected}개, 실패 {summary.failed}개, 조회불가 {summary.unavailable}개, "
            f"문서 {summary.documents_collected}개, 문서 대기 {summary.documents_pending}개"
        )
        return 0

    if args.command == "export-snapshot":
        store = AuctionStore(args.db)
        payload = build_snapshot_payload(store)
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
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

    raise ValueError(f"지원하지 않는 명령입니다: {args.command}")


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
