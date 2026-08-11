"""Parses the weekly Czech-language lunch menu email into structured items.

The exact vendor email format is not yet available (see REQUIREMENTS.md
revision note), so this parser targets the structure described in the spec:
a day header (PONDĚLÍ, ÚTERÝ, ...), followed by category headers (Polévka,
Hlavní jídlo 1-3, Vege. jídlo), each followed by one or more lines of item
text ending in a CZK price. It will need tuning against a real sample email.
"""

import re
from dataclasses import dataclass

DAY_NAMES: dict[str, str] = {
    "PONDELI": "Monday",
    "UTERY": "Tuesday",
    "STREDA": "Wednesday",
    "CTVRTEK": "Thursday",
    "PATEK": "Friday",
}

CATEGORY_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^pol[ée]vka\b\.?:?\s*", re.IGNORECASE), "Polévka"),
    (re.compile(r"^hlavn[ií]\s*j[ií]dlo\s*1\b\.?:?\s*", re.IGNORECASE), "Hlavní jídlo 1"),
    (re.compile(r"^hlavn[ií]\s*j[ií]dlo\s*2\b\.?:?\s*", re.IGNORECASE), "Hlavní jídlo 2"),
    (re.compile(r"^hlavn[ií]\s*j[ií]dlo\s*3\b\.?:?\s*", re.IGNORECASE), "Hlavní jídlo 3"),
    (re.compile(r"^vege\.?\s*j[ií]dlo\b\.?:?\s*", re.IGNORECASE), "Vege. jídlo"),
    (re.compile(r"^vegetari[áa]nsk[ée]\s*j[ií]dlo\b\.?:?\s*", re.IGNORECASE), "Vege. jídlo"),
]

ALLERGEN_RE = re.compile(r"\(\s*\d+(?:\s*[,.]\s*\d+)*\s*\)")
PRICE_RE = re.compile(r"(\d{2,4})\s*(?:,-|\s*Kč|\s*CZK)\b", re.IGNORECASE)


def _strip_diacritics(text: str) -> str:
    replacements = str.maketrans(
        "áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ",
        "acdeeinorstuuyzACDEEINORSTUUYZ",
    )
    return text.translate(replacements)


def _match_day(line: str) -> str | None:
    normalized = _strip_diacritics(line).strip().upper()
    normalized = re.sub(r"[^A-Z]", "", normalized.split()[0]) if normalized.split() else ""
    return DAY_NAMES.get(normalized)


def _match_category(line: str) -> tuple[str, str] | None:
    for pattern, canonical in CATEGORY_PATTERNS:
        match = pattern.match(line)
        if match:
            return canonical, line[match.end():].strip()
    return None


@dataclass
class ParsedMenuItem:
    day: str
    category: str
    item_name: str
    description: str
    price_czk: int


class MenuParseError(Exception):
    pass


def parse_menu_email(body: str) -> list[ParsedMenuItem]:
    items: list[ParsedMenuItem] = []
    current_day: str | None = None
    current_category: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        # Any buffered text without a price by the time a new day/category
        # header appears is an incomplete item (no price line found) and is
        # dropped rather than stored with a bogus price.
        nonlocal buffer
        buffer = []

    for raw_line in body.splitlines():
        line = ALLERGEN_RE.sub("", raw_line).strip()
        if not line:
            continue

        day = _match_day(line)
        if day:
            flush()
            current_day = day
            current_category = None
            continue

        category_match = _match_category(line)
        if category_match:
            flush()
            current_category, remainder = category_match
            if not remainder:
                continue
            line = remainder  # falls through to the price check below, since a
            # single-line "Category: Item ... Price" entry is still possible

        price_match = PRICE_RE.search(line)
        if price_match:
            remainder = (line[: price_match.start()] + line[price_match.end():]).strip(" -,")
            if remainder:
                buffer.append(remainder)
            lines = [l for l in buffer if l.strip()]
            if current_day and current_category and lines:
                item_name = lines[0].strip()
                description = " ".join(lines[1:]).strip()
                items.append(
                    ParsedMenuItem(
                        day=current_day,
                        category=current_category,
                        item_name=item_name,
                        description=description,
                        price_czk=int(price_match.group(1)),
                    )
                )
            buffer = []
            continue

        buffer.append(line)

    if not items:
        raise MenuParseError("No menu items found in email body")

    return items
