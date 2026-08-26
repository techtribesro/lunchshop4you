"""LLM-based menu PDF extraction. The vendor's PDF text-stream order is
unreliable (see email_poller._extract_menu_text_from_pdf), so instead of
relying purely on layout heuristics, the PDF's extracted text is handed to
the LLM with an explicit JSON shape to fill in. This is the primary
extraction path when GROQ_API_KEY is configured; refresh_menu() falls back
to the regex parser on any failure here, since the LLM path is inherently
non-deterministic.
"""

import logging

from pypdf import PdfReader
from io import BytesIO

from app.services.llm_client import LLMError, generate_json
from app.services.menu_parser import ParsedMenuItem

logger = logging.getLogger("menu_llm_extractor")

VALID_DAYS = {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday"}
VALID_CATEGORIES = {"Polévka", "Hlavní jídlo 1", "Hlavní jídlo 2", "Hlavní jídlo 3", "Vege. jídlo"}

PROMPT_TEMPLATE = """This is the extracted text of a Czech restaurant's weekly
lunch menu PDF, laid out with a day header (PONDĚLÍ=Monday, ÚTERÝ=Tuesday,
STŘEDA=Wednesday, ČTVRTEK=Thursday, PÁTEK=Friday) followed by category lines:
Polévka (soup), Hlavní jídlo 1-3 (main 1-3), Vege. jídlo (vegetarian). Not
every day has every category. The text extraction may have scrambled the
original layout order to some degree -- use context to reconstruct which day
and category each item belongs to.

Menu text:
{menu_text}

Return JSON only, as an object {{"items": [...]}}. Extract every item as an
object with:
- day: the English weekday name (Monday..Friday)
- category: exactly one of "Polévka", "Hlavní jídlo 1", "Hlavní jídlo 2", "Hlavní jídlo 3", "Vege. jídlo"
- item_name: the dish name in Czech, without allergen codes like (1,3,7)
- description: any extra descriptive text beyond the name, or "" if none
- price_czk: the price in Czech crowns as an integer (strip "Kč", commas, and any other non-digit characters)
- calories_kcal: your best-estimate integer calorie count for one typical
  restaurant portion of this dish, based on its name/description and your
  general knowledge of the ingredients and cuisine -- exact values aren't
  expected

Ignore the allergen legend, opening hours, and any text that isn't a menu item."""


class MenuExtractionError(Exception):
    pass


def _pdf_text(pdf_bytes: bytes) -> str:
    reader = PdfReader(BytesIO(pdf_bytes))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def extract_menu_with_llm(pdf_bytes: bytes) -> list[ParsedMenuItem]:
    menu_text = _pdf_text(pdf_bytes)
    if not menu_text.strip():
        raise MenuExtractionError("No extractable text found in menu PDF")

    prompt = PROMPT_TEMPLATE.format(menu_text=menu_text)
    try:
        data = generate_json(prompt)
    except LLMError as exc:
        raise MenuExtractionError(str(exc)) from exc

    raw_items = data if isinstance(data, list) else data.get("items", [])

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
        try:
            calories = int(raw["calories_kcal"])
        except (KeyError, TypeError, ValueError):
            calories = None
        items.append(
            ParsedMenuItem(
                day=day,
                category=category,
                item_name=str(raw.get("item_name", "")).strip(),
                description=str(raw.get("description", "")).strip(),
                price_czk=price,
                calories_kcal=calories,
            )
        )

    if not items:
        raise MenuExtractionError("LLM returned no valid menu items")

    return items
