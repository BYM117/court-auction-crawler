from __future__ import annotations

import argparse
from datetime import date, timedelta
from pathlib import Path

from .crawler import collect_all_sync, collect_sync
from .excel import save_items_to_excel
from .geocoder import geocode_address, normalize_auction_address
from .models import SearchOptions
from .parser import parse_items_from_html
from .store import AuctionStore
from .utils import parse_date
from .web import WatchRunner, run_server


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
    collect.add_argument("--details", action="store_true", help="상세 링크가 있으면 상세 페이지 정보도 수집합니다.")
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
    sync_once.add_argument("--details", action="store_true", help="상세 링크가 있으면 상세 페이지 정보도 수집합니다.")
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
    collect_all.add_argument("--current-days-before", type=int, default=365, help="진행 물건 시작일 기본값: 오늘부터 N일 전")
    collect_all.add_argument("--current-days-ahead", type=int, default=365, help="진행 물건 종료일 기본값: 오늘부터 N일 뒤")
    collect_all.add_argument("--scheduled-start-date", type=parse_date, help="예정 물건 매각기일 시작일")
    collect_all.add_argument("--scheduled-end-date", type=parse_date, help="예정 물건 매각기일 종료일")
    collect_all.add_argument("--scheduled-days-ahead", type=int, default=60, help="예정 물건 종료일 기본값: 오늘부터 N일 뒤")
    collect_all.add_argument("--court", help="특정 법원명만 수집")
    collect_all.add_argument("--court-start", help="지정한 법원명부터 이어서 수집")
    collect_all.add_argument("--court-limit", type=int, help="테스트용으로 앞에서 N개 법원만 수집")
    collect_all.add_argument("--date-chunk-days", type=int, default=14, help="날짜 구간 분할 크기")
    collect_all.add_argument("--headful", action="store_true", help="브라우저 창을 표시합니다.")
    collect_all.add_argument("--details", action="store_true", help="상세 링크가 있으면 상세 페이지 정보도 수집합니다.")
    collect_all.add_argument("--max-pages", type=int, default=50, help="각 파티션에서 수집할 최대 페이지 수")
    collect_all.add_argument("--max-items", type=int, help="전체 수집할 최대 물건 수")
    collect_all.add_argument("--delay", type=float, default=1.5, help="페이지 사이 대기 시간, 초 단위")
    collect_all.add_argument("--output", help="수집 후 Excel 파일도 저장할 경로")

    geocode_missing = subparsers.add_parser("geocode-missing", help="좌표가 없는 DB 물건의 주소를 미리 좌표 변환합니다.")
    geocode_missing.add_argument("--db", default="data/auction.sqlite3", help="SQLite DB 경로")
    geocode_missing.add_argument("--limit", type=int, default=200, help="이번 실행에서 처리할 최대 물건 수")
    geocode_missing.add_argument("--include-inactive", action="store_true", help="종결/비활성 물건도 좌표 변환합니다.")
    geocode_missing.add_argument("--dry-run", action="store_true", help="DB 저장 없이 변환 가능 여부만 출력합니다.")
    geocode_missing.add_argument("--quiet", action="store_true", help="개별 물건 로그를 출력하지 않습니다.")
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
            collect_details=args.details,
        )
        items = collect_sync(options)
        if args.db:
            result = AuctionStore(args.db).upsert_items(items)
            print(f"DB 저장: 신규 {result.inserted}개, 변경 {result.updated}개, 동일 {result.unchanged}개")
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
        result = AuctionStore(args.db).upsert_items(items)
        print(
            f"{len(items)}개 물건을 DB에 반영했습니다: "
            f"신규 {result.inserted}개, 변경 {result.updated}개, 동일 {result.unchanged}개"
        )
        return 0

    if args.command == "serve":
        store = AuctionStore(args.db)
        runner = None
        if args.watch:
            runner = WatchRunner(
                store,
                _search_options_from_args(args),
                interval_seconds=int(args.interval_minutes * 60),
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
            collect_details=args.details,
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

        def save_partition(partition, partition_items):
            result = store.upsert_items(partition_items)
            totals["inserted"] += result.inserted
            totals["updated"] += result.updated
            totals["unchanged"] += result.unchanged
            print(
                f"  -> {len(partition_items)}개 반영 "
                f"(신규 {result.inserted}, 변경 {result.updated}, 동일 {result.unchanged})"
            )

        items = collect_all_sync(options, on_partition=save_partition)
        print(
            f"{len(items)}개 물건 수집 완료: "
            f"신규 {totals['inserted']}개, 변경 {totals['updated']}개, 동일 {totals['unchanged']}개"
        )
        if args.output:
            output = save_items_to_excel(items, args.output)
            print(f"Excel 저장: {output}")
        return 0

    if args.command == "geocode-missing":
        store = AuctionStore(args.db)
        active = None if args.include_inactive else True
        rows = store.list_missing_coordinates(limit=args.limit, active=active)
        updated = 0
        missing = 0
        for index, row in enumerate(rows, start=1):
            address = row.get("address", "")
            result = geocode_address(address)
            if result is None:
                missing += 1
                if not args.dry_run:
                    store.mark_coordinate_missing(
                        row["item_key"],
                        normalized_address=normalize_auction_address(address),
                        geocode_query=normalize_auction_address(address),
                    )
                if not args.quiet:
                    print(f"[{index}/{len(rows)}] 좌표 없음: {row['item_key']} {address}")
                continue

            updated += 1
            if not args.dry_run:
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
            if not args.quiet:
                print(f"[{index}/{len(rows)}] 좌표 저장: {row['item_key']} {result.lat:.6f},{result.lng:.6f}")

        print(f"좌표 변환 완료: 성공 {updated}개, 실패 {missing}개, 대상 {len(rows)}개")
        return 0

    raise ValueError(f"지원하지 않는 명령입니다: {args.command}")


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
        collect_details=args.details,
    )


if __name__ == "__main__":
    raise SystemExit(main())
