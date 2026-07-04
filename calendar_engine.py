"""
calendar_engine.py — Google Calendar service builder via OAuth.

Shares the same credentials.json and token.json as engine.py.
Provides a _build_calendar_service() function that returns a
googleapiclient.discovery.Resource for the Calendar v3 API.

Usage:
    python -c "from calendar_engine import _build_calendar_service; service = _build_calendar_service(); print(service)"
"""

from __future__ import annotations
import socket

_original_getaddrinfo = socket.getaddrinfo

def ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _original_getaddrinfo(
        host,
        port,
        socket.AF_INET,  # Force IPv4
        type,
        proto,
        flags,
    )

socket.getaddrinfo = ipv4_only_getaddrinfo

import json
import os
import re
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build


# ---------------------------------------------------------------------------
# Shared OAuth paths (same as engine.py)
# ---------------------------------------------------------------------------

MCP_CONFIG_DIR = Path.home() / ".gmail-mcp"
OAUTH_KEYS_PATH = MCP_CONFIG_DIR / "gcp-oauth.keys.json"

# In-project auth files (managed via git)
PROJECT_CREDENTIALS = Path("credentials.json")   # OAuth client ID/secret
PROJECT_TOKEN = Path("token.json")               # cached access/refresh token


# ---------------------------------------------------------------------------
# Calendar scopes
# ---------------------------------------------------------------------------

CALENDAR_SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.readonly",
]


# ---------------------------------------------------------------------------
# OAuth helpers (mirrors _ensure_gmail_auth from engine.py)
# ---------------------------------------------------------------------------

