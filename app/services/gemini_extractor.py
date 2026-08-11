"""LLM-based menu PDF extraction. The vendor's PDF text-stream order is
unreliable (see email_poller._extract_menu_text_from_pdf), so instead of
relying purely on layout heuristics, the PDF is handed directly to Gemini
with a structured JSON schema. This is the primary extraction path when
GEMINI_API_KEY is configured; refresh_menu() falls back to the regex parser
on any failure here, since the LLM path is inherently non-deterministic.
"""

import base64
import logging

from app.services.gemini_client import GeminiError, generate_json
from app.services.menu_parser import ParsedMenuItem

logger = logging.getLogger("gemini_extractor")

VALID_DAYS = {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday"}
VALID_CATEGORIES = {"Polévka", "Hlavní jídlo 1", "Hlavní jídlo 2", "Hlavní jídlo 3", "Vege. jídlo"}

PROMPT = """This PDF is a Czech restaurant's weekly lunch menu, laid out with a
day header (PONDĚLÍ=Monday, ÚTERÝ=Tuesday, STŘEDA=Wednesday, ČTVRTEK=Thursday,
PÁTEK=Friday) followed by category lines: Polévka (soup), Hlavní jídlo 1-3
(main 1-3), Vege. jídlo (vegetarian). Not every day has every category.

Extract every item as an object with:
- day: the English weekday name (Monday..Friday)
- category: exactly one of "Polévka", "Hlavní jídlo 1", "Hlavní jídlo 2", "Hlavní jídlo 3", "Vege. jídlo"
- item_name: the dish name in Czech, without allergen codes like (1,3,7)
- description: any extra descriptive text beyond the name, or "" if none
- price_czk: the price in Czech crowns as an integer (strip "Kč", commas, and any other non-digit characters)

Ignore the allergen legend, opening hours, and any text that isn't a menu item."""

RESPONSE_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "day": {"type": "STRING"},
            "category": {"type": "STRING"},
            "item_name": {"type": "STRING"},
            "description": {"type": "STRING"},
            "price_czk": {"type": "INTEGER"},
        },
        "required": ["day", "category", "item_name", "price_czk"],
    },
}


class GeminiExtractionError(Exception):
    pass


def extract_menu_with_gemini(pdf_bytes: bytes) -> list[ParsedMenuItem]:
    parts = [
        {
            "inline_data": {
                "mime_type": "application/pdf",
                "data": base64.b64encode(pdf_bytes).decode("ascii"),
            }
        },
        {"text": PROMPT},
    ]
    try:
        raw_items = generate_json(parts, RESPONSE_SCHEMA)
    except GeminiError as exc:
        raise GeminiExtractionError(str(exc)) from exc

    items: list[ParsedMenuItem] = []
    for raw in raw_items:
        day = raw.get("day")
        category = raw.get("category")
        if day not in VALID_DAYS or category not in VALID_CATEGORIES:
            logger.warning("Skipping item with unexpected day/category: %r", raw)
            continue
        try:
            price = int(raw["price_czk"])
        except (KeyError, TypeError, ValueError):
            logger.warning("Skipping item with unparseable price: %r", raw)
            continue
        items.append(
            ParsedMenuItem(
                day=day,
                category=category,
                item_name=str(raw.get("item_name", "")).strip(),
                description=str(raw.get("description", "")).strip(),
                price_czk=price,
            )
        )

    if not items:
        raise GeminiExtractionError("Gemini returned no valid menu items")

    return items
