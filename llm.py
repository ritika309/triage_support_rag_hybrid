"""LLM provider abstraction.

Single function `call_structured` that takes a Pydantic schema and returns
a validated instance. Supports Gemini (native) and Groq (json_object mode +
Pydantic validation). Includes:

- a global token-bucket rate limiter (LLM_MAX_RPM)
- 429-aware exponential backoff with jitter
- structured-output retries on validation errors
"""
from __future__ import annotations

import json
import os
import random
import re
import threading
import time
from typing import Type, TypeVar

from pydantic import BaseModel, ValidationError

from .config import SETTINGS

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    pass


class RateLimiter:
    """Simple thread-safe token bucket. Enforces min interval between calls."""

    def __init__(self, rpm: int) -> None:
        self.min_interval = 60.0 / max(1, rpm)
        self._lock = threading.Lock()
        self._next_ok = 0.0

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._next_ok - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_ok = now + self.min_interval


_RATE_LIMITER = RateLimiter(SETTINGS.max_rpm)
_gemini_client = None
_groq_client = None


_RETRY_AFTER_RE = re.compile(r"retry[_\- ]after[^\d]*(\d+)", re.IGNORECASE)


def _is_rate_limit_error(e: BaseException) -> bool:
    msg = str(e).lower()
    return (
        "429" in msg
        or "resource_exhausted" in msg
        or "rate limit" in msg
        or "quota" in msg
    )


def _parse_retry_after(e: BaseException) -> float | None:
    """Extract a server-provided retry-after hint, in seconds."""
    m = _RETRY_AFTER_RE.search(str(e))
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def _get_groq():
    global _groq_client
    if _groq_client is not None:
        return _groq_client
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise LLMError(
            "GROQ_API_KEY not set. Get a free key at https://console.groq.com/keys"
        )
    from groq import Groq
    _groq_client = Groq(api_key=api_key)
    return _groq_client


def _get_gemini():
    global _gemini_client
    if _gemini_client is not None:
        return _gemini_client
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise LLMError(
            "GOOGLE_API_KEY not set. Copy .env.example to .env and add your "
            "free-tier Gemini API key from https://aistudio.google.com/apikey"
        )
    from google import genai
    _gemini_client = genai.Client(api_key=api_key)
    return _gemini_client


# Backoff schedule on 429 (seconds). Indexed by attempt number.
_BACKOFF_429 = [15.0, 30.0, 60.0, 90.0, 120.0]
# Backoff schedule for transient/validation errors.
_BACKOFF_TRANSIENT = [1.0, 2.0, 4.0, 8.0, 12.0]
_MAX_ATTEMPTS_429 = 5  # override SETTINGS.max_retries when rate-limited


def call_structured(
    system: str,
    user: str,
    schema: Type[T],
    model: str | None = None,
    temperature: float = 0.0,
) -> T:
    """Call the LLM and return a validated Pydantic instance.

    Globally rate-limited and retries on 429 with long backoff.
    Raises LLMError after exhausting retries.
    """
    provider = SETTINGS.provider
    model = model or SETTINGS.triage_model
    base_max_retries = SETTINGS.max_retries

    last_err: Exception | None = None
    attempt = 0
    while True:
        _RATE_LIMITER.acquire()
        try:
            if provider == "gemini":
                return _call_gemini(system, user, schema, model, temperature)
            if provider == "groq":
                return _call_groq(system, user, schema, model, temperature)
            raise LLMError(f"unsupported provider: {provider}")
        except Exception as e:
            last_err = e
            is_429 = _is_rate_limit_error(e)
            limit = (_MAX_ATTEMPTS_429 if is_429 else base_max_retries)
            if attempt >= limit:
                raise LLMError(
                    f"LLM call failed after {attempt + 1} attempts "
                    f"(rate_limit={is_429}): {e}"
                ) from e
            schedule = _BACKOFF_429 if is_429 else _BACKOFF_TRANSIENT
            sleep_s = schedule[min(attempt, len(schedule) - 1)]
            # Prefer server-provided retry-after if present
            ra = _parse_retry_after(e)
            if ra is not None and ra > sleep_s:
                sleep_s = ra
            sleep_s += random.uniform(0, 1.0)  # jitter
            kind = "429" if is_429 else type(e).__name__
            print(f"[llm] {kind} on attempt {attempt + 1}; sleeping {sleep_s:.1f}s")
            time.sleep(sleep_s)
            attempt += 1


def _call_groq(
    system: str, user: str, schema: Type[T], model: str, temperature: float
) -> T:
    """Groq via OpenAI-compatible API. JSON-object mode + Pydantic validation.

    The schema is injected into the system prompt so the model produces
    a JSON object that we then validate against the Pydantic model.
    """
    client = _get_groq()
    schema_json = json.dumps(schema.model_json_schema(), indent=2)
    system_with_schema = (
        f"{system}\n\n"
        f"## Required JSON output schema\n\n"
        f"```json\n{schema_json}\n```\n\n"
        f"Return ONLY a single JSON object that matches the schema. "
        f"No prose, no markdown fences, no commentary."
    )
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_with_schema},
            {"role": "user", "content": user},
        ],
        temperature=temperature,
        response_format={"type": "json_object"},
        max_tokens=2048,
    )
    text = resp.choices[0].message.content if resp.choices else None
    if not text:
        raise LLMError("empty response from Groq")
    return schema.model_validate_json(text)


def _call_gemini(
    system: str, user: str, schema: Type[T], model: str, temperature: float
) -> T:
    from google.genai import types

    client = _get_gemini()
    cfg = types.GenerateContentConfig(
        system_instruction=system,
        temperature=temperature,
        response_mime_type="application/json",
        response_schema=schema,
    )
    resp = client.models.generate_content(
        model=model,
        contents=user,
        config=cfg,
    )
    parsed = getattr(resp, "parsed", None)
    if parsed is not None:
        if isinstance(parsed, schema):
            return parsed
        if isinstance(parsed, dict):
            return schema.model_validate(parsed)
    text = getattr(resp, "text", None)
    if not text:
        raise LLMError("empty response from Gemini")
    return schema.model_validate_json(text)