def _is_valid_token(path: Path) -> bool:
    """Return True if the file exists and contains valid JSON with a token."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return bool(data.get("token") or data.get("access_token") or data.get("refresh_token"))
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return False


def _ensure_calendar_auth() -> Credentials:
    """
    Make sure we have valid OAuth credentials for the Calendar API.
    Shares the same credentials.json and token.json as engine.py.

    Returns:
        A google.oauth2.credentials.Credentials object.
    """
    MCP_CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    creds = None

    # 1) Try loading from project-level token.json
    if PROJECT_TOKEN.exists():
        try:
            creds = Credentials.from_authorized_user_file(
                str(PROJECT_TOKEN), CALENDAR_SCOPES
            )
            # Verify the loaded credentials actually cover the calendar scopes.
            # If the token was created by the Gmail MCP server it won't have them.
            if creds and creds.scopes:
                calendar_scope = "https://www.googleapis.com/auth/calendar"
                if not any(calendar_scope in s for s in creds.scopes):
                    print("[calendar_engine] Token missing Calendar scopes — re-running auth flow")
                    creds = None
        except Exception:
            creds = None

    # 2) If no valid credentials, run the auth flow
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            # Save the refreshed token immediately so next run doesn't re-refresh
            with open(str(PROJECT_TOKEN), "w", encoding="utf-8") as token_file:
                token_file.write(creds.to_json())
        else:
            # Use the shared OAuth client ID file
            client_secret = OAUTH_KEYS_PATH if OAUTH_KEYS_PATH.exists() else PROJECT_CREDENTIALS
            if not client_secret.exists():
                raise FileNotFoundError(
                    f"Missing OAuth keys. Place gcp-oauth.keys.json in {MCP_CONFIG_DIR} "
                    f"or credentials.json in the project directory."
                )

            flow = InstalledAppFlow.from_client_secrets_file(
                str(client_secret), CALENDAR_SCOPES
            )
            creds = flow.run_local_server(port=0)

            # 3) Save the token to project-level token.json
            with open(str(PROJECT_TOKEN), "w", encoding="utf-8") as token_file:
                token_file.write(creds.to_json())

    return creds


# ---------------------------------------------------------------------------
# Calendar service builder
# ---------------------------------------------------------------------------

def _build_calendar_service() -> Any:
    """
    Build and return a Google Calendar v3 service object.

    Uses the same credentials.json and token.json as engine.py.
    Returns the googleapiclient.discovery.Resource for the Calendar API.
    """
    creds = _ensure_calendar_auth()
    service = build("calendar", "v3", credentials=creds)
    return service


# ---------------------------------------------------------------------------
# Meeting request parser (Gemini-powered)
# ---------------------------------------------------------------------------

def parse_meeting_request(thread: dict[str, Any] | list[dict[str, Any]]) -> dict[str, Any]:
    """
    Use Gemini to extract structured meeting details from an email thread.

    Args:
        thread: A single thread dict (from engine.fetch_threads) with keys like
                sender, subject, snippet, date — OR a list of email message dicts.

    Returns:
        A dict with keys:
            proposed_times  – list of ISO-8601 datetime strings
            attendees       – list of email addresses
            topic           – one-line summary string
            duration_minutes – int (default 30)
        On failure returns: {"parsing_error": "<description>"}
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return {"parsing_error": "GEMINI_API_KEY environment variable is not set."}

    try:
        from gemini_client import generate_with_fallback, GEMINI_MODEL_LABELS

        # Normalise input: accept a single thread dict or a list of messages
        if isinstance(thread, dict):
            messages_list: list[dict[str, Any]] = [thread]
        else:
            messages_list = thread

        # Build a plain-text representation of the thread
        conversation_parts: list[str] = []
        for msg in messages_list:
            sender = msg.get("sender", "unknown") or msg.get("from", "unknown")
            subject = msg.get("subject", "")
            date_str = msg.get("date", "")
            snippet = msg.get("snippet", "") or msg.get("body", "")
            conversation_parts.append(
                f"From: {sender}\nSubject: {subject}\nDate: {date_str}\nBody:\n{snippet}\n---"
            )

        conversation_text = "\n".join(conversation_parts)
        today_iso = date.today().isoformat()

        system_instruction = (
            "You are a meeting detail extraction assistant. "
            "Your job is to read the following email thread and extract structured meeting information.\n\n"
            f"Today's date is {today_iso}. Use this to resolve relative day names (e.g. 'tomorrow', 'next Monday') "
            "into concrete ISO-8601 datetime strings.\n\n"
            "Return ONLY valid JSON — no markdown, no code fences, no extra text. "
            "The JSON must have exactly these keys:\n"
            '  "proposed_times": list of ISO-8601 datetime strings (e.g. ["2026-06-27T10:00:00"]). '
            "If a timezone is not specified, assume Asia/Kolkata (UTC+5:30). Extract ALL proposed times mentioned.\n"
            '  "attendees": list of email addresses found in the thread (sender + any CC/To recipients).\n'
            '  "topic": a one-line summary of what the meeting is about (max 100 chars).\n'
            '  "duration_minutes": an integer for the suggested meeting duration (default to 30 if not mentioned).\n\n'
            "If you cannot extract meaningful data, return the JSON with empty lists and reasonable defaults."
        )

        full_prompt = f"{system_instruction}\n\n{conversation_text}"

        raw_text, model_used = generate_with_fallback(contents=full_prompt)
        print(f"[calendar_engine] Meeting parse used model: {GEMINI_MODEL_LABELS.get(model_used, model_used)}")

        # Strip markdown code fences if present
        raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
        raw_text = re.sub(r"\s*```$", "", raw_text)
        raw_text = raw_text.strip()

        result: dict[str, Any] = json.loads(raw_text)

        # Ensure expected keys exist with defaults
        result.setdefault("proposed_times", [])
        result.setdefault("attendees", [])
        result.setdefault("topic", "")
        result.setdefault("duration_minutes", 30)

        return result

    except Exception as exc:
        return {"parsing_error": str(exc)}


# ---------------------------------------------------------------------------
# Calendar availability helpers
# ---------------------------------------------------------------------------

def _ensure_z(time_str: str) -> str:
    """Append 'Z' to a time string if it has no timezone info."""
    stripped = time_str.strip()
    if stripped and stripped[-1] not in ("Z", "z", "+", "-") and "T" in stripped:
        # Check if it already ends with an offset like +05:30
        if not re.search(r"[+-]\d{2}:\d{2}$", stripped):
            return stripped + "Z"
    return stripped


