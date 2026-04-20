"""HTML parsing helpers for BB/DSE tables."""

import re

import bs4
from bs4 import BeautifulSoup


def parse_number(s: str) -> float | None:
    """Parse a numeric string into a float, handling common formatting.

    Handles:
        - Thousands separators: "1,234.56" -> 1234.56
        - Bengali taka prefix: "1,234.56" -> 1234.56
        - Integers: "1,234" -> 1234.0
        - Null indicators: "--", "-", "N/A", "" -> None

    Args:
        s: Raw string from a table cell.

    Returns:
        Parsed float, or None if the string is non-numeric.
    """
    if not isinstance(s, str):
        return None

    # Strip whitespace and Unicode NBSP
    cleaned = s.strip().replace("\u00a0", "").replace("\xa0", "")

    # Null indicators
    if cleaned in ("", "--", "-", "N/A", "n/a", "NA"):
        return None

    # Strip currency symbols (taka sign, ASCII variants)
    cleaned = re.sub(r"[৳$€£¥\u09F3]", "", cleaned).strip()

    # Remove thousands separators and parse
    cleaned = cleaned.replace(",", "")

    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_percent(s: str) -> float | None:
    """Parse a percentage string into a fractional float.

    Handles:
        - "5.42%"  -> 5.42
        - "-0.42%" -> -0.42
        - "+1.2%"  -> 1.2

    Args:
        s: Raw string such as "5.42%" or "-0.42%".

    Returns:
        Numeric percentage value (not divided by 100), or None if unparseable.
    """
    if not isinstance(s, str):
        return None

    cleaned = s.strip().replace("%", "").replace(",", "").strip()

    if cleaned in ("", "--", "-", "N/A"):
        return None

    # Strip explicit '+' sign that float() can't handle in all locales
    cleaned = cleaned.lstrip("+")

    try:
        return float(cleaned)
    except ValueError:
        return None


def find_table(
    soup: BeautifulSoup,
    id: str | None = None,
    class_: str | None = None,
    caption_contains: str | None = None,
) -> bs4.Tag | None:
    """Locate an HTML table by id, CSS class, or caption text.

    Args:
        soup: Parsed BeautifulSoup document.
        id: The HTML id attribute of the table element.
        class_: A CSS class name the table element must have.
        caption_contains: Substring to match in the table's <caption> text.

    Returns:
        The matching bs4.Tag, or None if not found.
    """
    if id is not None:
        return soup.find("table", id=id)  # type: ignore[return-value]

    if class_ is not None:
        return soup.find("table", class_=class_)  # type: ignore[return-value]

    if caption_contains is not None:
        for table in soup.find_all("table"):
            caption = table.find("caption")
            if caption and caption_contains.lower() in caption.get_text().lower():
                return table  # type: ignore[return-value]

    return None


def rows_as_dicts(table: bs4.Tag, header_row: int = 0) -> list[dict]:
    """Convert an HTML table into a list of dicts keyed by header text.

    Args:
        table: A bs4.Tag representing a <table> element.
        header_row: Index of the row to treat as headers (default 0).

    Returns:
        List of dicts. Each dict maps header text -> cell text.
        Rows before header_row are skipped. Empty rows are skipped.
    """
    all_rows = table.find_all("tr")
    if not all_rows or header_row >= len(all_rows):
        return []

    def _cell_text(cell: bs4.Tag) -> str:
        return cell.get_text(separator=" ", strip=True)

    header_cells = all_rows[header_row].find_all(["th", "td"])
    headers = [_cell_text(c) for c in header_cells]

    result: list[dict] = []
    for row in all_rows[header_row + 1 :]:
        cells = row.find_all(["td", "th"])
        if not cells:
            continue
        row_dict = {headers[i]: _cell_text(cells[i]) for i in range(min(len(headers), len(cells)))}
        result.append(row_dict)

    return result
