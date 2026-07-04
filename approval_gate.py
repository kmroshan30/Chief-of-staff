"""
approval_gate.py
Utility functions for the human-in-the-loop approval gate.

This module is imported by app.py and provides:
  - save_approved_draft(): persist an approved draft to approved_drafts.json
  - APPROVED_DRAFTS_FILE: path constant used by the UI

The standalone Streamlit UI that previously lived here has been moved into
app.py's render_approval_phase() function to avoid double page-config errors
and conflicting session-state initialisation.
"""

import json
import os
from datetime import datetime


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

APPROVED_DRAFTS_FILE = "approved_drafts.json"


# ---------------------------------------------------------------------------
# Helper: save approved draft
# ---------------------------------------------------------------------------

def save_approved_draft(thread: dict, draft_text: str, model_used: str = "") -> None:
    """Append an approved draft to approved_drafts.json with a timestamp."""
    # Resolve the actual model used — read from draft_machine at call time
    if not model_used:
        try:
            import draft_machine as _dm
            mc = thread.get("_model_choice", "groq")
            if mc == "gemini":
                model_used = _dm.MODEL_NAME_GEMINI
            elif mc == "openrouter":
                model_used = _dm.MODEL_NAME_OPENROUTER
            else:
                model_used = _dm.MODEL_NAME_GROQ
        except Exception:
            model_used = "unknown"

    entry = {
        "timestamp": datetime.now().isoformat(),
        "subject": thread.get("subject", "(no subject)"),
        "messages": thread.get("messages", []),
        "approved_draft": draft_text,
        "model": model_used,
    }
    existing = []
    if os.path.exists(APPROVED_DRAFTS_FILE):
        with open(APPROVED_DRAFTS_FILE, "r", encoding="utf-8") as f:
            try:
                existing = json.load(f)
            except json.JSONDecodeError:
                existing = []
    existing.append(entry)
    with open(APPROVED_DRAFTS_FILE, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Helper: load approved drafts
# ---------------------------------------------------------------------------

def load_approved_drafts() -> list:
    """Return all approved drafts from approved_drafts.json."""
    if not os.path.exists(APPROVED_DRAFTS_FILE):
        return []
    try:
        with open(APPROVED_DRAFTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