def check_availability(time_min: str, time_max: str) -> bool:
    """
    Check the user's primary calendar for conflicts in the given window.

    Args:
        time_min: ISO-8601 start of the window.
        time_max: ISO-8601 end of the window.

    Returns:
        True if the time slot is free (no busy periods), False otherwise.
        Also returns False on any error (safe default).
    """
    try:
        time_min = _ensure_z(time_min)
        time_max = _ensure_z(time_max)

        service = _build_calendar_service()
        body = {
            "timeMin": time_min,
            "timeMax": time_max,
            "items": [{"id": "primary"}],
        }
        result = service.freebusy().query(body=body).execute()
        busy = result.get("calendars", {}).get("primary", {}).get("busy", [])
        return len(busy) == 0
    except Exception:
        return False


def find_free_slot(
    proposed_times: list[str], duration_minutes: int = 30
) -> str | None:
    """
    Iterate through proposed times and return the first free slot.

    Args:
        proposed_times: List of ISO-8601 datetime strings for proposed start times.
        duration_minutes: Length of the meeting in minutes.

    Returns:
        The ISO-8601 string of the first free time slot, or None if all are busy
        or if the list is empty.
    """
    for time_str in proposed_times:
        try:
            # Parse the start time
            start_str = _ensure_z(time_str)
            # Try parsing with timezone offset first
            try:
                start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            except ValueError:
                # Fall back: assume UTC if Z was appended
                if start_str.endswith("Z"):
                    start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                else:
                    continue

            end_dt = start_dt + timedelta(minutes=duration_minutes)
            end_str = end_dt.isoformat()

            if check_availability(time_str, end_str):
                return time_str
        except Exception:
            # Skip malformed time strings gracefully
            continue

    return None


# ---------------------------------------------------------------------------
# Event creation
# ---------------------------------------------------------------------------

def create_event(
    summary: str,
    start_time: str,
    duration_minutes: int,
    attendees: list[str],
    description: str = "",
) -> dict[str, Any]:
    """
    Create a Google Calendar event and send invitations.

    Args:
        summary: Event title.
        start_time: ISO-8601 start datetime string.
        duration_minutes: Length of the event in minutes.
        attendees: List of email addresses to invite.
        description: Optional event description.

    Returns:
        The created event dict from the Google Calendar API.
    """
    end_dt = None
    try:
        # Parse start_time, handling Z and timezone offsets
        parsed = start_time.replace("Z", "+00:00")
        start_dt = datetime.fromisoformat(parsed)
        end_dt = start_dt + timedelta(minutes=duration_minutes)
    except Exception:
        # Fallback: treat start_time as a naive UTC datetime
        start_dt = datetime.fromisoformat(start_time) if "T" in start_time else datetime.utcnow()
        end_dt = start_dt + timedelta(minutes=duration_minutes)

    event_body: dict[str, Any] = {
        "summary": summary,
        "description": description,
        "start": {
            "dateTime": start_dt.isoformat(),
            "timeZone": "UTC",
        },
        "end": {
            "dateTime": end_dt.isoformat(),
            "timeZone": "UTC",
        },
    }

    # Only include attendees with valid email addresses (contain "@")
    valid_attendees = [{"email": e.strip()} for e in attendees if "@" in e]
    if valid_attendees:
        event_body["attendees"] = valid_attendees

    service = _build_calendar_service()
    created = (
        service.events()
        .insert(
            calendarId="primary",
            body=event_body,
            sendUpdates="all",
        )
        .execute()
    )
    return created


# ---------------------------------------------------------------------------
# Quick test (when run directly)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    service = _build_calendar_service()
    # List the next 10 events on the primary calendar
    events_result = service.events().list(
        calendarId="primary",
        maxResults=10,
        singleEvents=True,
        orderBy="startTime",
    ).execute()
    events = events_result.get("items", [])
    if not events:
        print("No upcoming events found.")
    else:
        for event in events:
            start = event["start"].get("dateTime", event["start"].get("date"))
            print(f"{start} - {event['summary']}")