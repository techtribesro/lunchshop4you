"""Shared low-level Groq (OpenAI-compatible) chat call, used by
menu_llm_extractor (menu PDF -> structured items) and calorie_estimator."""

import json
import ssl
import urllib.error
import urllib.request

import certifi

from app.config import settings

GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"

_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


class LLMError(Exception):
    pass


def generate_json(prompt: str) -> dict | list:
    """Sends `prompt` to Groq asking for a JSON reply and returns the parsed
    JSON. The prompt itself must spell out the desired shape -- Groq's
    json_object response format guarantees valid JSON syntax, not a specific
    schema, unlike Gemini's responseSchema."""
    if not settings.groq_api_key:
        raise LLMError("GROQ_API_KEY is not configured")

    body = {
        "model": settings.groq_model,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
        "temperature": 0,
    }

    request = urllib.request.Request(
        GROQ_ENDPOINT,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings.groq_api_key}",
            # Groq's Cloudflare edge blocks the default urllib User-Agent
            # outright (error code 1010) -- any non-empty, non-python-looking
            # value clears it.
            "User-Agent": "lunchshop4you/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60, context=_SSL_CONTEXT) as resp:
            payload = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # A read timeout past connection establishment surfaces as a bare
        # TimeoutError/OSError, not urllib.error.URLError -- every caller
        # treats LLMError as "fall back gracefully", so anything network-
        # related here needs to actually land in that bucket.
        raise LLMError(f"Groq request failed: {exc}") from exc

    try:
        text = payload["choices"][0]["message"]["content"]
        return json.loads(text)
    except (KeyError, IndexError, json.JSONDecodeError) as exc:
        raise LLMError(f"Unexpected Groq response shape: {payload}") from exc
