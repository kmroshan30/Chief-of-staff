"""
gemini_client.py
Shared Gemini client with automatic model fallback.

Model priority order (try each in sequence on retryable errors):
  1. gemini-2.5-flash      — primary, most capable
  2. gemini-2.5-flash-lite — fast, lower quota cost

Usage:
    from gemini_client import generate_with_fallback, GEMINI_MODELS

    text, model_used = generate_with_fallback(contents="...")
"""

from __future__ import annotations

import os

from google import genai
from dotenv import load_dotenv

load_dotenv(".env.local")

# ---------------------------------------------------------------------------
# Model roster — ordered by preference (primary first)
# ---------------------------------------------------------------------------
GEMINI_MODELS: list[str] = [
    "gemini-2.5-flash",       # 1st choice: primary
    "gemini-2.5-flash-lite",  # 2nd choice: lighter fallback
]

# Human-readable labels for the UI
GEMINI_MODEL_LABELS: dict[str, str] = {
    "gemini-2.5-flash":      "Gemini 2.5 Flash",
    "gemini-2.5-flash-lite": "Gemini 2.5 Flash Lite",
}

# ---------------------------------------------------------------------------
# Error signals that mean "try the next provider"
# ---------------------------------------------------------------------------
_RETRYABLE_SIGNALS = (
    "quota",
    "rate limit",
    "rate_limit",
    "resource_exhausted",
    "resourceexhausted",
    "429",
    "404",
    "500",
    "503",
    "overloaded",
    "high demand",
    "unavailable",
    "capacity",
    "not found",
)


def _is_retryable(exc: Exception) -> bool:
    """Return True if the exception warrants trying the next model."""
    msg = str(exc).lower()
    return any(signal in msg for signal in _RETRYABLE_SIGNALS)


def _get_client() -> genai.Client:
    """Return a Gemini client using GEMINI_API_KEY from the environment."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY not found. Make sure it's set in .env.local")
    return genai.Client(api_key=api_key)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_with_fallback(
    contents: str,
    models: list[str] | None = None,
) -> tuple[str, str]:
    """
    Call Gemini with automatic model fallback.

    Tries each model in *models* (default: GEMINI_MODELS) in order.
    Moves to the next model on any retryable error (quota, 404, 429, 500…).
    Raises the last exception if all models fail.

    Args:
        contents: The full prompt string to send.
        models:   Optional override for the model list.

    Returns:
        A (response_text, model_id_used) tuple.
    """
    model_list = models or GEMINI_MODELS
    client = _get_client()
    last_exc: Exception = RuntimeError("No Gemini models available.")

    for model_id in model_list:
        try:
            response = client.models.generate_content(
                model=model_id,
                contents=contents,
            )
            if not response.text:
                raise RuntimeError(f"Model {model_id} returned an empty response.")
            return response.text.strip(), model_id

        except Exception as exc:
            if _is_retryable(exc):
                label = GEMINI_MODEL_LABELS.get(model_id, model_id)
                print(f"[gemini_client] {label} unavailable ({exc}), trying next model...")
                last_exc = exc
                continue
            # Non-retryable error — re-raise immediately
            raise

    raise last_exc


def get_active_model_label() -> str:
    """
    Return the label of the first model that is currently responding.
    Falls back to the first model name if the probe fails.
    """
    try:
        _, model_id = generate_with_fallback("ping")
        return GEMINI_MODEL_LABELS.get(model_id, model_id)
    except Exception:
        return GEMINI_MODEL_LABELS.get(GEMINI_MODELS[0], GEMINI_MODELS[0])
