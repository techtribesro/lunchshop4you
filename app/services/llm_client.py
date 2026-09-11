"""Shared low-level Groq chat call, used by menu_llm_extractor (menu PDF ->
structured items) and calorie_estimator."""

import json

from groq import APIError, Groq

from app.config import settings

# A full week's menu is ~25 items of Czech text, which measured at roughly
# 1800-1950 output tokens. 8000 leaves generous headroom for a longer menu
# while still capping a runaway generation.
MAX_OUTPUT_TOKENS = 8000

# gpt-oss-* are reasoning models: left at their default effort they spend the
# bulk of the completion budget on hidden reasoning and then truncate the JSON
# mid-object. Measured on a full week's menu, the default returned 14 of 25
# items with finish_reason "stop"; "low" returns all 25. Ignored by non-
# reasoning models, so it's safe to send unconditionally.
REASONING_EFFORT = "low"


class LLMError(Exception):
    pass


def generate_json(prompt: str) -> dict | list:
    """Sends `prompt` to Groq asking for a JSON reply and returns the parsed
    JSON. The prompt itself must spell out the desired shape -- Groq's
    json_object response format guarantees valid JSON syntax, not a specific
    schema, unlike Gemini's responseSchema."""
    if not settings.groq_api_key:
        raise LLMError("GROQ_API_KEY is not configured")

    client = Groq(api_key=settings.groq_api_key)
    try:
        completion = client.chat.completions.create(
            model=settings.groq_model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=MAX_OUTPUT_TOKENS,
            reasoning_effort=REASONING_EFFORT,
        )
    except APIError as exc:
        raise LLMError(f"Groq request failed: {exc}") from exc

    if not completion.choices:
        raise LLMError("Groq returned no choices")

    choice = completion.choices[0]

    # A truncated response is the dangerous failure here: the JSON is cut off
    # mid-object, so it either fails to parse or -- worse -- parses into a
    # silently short item list that _store_week would commit over a good week
    # (it deletes the week before inserting). Fail loudly instead.
    if choice.finish_reason == "length":
        raise LLMError(
            f"Groq response truncated at the {MAX_OUTPUT_TOKENS}-token output limit; "
            "the menu may be too long for a single request"
        )

    text = choice.message.content
    if not text:
        raise LLMError("Groq returned an empty response")

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMError(f"Unexpected Groq response shape: {text}") from exc
