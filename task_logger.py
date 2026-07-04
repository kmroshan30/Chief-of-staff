"""
task_logger.py — Persistent action log for the Chief of Staff agent.

Records "sent" and "booked" actions to action_log.json so the user
can review what the agent has done across sessions.
"""

import json
import os
from datetime import datetime


ACTION_LOG_PATH = "action_log.json"


def log_action(
    action_type: str,
    thread_subject: str,
    detail: str,
    action_id: str,
) -> None:
    """
    Append a record to action_log.json.

    Args:
        action_type: Either "sent" or "booked".
        thread_subject: The subject line of the email thread.
        detail: Recipient email (for "sent") or meeting title (for "booked").
        action_id: Gmail message_id or Google Calendar event_id.
    """
    record = {
        "timestamp": datetime.now().isoformat(),
        "action_type": action_type,
        "thread_subject": thread_subject,
        "detail": detail,
        "id": action_id,
    }

    log = []
    if os.path.exists(ACTION_LOG_PATH):
        try:
            with open(ACTION_LOG_PATH, "r", encoding="utf-8") as f:
                log = json.load(f)
        except (json.JSONDecodeError, OSError):
            log = []

    log.append(record)

    with open(ACTION_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2, ensure_ascii=False)


def get_action_log() -> list[dict]:
    """
    Read action_log.json and return the full list of records.

    Returns:
        An empty list if the file does not exist, is empty, or is corrupt.
    """
    if not os.path.exists(ACTION_LOG_PATH):
        return []

    try:
        with open(ACTION_LOG_PATH, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                return []
            return json.loads(content)
    except (json.JSONDecodeError, OSError):
        return []


def clear_log() -> None:
    """Write an empty list to action_log.json, effectively clearing the log."""
    with open(ACTION_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump([], f)