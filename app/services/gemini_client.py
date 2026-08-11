"""Shared low-level Gemini REST call, used by gemini_extractor (menu PDF ->
structured items) and order_summary (Czech email copy)."""

import json
import ssl
import urllib.error
import urllib.request

import certifi

from app.config import settings

GEMINI_MODEL = "gemini-flash-latest"
GEMINI_ENDPOINT = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


class GeminiError(Exception):
    pass


def generate_json(parts: list[dict], response_schema: dict) -> dict | list:
    """Sends `parts` (text and/or inline_data blocks) to Gemini and returns
    the parsed JSON response, constrained to response_schema."""
    if not settings.gemini_api_key:
        raise GeminiError("GEMINI_API_KEY is not configured")

    body = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": response_schema,
        },
    }

    url = f"{GEMINI_ENDPOINT}?key={settings.gemini_api_key}"
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60, context=_SSL_CONTEXT) as resp:
            payload = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # A read timeout past connection establishment surfaces as a bare
        # TimeoutError/OSError, not urllib.error.URLError -- every caller
        # treats GeminiError as "fall back gracefully", so anything network-
        # related here needs to actually land in that bucket.
        raise GeminiError(f"Gemini request failed: {exc}") from exc

    try:
        text = payload["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(text)
    except (KeyError, IndexError, json.JSONDecodeError) as exc:
        raise GeminiError(f"Unexpected Gemini response shape: {payload}") from exc
