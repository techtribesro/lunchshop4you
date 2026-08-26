"""Shared low-level Groq chat call, used by menu_llm_extractor (menu PDF ->
structured items) and calorie_estimator."""

import json

from groq import APIError, Groq

from app.config import settings


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
        )
    except APIError as exc:
        raise LLMError(f"Groq request failed: {exc}") from exc

    text = completion.choices[0].message.content
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMError(f"Unexpected Groq response shape: {text}") from exc
