import json
import os
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

TOKEN_PATH = "token.json"
CLIENT_PATH = "credentials.json"
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.settings.basic",
]


def get_gmail_service():
    # The token.json from the MCP server lacks client_id/client_secret.
    # Merge it with credentials.json to build proper Credentials.
    with open(TOKEN_PATH, "r") as f:
        token_data = json.load(f)

    # Use the original scopes from the token to avoid invalid_scope on refresh
    raw_scope = token_data.get("scope", "")
    if isinstance(raw_scope, list):
        token_scopes = raw_scope
    else:
        token_scopes = raw_scope.split() if raw_scope else []

    with open(CLIENT_PATH, "r") as f:
        client_data = json.load(f)

    installed = client_data.get("installed", client_data.get("web", {}))

    creds = Credentials(
        token=token_data.get("access_token"),
        refresh_token=token_data.get("refresh_token"),
        token_uri=installed.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=installed.get("client_id"),
        client_secret=installed.get("client_secret"),
        scopes=token_scopes,
    )
    return build("gmail", "v1", credentials=creds)


def fetch_threads(max_results: int = 20) -> list[dict]:
    """
    Fetch Gmail inbox threads. Returns a list of thread dicts.
    Never raises — returns an empty list on any error.
    """
    try:
        service = get_gmail_service()
        results = service.users().messages().list(
            userId="me", maxResults=max_results
        ).execute()

        messages = results.get("messages", [])
        threads = []

        for msg in messages:
            msg_data = service.users().messages().get(
                userId="me", id=msg["id"], format="metadata",
                metadataHeaders=["From", "Subject"]
            ).execute()

            headers = msg_data.get("payload", {}).get("headers", [])
            sender = next((h["value"] for h in headers if h["name"] == "From"), "Unknown")
            subject = next((h["value"] for h in headers if h["name"] == "Subject"), "No Subject")
            snippet = msg_data.get("snippet", "")

            threads.append({
                "thread_id": msg["id"],
                "sender": sender,
                "subject": subject,
                "snippet": snippet
            })

        return threads
    except Exception as e:
        print(f"[Gmail Warning] {e}")
        return []