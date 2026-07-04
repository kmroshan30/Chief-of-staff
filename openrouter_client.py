"""
openrouter_client.py
OpenRouter API client for the Chief of Staff agent.

OpenRouter gives access to many models (Claude, GPT-4o, Mistral, etc.)
through one unified OpenAI-compatible endpoint.

Default model: mistralai/mistral-7b-instruct:free
  — free tier, fast, good for email drafting

Fallback chain (in order):
  1. mistralai/mistral-7b-instruct:free
  2. meta-llama/llama-3.1-8b-instruct:free
  3. google/gemma-3-4b-it:free

All models above are free-tier on OpenRouter.

Usage:
    from openrouter_client import draft_with_openrouter

    text = draft_with_openrouter(system_prompt="...", user_prompt="...")
"""

from __future__ import annotations

import os
import requests
from dotenv import load_dotenv

load_dotenv(".env.local")

# ---------------------------------------------------------------------------
# Model roster — free-tier models, ordered by quality
# ---------------------------------------------------------------------------
OPENROUTER_MODELS: list[str] = [
    "mistralai/mistral-7b-instruct:free",
    "meta-llama/llama-3.1-8b-instruct:free",
    "google/gemma-3-4b-it:free",
]

OPENROUTER_MODEL_LABELS: dict[str, str] = {
    "mistralai/mistral-7b-instruct:free":        "Mistral 7B (free)",
    "meta-llama/llama-3.1-8b-instruct:free":     "Llama 3.1 8B (free)",
    "google/gemma-3-4b-it:free":                 "Gemma 3 4B (free)",
}

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"

# Tracks which model was actually used last (updated at runtime)
_last_model_used: str = OPENROUTER_MODELS[0]

# ---------------------------------------------------------------------------
# Error detection
# ---------------------------------------------------------------------------
_RATE_LIMIT_SIGNALS = (
    "rate limit",
    "rate_limit",
    "quota",
    "429",
    "503",
    "overloaded",
    "high demand",
    "unavailable",
    "no endpoints",
    "context length",
)


def _is_retryable(exc: Exception | None, status_code: int = 0, body: str = "") -> bool:
    msg = (str(exc) + body).lower()
    return status_code in (429, 503, 529) or any(s in msg for s in _RATE_LIMIT_SIGNALS)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def draft_with_openrouter(
    system_prompt: str,
    user_prompt: str,
    models: list[str] | None = None,
    temperature: float = 0.5,
) -> tuple[str, str]:
    """
    Generate a draft reply via OpenRouter with automatic model fallback.

    Args:
        system_prompt: The system/persona prompt.
        user_prompt:   The user message (thread + drafting rules).
        models:        Optional model list override (default: OPENROUTER_MODELS).
        temperature:   Sampling temperature (default 0.5).

    Returns:
        A (reply_text, model_id_used) tuple.

    Raises:
        ValueError: If OPENROUTER_API_KEY is not set.
        Exception:  If all models fail with non-retryable errors.
    """
    global _last_model_used

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise ValueError(
            "OPENROUTER_API_KEY not found. Make sure it's set in .env.local"
        )

    model_list = models or OPENROUTER_MODELS
    last_exc: Exception = RuntimeError("No OpenRouter models available.")

    for model_id in model_list:
        try:
            response = requests.post(
                OPENROUTER_API_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://github.com/chief-of-staff",
                    "X-Title": "Chief of Staff",
                },
                json={
                    "model": model_id,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user",   "content": user_prompt},
                    ],
                    "temperature": temperature,
                },
                timeout=60,
            )

            body = response.text

            if response.status_code != 200:
                if _is_retryable(None, response.status_code, body):
                    label = OPENROUTER_MODEL_LABELS.get(model_id, model_id)
                    print(f"[openrouter] {label} unavailable (HTTP {response.status_code}), trying next...")
                    last_exc = Exception(f"HTTP {response.status_code}: {body[:200]}")
                    continue
                raise Exception(f"OpenRouter API error (HTTP {response.status_code}): {body}")

            data = response.json()

            # OpenRouter wraps errors in a JSON "error" field even on 200
            if "error" in data:
                err_msg = str(data["error"])
                if _is_retryable(None, 0, err_msg):
                    label = OPENROUTER_MODEL_LABELS.get(model_id, model_id)
                    print(f"[openrouter] {label} error: {err_msg[:120]}, trying next...")
                    last_exc = Exception(err_msg)
                    continue
                raise Exception(f"OpenRouter error: {err_msg}")

            text = data["choices"][0]["message"]["content"].strip()
            if not text:
                raise Exception(f"Model {model_id} returned empty content.")

            _last_model_used = model_id
            label = OPENROUTER_MODEL_LABELS.get(model_id, model_id)
            print(f"[openrouter] Draft generated with: {label}")
            return text, model_id

        except requests.RequestException as exc:
            if _is_retryable(exc):
                label = OPENROUTER_MODEL_LABELS.get(model_id, model_id)
                print(f"[openrouter] {label} request failed ({exc}), trying next...")
                last_exc = exc
                continue
            raise

    raise last_exc


def get_active_model_label() -> str:
    """Return the human-readable label of the last successfully used model."""
    return OPENROUTER_MODEL_LABELS.get(_last_model_used, _last_model_used)
