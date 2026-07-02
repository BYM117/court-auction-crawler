from __future__ import annotations

from html.parser import HTMLParser

from .models import AuctionItem
from .utils import clean_text, unique_headers


class TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._table_stack = 0
        self._current_table: list[list[str]] | None = None
        self._current_row: list[str] | None = None
        self._current_cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "table":
            self._table_stack += 1
            if self._table_stack == 1:
                self._current_table = []
        elif tag == "tr" and self._current_table is not None:
            self._current_row = []
        elif tag in {"th", "td"} and self._current_row is not None:
            self._current_cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in {"th", "td"} and self._current_cell is not None and self._current_row is not None:
            self._current_row.append(clean_text(" ".join(self._current_cell)))
            self._current_cell = None
        elif tag == "tr" and self._current_table is not None and self._current_row is not None:
            if any(self._current_row):
                self._current_table.append(self._current_row)
            self._current_row = None
        elif tag == "table" and self._current_table is not None:
            self._table_stack -= 1
            if self._table_stack == 0:
                self.tables.append(self._current_table)
                self._current_table = None

    def handle_data(self, data: str) -> None:
        if self._current_cell is not None:
            self._current_cell.append(data)


def parse_items_from_html(html: str) -> list[AuctionItem]:
    parser = TableParser()
    parser.feed(html)
    best_table = max(parser.tables, key=_table_score, default=[])
    return rows_to_items(best_table)


def rows_to_items(table_rows: list[list[str]]) -> list[AuctionItem]:
    if len(table_rows) < 2:
        return []
    header_index = _find_header_index(table_rows)
    headers = unique_headers(table_rows[header_index])
    items: list[AuctionItem] = []

    for row in table_rows[header_index + 1 :]:
        if len(row) < 2:
            continue
        padded = row + [""] * max(0, len(headers) - len(row))
        values = dict(zip(headers, padded[: len(headers)]))
        if any(values.values()):
            items.append(AuctionItem(values))
    return items


def _find_header_index(table_rows: list[list[str]]) -> int:
    header_words = ("사건", "물건", "소재", "용도", "감정", "최저", "매각")
    best_index = 0
    best_score = -1
    for index, row in enumerate(table_rows[:5]):
        text = " ".join(row)
        score = sum(1 for word in header_words if word in text)
        if score > best_score:
            best_index = index
            best_score = score
    return best_index


def _table_score(table: list[list[str]]) -> int:
    if not table:
        return 0
    text = " ".join(cell for row in table for cell in row)
    keyword_score = sum(text.count(word) for word in ("사건", "물건", "소재지", "매각", "감정", "최저"))
    cell_count = sum(len(row) for row in table)
    return keyword_score * 100 + cell_count

