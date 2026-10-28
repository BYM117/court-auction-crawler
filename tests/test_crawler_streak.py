"""망가진 창이 연속 0건을 돌려줄 때 창을 새로 띄우고 연속 0건 구간을 전부 다시 치는지(2026-10-28 마산·진주)."""

import asyncio
import unittest
from datetime import date
from unittest import mock

from court_auction_crawler import crawler
from court_auction_crawler.models import AuctionItem, SearchOptions


class _Browser:
    async def new_page(self, **_):
        return object()

    async def close(self):
        pass


class _Playwright:
    def __init__(self):
        self.chromium = self

    async def launch(self, **_):
        return _Browser()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


class EmptyStreakRetryTest(unittest.TestCase):
    def test_refresh_retries_every_partition_in_the_empty_run(self):
        bot = crawler.CourtAuctionCrawler(SearchOptions())
        state = {"broken": True}

        async def noop(*_, **__):
            return None

        async def courts(*_):
            return ["A", "B", "C", "D"]

        async def restart(*_):
            state["broken"] = False
            return object()

        async def collect(_page, partition, _config):
            if state["broken"]:
                return []
            return [AuctionItem({"사건번호": f"{partition.court} 2026타경1", "물건번호": "1"})]

        bot._collection_configs = lambda: [crawler.CURRENT_SEARCH]
        bot._open_search_page = noop
        bot._get_court_options = courts
        bot._filter_courts = lambda c: c
        bot._date_range_for_config = lambda _: (date(2026, 10, 28), date(2026, 10, 28))
        bot._restart_page = restart
        bot._collect_partition = collect

        saved: list[tuple[str, int]] = []
        with mock.patch.object(crawler, "async_playwright", _Playwright), \
                mock.patch.object(crawler, "_prefer_local_browser_cache", lambda: None):
            items = asyncio.run(bot.collect_all(lambda p, got: saved.append((p.court, len(got)))))

        self.assertEqual(len(items), 4)
        self.assertEqual({court for court, n in saved if n}, {"A", "B", "C", "D"})


if __name__ == "__main__":
    unittest.main()
