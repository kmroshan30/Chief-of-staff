"""
triage.py
Email thread triage with a multi-provider fallback chain.

Provider priority (tried in order):
  1. Gemini  gemini-2.5-flash            (primary)
  2. Gemini  gemini-2.5-flash-lite       (Gemini fallback)
  3. Groq    llama-3.3-70b-versatile     (Groq fallback)
  4. OpenRouter  meta-llama/llama-3.3-70b-instruct:free  (last resort)

If ALL providers fail, returns {"label": "uncategorized", "confidence": 0.0,
"reason": "all providers failed", "model_used": "none"} — never crashes.

Triage output dict keys:
  label       — one of: urgent | needs-reply | meeting-request | newsletter | no-action
  confidence  — float 0.0–1.0
  reason      — one-line explanation
  model_used  — model id that produced the result
  priority    — alias of label (kept for backward-compat with app.py sort logic)
  category    — alias of label (kept for backward-compat with digest/app display)
"""

from __future__ import annotations

import json
import os
import re

import requests
from dotenv import load_dotenv
from google import genai

load_dotenv(".env.local")

# ---------------------------------------------------------------------------
# Provider / model chain
# ---------------------------------------------------------------------------
_TRIAGE_CHAIN: list[dict] = [
    {"provider": "gemini",      "model": "gemini-2.5-flash"},
    {"provider": "gemini",      "model": "gemini-2.5-flash-lite"},
    {"provider": "groq",        "model": "llama-3.3-70b-versatile"},
    {"provider": "openrouter",  "model": "meta-llama/llama-3.3-70b-instruct:free"},
]

# Retryable error signals (move to next provider on these)
_RETRYABLE = (
    "404", "429", "500", "503",
    "quota", "rate limit", "rate_limit",
    "resource_exhausted", "resourceexhausted",
    "overloaded", "high demand", "unavailable",
    "capacity", "not found",
)

# Valid labels the model must return
_VALID_LABELS = {"urgent", "needs-reply", "meeting-request", "newsletter", "no-action"}

# Mapping from new labels → legacy priority/category keys (for app.py compatibility)
_LABEL_TO_PRIORITY: dict[str, str] = {
    "urgent":          "urgent",
    "needs-reply":     "needs-reply",
    "meeting-request": "needs-reply",   # treated as actionable
    "newsletter":      "fyi",
    "no-action":       "ignore",
    "uncategorized":   "unknown",
}

# System prompt — identical for all providers
_SYSTEM_PROMPT = (
    "You are an expert email triage assistant. "
    "Classify the given email thread into exactly one of these categories: "
    "urgent, needs-reply, meeting-request, newsletter, no-action.\n"
    "Respond ONLY in valid JSON with keys: label, confidence, reason.\n"
    "No markdown, no explanation outside JSON."
)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_retryable(exc: Exception | None = None, status: int = 0, body: str = "") -> bool:
    text = (str(exc) + body).lower()
    return status in (404, 429, 500, 503, 529) or any(s in text for s in _RETRYABLE)


def _strip_fences(text: str) -> str:
    """Remove ```json ... ``` or ``` ... ``` markdown fences."""
    text = re.sub(r"^```(?:json)?\s*", "", text.strip())
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _parse_json_result(raw: str, model_id: str) -> dict:
    """
    Parse the JSON response from any provider.
    Returns a normalised triage dict or raises ValueError on bad output.
    """
    cleaned = _strip_fences(raw)
    data = json.loads(cleaned)

    label = str(data.get("label", "")).lower().strip()
    if label not in _VALID_LABELS:
        raise ValueError(f"Unknown label '{label}' from {model_id}")

    confidence = float(data.get("confidence", 0.5))
    confidence = max(0.0, min(1.0, confidence))
    reason = str(data.get("reason", "")).strip()

    return {
        "label":      label,
        "confidence": confidence,
        "reason":     reason,
        "model_used": model_id,
        # Backward-compat keys used by app.py / digest.py
        "priority":   _LABEL_TO_PRIORITY.get(label, "unknown"),
        "category":   label,
    }


def _call_gemini(model_id: str, user_text: str) -> str:
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise ValueError("GEMINI_API_KEY not set")
    client = genai.Client(api_key=api_key)
    combined = f"{_SYSTEM_PROMPT}\n\n{user_text}"
    response = client.models.generate_content(model=model_id, contents=combined)
    if not response.text:
        raise RuntimeError(f"Gemini {model_id} returned empty response")
    return response.text


