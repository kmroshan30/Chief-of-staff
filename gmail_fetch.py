"""
gmail_fetch.py
Fetch Gmail inbox threads using the direct Gmail API.
Works both locally (token.json + credentials.json) and on Streamlit Cloud
(credentials loaded from st.secrets via credentials_manager).
"""

from credentials_manager import build_gmail_service


def fetch_threads(max_results: int = 20) -> list[dict]:
    """
    Fetch Gmail inbox threads via the direct Gmail API.
    Returns a list of thread dicts with keys: thread_id, sender, subject, snippet.
    Never raises — returns an empty list on any error.
    """
    try:
        service = build_gmail_service()
        results = service.users().messages().list(
            userId="me",
            labelIds=["INBOX"],
            maxResults=max_results,
        ).execute()

        messages = results.get("messages", [])
        threads = []

        for msg in messages:
            msg_data = service.users().messages().get(
                userId="me",
                id=msg["id"],
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute()

            headers = msg_data.get("payload", {}).get("headers", [])
            sender  = next((h["value"] for h in headers if h["name"] == "From"),    "Unknown")
            subject = next((h["value"] for h in headers if h["name"] == "Subject"), "No Subject")
            date    = next((h["value"] for h in headers if h["name"] == "Date"),    "")
            snippet = msg_data.get("snippet", "")
            thread_id = msg_data.get("threadId", msg["id"])

            threads.append({
                "thread_id": thread_id,
                "sender":    sender,
                "subject":   subject,
                "snippet":   snippet,
                "date":      date,
            })

        return threads

    except Exception as e:
        print(f"[gmail_fetch] Warning: {e}")
        return []
