from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(slots=True)
class SearchOptions:
    court: str | None = None
    keyword: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    auto_search: bool = False
    max_pages: int = 20
    max_items: int | None = None
    delay: float = 1.0
    headful: bool = False
    collect_details: bool = False
    court_limit: int | None = None
    court_start: str | None = None
    date_chunk_days: int = 14
    collection_mode: str = "current"
    current_start_date: date | None = None
    current_end_date: date | None = None
    scheduled_start_date: date | None = None
    scheduled_end_date: date | None = None


@dataclass(slots=True)
class AuctionItem:
    values: dict[str, str] = field(default_factory=dict)

    def normalized(self) -> dict[str, str]:
        return {key.strip(): value.strip() for key, value in self.values.items() if key.strip()}


@dataclass(slots=True)
class SyncSummary:
    collected: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    failed: int = 0
    message: str = ""


@dataclass(slots=True)
class CrawlPartition:
    court: str
    start_date: date
    end_date: date
    source_mode: str = ""

    def label(self) -> str:
        return f"{self.court} {self.start_date.isoformat()}~{self.end_date.isoformat()}"