def _call_groq(model_id: str, user_text: str) -> str:
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        raise ValueError("GROQ_API_KEY not set")
    resp = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model_id,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user",   "content": user_text},
            ],
            "temperature": 0.2,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise Exception(f"Groq HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json()["choices"][0]["message"]["content"]


def _call_openrouter(model_id: str, user_text: str) -> str:
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        raise ValueError("OPENROUTER_API_KEY not set")
    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "draft-desk-agent",
        },
        json={
            "model": model_id,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user",   "content": user_text},
            ],
            "temperature": 0.2,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise Exception(f"OpenRouter HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    if "error" in data:
        raise Exception(f"OpenRouter error: {data['error']}")
    return data["choices"][0]["message"]["content"]


_PROVIDER_CALLERS = {
    "gemini":      _call_gemini,
    "groq":        _call_groq,
    "openrouter":  _call_openrouter,
}

# ---------------------------------------------------------------------------
# Public: triage_thread  (same signature as before)
# ---------------------------------------------------------------------------

def triage_thread(sender: str, subject: str, snippet: str) -> dict:
    """
    Classify a single email thread using the multi-provider fallback chain.

    Returns a dict with keys: label, confidence, reason, model_used,
    priority (legacy), category (legacy).
    Never raises — returns "uncategorized" if all providers fail.
    """
    user_text = (
        f"Sender: {sender}\n"
        f"Subject: {subject}\n"
        f"Preview: {snippet}"
    )

    for entry in _TRIAGE_CHAIN:
        provider = entry["provider"]
        model_id = entry["model"]
        caller   = _PROVIDER_CALLERS[provider]

        try:
            raw = caller(model_id, user_text)
            result = _parse_json_result(raw, model_id)
            print(f"[triage] ✓ {provider}/{model_id} → {result['label']} ({result['confidence']:.2f})")
            return result

        except Exception as exc:
            status = 0
            body   = str(exc)
            # Extract HTTP status from exception message if present
            m = re.search(r"HTTP (\d{3})", body)
            if m:
                status = int(m.group(1))

            if _is_retryable(exc, status, body):
                print(f"[triage] ✗ {provider}/{model_id} retryable error ({body[:80]}), trying next...")
                continue
            # Non-retryable (e.g. bad API key format, JSON decode worked but label invalid)
            print(f"[triage] ✗ {provider}/{model_id} non-retryable error: {body[:120]}")
            continue  # still try next provider for robustness

    # All providers failed
    print("[triage] ✗ All providers failed — returning uncategorized")
    return {
        "label":      "uncategorized",
        "confidence": 0.0,
        "reason":     "all providers failed",
        "model_used": "none",
        "priority":   "unknown",
        "category":   "uncategorized",
    }


# ---------------------------------------------------------------------------
# parse_triage_response  (kept for backward-compat, used by test_pipeline.py)
# ---------------------------------------------------------------------------

def parse_triage_response(text: str) -> dict:
    """
    Legacy parser for the old line-by-line format.
    Still works if called externally; also attempts JSON parse first.
    """
    try:
        return _parse_json_result(text, model_id="legacy")
    except Exception:
        pass

    result = {"priority": "unknown", "category": "other", "reason": "",
              "label": "no-action", "confidence": 0.5, "model_used": "legacy"}
    for line in text.strip().split("\n"):
        if line.startswith("Priority:"):
            result["priority"] = line.replace("Priority:", "").strip().lower()
        elif line.startswith("Category:"):
            result["category"] = line.replace("Category:", "").strip().lower()
        elif line.startswith("Reason:"):
            result["reason"] = line.replace("Reason:", "").strip()
    return result


# ---------------------------------------------------------------------------
# _extract_thread_fields  (unchanged)
# ---------------------------------------------------------------------------

def _extract_thread_fields(thread: dict) -> tuple:
    """
    Safely extract (sender, subject, snippet) from a thread dict.
    Handles both Gmail threads (top-level fields) and sample threads
    that carry a 'messages' list instead.
    """
    subject = thread.get("subject", "(no subject)")

    sender = thread.get("sender", "")
    if not sender:
        messages = thread.get("messages", [])
        if messages:
            sender = messages[0].get("from", "Unknown")

    snippet = thread.get("snippet", "")
    if not snippet:
        messages = thread.get("messages", [])
        if messages:
            snippet = messages[-1].get("body", "")[:200]

    return sender, subject, snippet


# ---------------------------------------------------------------------------
# triage_inbox  (unchanged signature)
# ---------------------------------------------------------------------------

def triage_inbox(threads: list) -> list:
    triaged = []

    for thread in threads:
        sender, subject, snippet = _extract_thread_fields(thread)
        label = triage_thread(
            sender=sender,
            subject=subject,
            snippet=snippet,
        )
        triaged.append({**thread, **label})

    priority_order = {"urgent": 0, "needs-reply": 1, "fyi": 2, "ignore": 3, "unknown": 4}
    triaged.sort(key=lambda x: priority_order.get(x.get("priority", "unknown"), 4))

    return triaged


# ---------------------------------------------------------------------------
# CLI demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sample_threads = [
        {"sender": "boss@company.com",    "subject": "Need your input by EOD",       "snippet": "Can you review the attached proposal before 5pm?"},
        {"sender": "newsletter@medium.com","subject": "Top stories for you this week","snippet": "Here's what's trending in tech..."},
        {"sender": "recruiter@startup.io", "subject": "Quick call this week?",        "snippet": "Hi, I came across your profile and wanted to connect..."},
    ]

    results = triage_inbox(sample_threads)

    for r in results:
        print(
            f"[{r['label'].upper():20s}] conf={r['confidence']:.2f} "
            f"model={r['model_used']:35s} | {r['subject']} — {r['reason']}"
        )
