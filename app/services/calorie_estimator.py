"""Estimates calories per menu item via Gemini, since the vendor menu has
no nutritional info -- one batched call per weekly parse (all items at
once) rather than one call per dish."""

import logging

from app.services.gemini_client import GeminiError, generate_json
from app.services.menu_parser import ParsedMenuItem

logger = logging.getLogger("calorie_estimator")

PROMPT_TEMPLATE = """Estimate calories for each of these Czech restaurant lunch
dishes, as a typical single-portion restaurant serving (not a whole pot/dish).
Use your general knowledge of the ingredients and cuisine to give a
reasonable estimate -- exact values aren't expected.

Dishes (index: name -- description):
{dish_list}

Return a JSON array with one object per dish, in the same order, each with:
- "index": the integer index shown above
- "calories_kcal": your best-estimate integer calorie count for one portion"""

RESPONSE_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "index": {"type": "INTEGER"},
            "calories_kcal": {"type": "INTEGER"},
        },
        "required": ["index", "calories_kcal"],
    },
}


def estimate_calories(items: list[ParsedMenuItem]) -> dict[str, int]:
    """Returns {item_name: calories_kcal} for as many items as Gemini
    successfully estimated. Missing entries (on any failure) just mean the
    caller leaves calories_kcal as None for those dishes -- non-fatal."""
    if not items:
        return {}

    dish_list = "\n".join(
        f"{i}: {item.item_name} -- {item.description}" if item.description else f"{i}: {item.item_name}"
        for i, item in enumerate(items)
    )
    prompt = PROMPT_TEMPLATE.format(dish_list=dish_list)

    try:
        raw = generate_json([{"text": prompt}], RESPONSE_SCHEMA)
    except GeminiError as exc:
        logger.warning("Calorie estimation failed (%s); leaving calories unset", exc)
        return {}

    result: dict[str, int] = {}
    for entry in raw:
        try:
            idx = int(entry["index"])
            kcal = int(entry["calories_kcal"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= idx < len(items):
            result[items[idx].item_name] = kcal

    return result
