"""LLM-based menu PDF extraction. pdfplumber's position-aware extraction
(see _pdf_text) reconstructs this vendor's PDFs in correct reading order, so
the LLM just needs to structure already-well-ordered text into JSON. This is
the primary extraction path when GROQ_API_KEY is configured; refresh_menu()
falls back to the regex parser on any failure here, since the LLM path is
inherently non-deterministic.
"""

import logging
import re
from datetime import date
from io import BytesIO

import pdfplumber

from app.services.llm_client import LLMError, generate_json
from app.services.menu_parser import ParsedMenuItem

logger = logging.getLogger("menu_llm_extractor")

# Matches the vendor's own "Týden 24.8. - 28.8.2026" footer line. Extraction
# scrambles spacing arbitrarily (single letters can end up space-separated),
# so this is matched against the whitespace-stripped text, not the raw text.
WEEK_RANGE_RE = re.compile(r"T[ýy]den(\d{1,2})\.(\d{1,2})\.-(\d{1,2})\.(\d{1,2})\.(\d{4})")

VALID_DAYS = {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday"}
VALID_CATEGORIES = {"Polévka", "Hlavní jídlo 1", "Hlavní jídlo 2", "Hlavní jídlo 3", "Vege. jídlo"}

PROMPT_TEMPLATE = """This is the extracted text of a Czech restaurant's weekly
lunch menu PDF, laid out with a day header (PONDĚLÍ=Monday, ÚTERÝ=Tuesday,
STŘEDA=Wednesday, ČTVRTEK=Thursday, PÁTEK=Friday) followed by category lines:
Polévka (soup), Hlavní jídlo 1-3 (main 1-3), Vege. jídlo (vegetarian). Not
every day has every category.

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
    """pypdf's raw stream-order extraction badly scrambles this vendor's
    PDFs (columns/rows end up flattened out of order); pdfplumber's
    position-aware extraction reconstructs the actual visual reading order
    correctly."""
    with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


def extract_pdf_week_start(pdf_bytes: bytes) -> date | None:
    """Reads the vendor's own "Týden D.M. - D.M.YYYY" footer to find which
    Monday this menu is actually for -- more reliable than guessing from
    which day the email arrived, since the vendor sometimes sends next
    week's menu several days early."""
    compact = re.sub(r"\s+", "", _pdf_text(pdf_bytes))
    all_matches = list(WEEK_RANGE_RE.finditer(compact))
    logger.warning(
        "extract_pdf_week_start: %d match(es) in %d-char compact text; compact[:400]=%r",
        len(all_matches), len(compact), compact[:400],
    )
    m = WEEK_RANGE_RE.search(compact)
    if not m:
        return None
    start_day, start_month, _end_day, end_month, end_year = (int(g) for g in m.groups())
    start_year = end_year - 1 if start_month > end_month else end_year
    try:
        return date(start_year, start_month, start_day)
    except ValueError:
        return None


def extract_menu_with_llm(pdf_bytes: bytes) -> list[ParsedMenuItem]:
    menu_text = _pdf_text(pdf_bytes)
    if not menu_text.strip():
        raise MenuExtractionError("No extractable text found in menu PDF")

    logger.warning("extract_menu_with_llm: menu_text[:300]=%r", menu_text[:300])

    prompt = PROMPT_TEMPLATE.format(menu_text=menu_text)
    try:
        data = generate_json(prompt)
    except LLMError as exc:
        raise MenuExtractionError(str(exc)) from exc

    logger.warning("extract_menu_with_llm: raw Groq response=%r", data)

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
