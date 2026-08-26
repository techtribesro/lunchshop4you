"""Estimates calories per menu item via the configured LLM (Groq), since the
vendor menu has no nutritional info -- one batched call per weekly parse
(all items at once) rather than one call per dish."""

import logging

from app.services.llm_client import LLMError, generate_json
from app.services.menu_parser import ParsedMenuItem

logger = logging.getLogger("calorie_estimator")

PROMPT_TEMPLATE = """Estimate calories for each of these Czech restaurant lunch
dishes, as a typical single-portion restaurant serving (not a whole pot/dish).
Use your general knowledge of the ingredients and cuisine to give a
reasonable estimate -- exact values aren't expected.

Dishes (index: name -- description):
{dish_list}

Return JSON only, as an object {{"items": [...]}} with one object per dish,
in the same order, each with:
- "index": the integer index shown above
- "calories_kcal": your best-estimate integer calorie count for one portion"""


def estimate_calories(items: list[ParsedMenuItem]) -> dict[str, int]:
    """Returns {item_name: calories_kcal} for as many items as the LLM
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
        data = generate_json(prompt)
    except LLMError as exc:
        logger.warning("Calorie estimation failed (%s); leaving calories unset", exc)
        return {}

    raw = data if isinstance(data, list) else data.get("items", [])

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
