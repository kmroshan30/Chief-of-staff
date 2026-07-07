"""
credentials_manager.py
Unified OAuth credential loader for local and Streamlit Cloud environments.

Local:  reads credentials.json + token.json from the project directory.
Cloud:  reads GMAIL_TOKEN_JSON and GMAIL_CREDENTIALS_JSON from st.secrets.

Returns a google.oauth2.credentials.Credentials object ready for use with
the Gmail and Calendar APIs.  Auto-refreshes expired tokens and persists
the refreshed token back to disk (local) or nowhere (cloud — ephemeral).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

TOKEN_PATH      = Path("token.json")
CLIENT_PATH     = Path("credentials.json")

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
]


def _is_cloud() -> bool:
    """
    Detect Streamlit Cloud by checking for the appuser home or
    the STREAMLIT_SERVER_ADDRESS env var.
    """
    home = str(Path.home())
    return "/home/appuser" in home or bool(os.environ.get("STREAMLIT_SHARING_MODE"))


def _load_secrets_json(key: str) -> dict[str, Any]:
    """Load a JSON blob stored as a Streamlit secret."""
    try:
        import streamlit as st
        raw = st.secrets.get(key, "")
        if not raw:
            raise KeyError(key)
        return json.loads(raw) if isinstance(raw, str) else dict(raw)
    except Exception as e:
        raise RuntimeError(
            f"Streamlit secret '{key}' not found or invalid JSON: {e}\n"
            "Add it in the Streamlit Cloud dashboard under Settings → Secrets."
        )


def get_gmail_credentials(scopes: list[str] | None = None) -> Credentials:
    """
    Return a valid, possibly-refreshed Credentials object.

    On local:  reads token.json + credentials.json from disk.
    On cloud:  reads GMAIL_TOKEN_JSON + GMAIL_CREDENTIALS_JSON from st.secrets.

    Raises RuntimeError if credentials cannot be obtained.
    """
    target_scopes = scopes or GMAIL_SCOPES

    # ------------------------------------------------------------------ #
    # Load raw data                                                        #
    # ------------------------------------------------------------------ #
    if _is_cloud():
        token_data  = _load_secrets_json("GMAIL_TOKEN_JSON")
        client_data = _load_secrets_json("GMAIL_CREDENTIALS_JSON")
    else:
        if not TOKEN_PATH.exists():
            raise RuntimeError(
                "token.json not found. Run: node gmail-mcp-server/dist/index.js auth"
            )
        token_data  = json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
        client_data = (
            json.loads(CLIENT_PATH.read_text(encoding="utf-8"))
            if CLIENT_PATH.exists() else {}
        )

    installed = client_data.get("installed", client_data.get("web", {}))

    # ------------------------------------------------------------------ #
    # Build Credentials                                                    #
    # ------------------------------------------------------------------ #
    raw_scope = token_data.get("scope", "")
    token_scopes = (
        raw_scope if isinstance(raw_scope, list)
        else (raw_scope.split() if raw_scope else target_scopes)
    )

    creds = Credentials(
        token=token_data.get("access_token"),
        refresh_token=token_data.get("refresh_token"),
        token_uri=installed.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=installed.get("client_id"),
        client_secret=installed.get("client_secret"),
        scopes=token_scopes,
    )

    # ------------------------------------------------------------------ #
    # Refresh if expired                                                   #
    # ------------------------------------------------------------------ #
    if not creds.valid and creds.refresh_token:
        try:
            creds.refresh(Request())
            # Persist refreshed token locally (not possible on cloud — ephemeral FS)
            if not _is_cloud() and TOKEN_PATH.exists():
                TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
        except Exception as e:
            raise RuntimeError(
                f"Token refresh failed: {e}\n"
                "Re-run auth: node gmail-mcp-server/dist/index.js auth"
            )

    return creds


def build_gmail_service():
    """Return a Gmail API v1 service object."""
    from googleapiclient.discovery import build
    creds = get_gmail_credentials()
    return build("gmail", "v1", credentials=creds)


def build_calendar_service():
    """Return a Google Calendar API v3 service object."""
    from googleapiclient.discovery import build
    calendar_scopes = [
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/calendar.events",
    ]
    creds = get_gmail_credentials(scopes=calendar_scopes)
    return build("calendar", "v3", credentials=creds)
