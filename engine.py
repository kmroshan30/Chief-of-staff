"""
engine.py — Gmail inbox thread fetcher via MCP server.

Usage:
    python engine.py

    The fetch_threads() function returns the last 20 inbox threads
    as a list of dicts with keys:
        thread_id, sender, subject, snippet, date
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
import subprocess
import time
import webbrowser
from pathlib import Path
from typing import Any

from triage import triage_inbox  # used in __main__ CLI only


# ---------------------------------------------------------------------------
# Gmail OAuth token management
# ---------------------------------------------------------------------------

MCP_CONFIG_DIR = Path.home() / ".gmail-mcp"
OAUTH_KEYS_PATH = MCP_CONFIG_DIR / "gcp-oauth.keys.json"
MCP_TOKEN_PATH = MCP_CONFIG_DIR / "credentials.json"

# In-project auth files (these are the ones the user should manage via git)
PROJECT_CREDENTIALS = Path("credentials.json")   # OAuth client ID/secret
PROJECT_TOKEN = Path("token.json")               # cached access/refresh token


def _ensure_gmail_auth() -> None:
    """
    Make sure the Gmail MCP server has valid OAuth keys and a token.
    If either is missing it will be provisioned from the project-level
    files or by running the interactive auth flow.
    """
    MCP_CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    # --- 1. OAuth client keys (gcp-oauth.keys.json) ---
    if not OAUTH_KEYS_PATH.exists():
        if PROJECT_CREDENTIALS.exists():
            import shutil
            shutil.copy2(str(PROJECT_CREDENTIALS), str(OAUTH_KEYS_PATH))
            print(f"[engine] Copied {PROJECT_CREDENTIALS} -> {OAUTH_KEYS_PATH}")
        else:
            raise FileNotFoundError(
                f"Missing OAuth keys. Place gcp-oauth.keys.json in {MCP_CONFIG_DIR} "
                f"or credentials.json in the project directory."
            )

    # --- 2. OAuth token (credentials.json for the MCP server) ---
    token_valid = _is_valid_json(MCP_TOKEN_PATH)

    if not token_valid and _is_valid_json(PROJECT_TOKEN):
        # Copy project-level token.json to where the MCP server expects it
        import shutil
        shutil.copy2(str(PROJECT_TOKEN), str(MCP_TOKEN_PATH))
        print(f"[engine] Copied {PROJECT_TOKEN} -> {MCP_TOKEN_PATH}")
        token_valid = True

    if not token_valid:
        print("[engine] No Gmail token found. Starting interactive auth...")
        _run_auth_flow()
        if not _is_valid_json(MCP_TOKEN_PATH):
            raise RuntimeError("Gmail authentication failed — token was not created.")

    # Sync config-dir token → project-level token.json (never the reverse)
    if _is_valid_json(MCP_TOKEN_PATH):
        import shutil
        shutil.copy2(str(MCP_TOKEN_PATH), str(PROJECT_TOKEN))
        print(f"[engine] Synced {MCP_TOKEN_PATH} -> {PROJECT_TOKEN}")


def _run_auth_flow() -> None:
    """Run the MCP server's auth subcommand interactively."""
    auth_proc = subprocess.Popen(
        ["node", str(Path("gmail-mcp-server") / "dist" / "index.js"), "auth"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    # Print output until it finishes
    for line in iter(auth_proc.stdout.readline, ""):
        print(line, end="", flush=True)
        # If a URL is printed, open it automatically
        if "http://localhost" in line or "https://accounts.google.com" in line:
            url = line.strip()
            # Extract the URL from the line
            import re as _re
            match = _re.search(r"(https?://\S+)", url)
            if match:
                print(f"[engine] Opening browser for auth: {match.group(1)}")
                webbrowser.open(match.group(1))

    auth_proc.wait()
    if auth_proc.returncode != 0:
        raise RuntimeError("Gmail auth flow exited with error.")


def _is_valid_json(path: Path) -> bool:
    """Return True if the file exists and contains valid JSON with an access_token."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return bool(data.get("access_token"))
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# MCP transport
# ---------------------------------------------------------------------------

MCP_SERVER_CMD = [
    "node",
    str(Path("gmail-mcp-server") / "dist" / "index.js"),
]


# ---------------------------------------------------------------------------
# Low-level MCP transport  (JSON-RPC 2.0 over stdio)
# ---------------------------------------------------------------------------

class MCPClient:
    """Manages a single subprocess talking to an MCP server via stdio."""

    def __init__(self, cmd: list[str]) -> None:
        self.cmd = cmd
        self.proc: subprocess.Popen | None = None
        self._request_id = 0

    def start(self) -> None:
        self.proc = subprocess.Popen(
            self.cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        # Perform MCP handshake
        self._send_request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "engine", "version": "1.0.0"},
        })
        resp = self._read_response()
        if "error" in resp:
            raise RuntimeError(f"MCP initialize failed: {resp['error']}")

        # Send initialized notification (no response expected)
        self._send_notification("notifications/initialized")

    def stop(self) -> None:
        if self.proc:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
            self.proc = None

    # -- public helpers ----------------------------------------------------

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """Call a tool on the MCP server and return its result."""
        self._send_request("tools/call", {
            "name": name,
            "arguments": arguments or {},
        })
        resp = self._read_response()
        if "error" in resp:
            raise RuntimeError(f"Tool '{name}' error: {resp['error']}")
        return resp.get("result", {})

    # -- internal JSON-RPC helpers ----------------------------------------

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _send_request(self, method: str, params: dict[str, Any]) -> None:
        req_id = self._next_id()
        msg = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }
        line = json.dumps(msg, ensure_ascii=False)
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def _send_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        msg = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        }
        line = json.dumps(msg, ensure_ascii=False)
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def _read_response(self) -> dict[str, Any]:
        """Read the next JSON-RPC response line from stdout."""
        while True:
            line = self.proc.stdout.readline()
            if not line:
                err = self.proc.stderr.read()
                raise RuntimeError(
                    f"MCP server closed stdout unexpectedly.\nstderr: {err}"
                )
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue

    def _read_all_responses(self, timeout: float = 30.0) -> list[dict[str, Any]]:
        """Read all pending JSON-RPC response lines from stdout with timeout."""
        import time as _time
        import threading

        responses: list[dict[str, Any]] = []
        deadline = _time.time() + timeout
        lines: list[str] = []
        done = threading.Event()

        def _reader():
            while _time.time() < deadline and not done.is_set():
                line = self.proc.stdout.readline()
                if not line:
                    break
                lines.append(line)

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        t.join(timeout=timeout)
        done.set()

        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                responses.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return responses


# ---------------------------------------------------------------------------
# Gmail service builder
# ---------------------------------------------------------------------------

def _build_gmail_service():
    """Build and return a Gmail API v1 service using the project token."""
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build as _build
    import json as _json

    token_path = PROJECT_TOKEN
    creds_path = PROJECT_CREDENTIALS

    if not token_path.exists():
        raise FileNotFoundError(f"token.json not found at {token_path}")

    token_data = _json.loads(token_path.read_text(encoding="utf-8"))
    creds_data = _json.loads(creds_path.read_text(encoding="utf-8")) if creds_path.exists() else {}
    installed = creds_data.get("installed", creds_data.get("web", {}))

    scopes = token_data.get("scope", "")
    if isinstance(scopes, str):
        scope_list = scopes.split()
    else:
        scope_list = list(scopes)

    creds = Credentials(
        token=token_data.get("access_token"),
        refresh_token=token_data.get("refresh_token"),
        token_uri=installed.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=installed.get("client_id"),
        client_secret=installed.get("client_secret"),
        scopes=scope_list,
    )
    return _build("gmail", "v1", credentials=creds)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def send_reply(
    thread_id: str,
    to: str,
    subject: str,
    body: str,
    message_id: str | None = None,
) -> dict[str, str]:
    """
    Send a reply email. Strategy:
      1. Try via MCP server (primary).
      2. If MCP fails, fall back to direct Gmail API.

    Returns a dict with keys: message_id, thread_id, status, error (if failed).
    method_used key is also set to "mcp" or "direct_api".
    """
    import re as _re

    _ensure_gmail_auth()

    reply_subject = (
        subject if subject.lower().startswith("re:") else f"Re: {subject}"
    )

    # ------------------------------------------------------------------ #
    # PRIMARY: MCP send                                                    #
    # ------------------------------------------------------------------ #
    mcp_args: dict[str, Any] = {
        "to": [to],
        "subject": reply_subject,
        "body": body,
    }
    if thread_id and _re.match(r"^[0-9a-fA-F]{12,32}$", thread_id):
        mcp_args["threadId"] = thread_id
    if message_id and _re.match(r"^[0-9a-fA-F]{12,32}$", message_id):
        mcp_args["inReplyTo"] = message_id

    mcp_error: str = ""
    try:
        client = MCPClient(MCP_SERVER_CMD)
        try:
            client.start()
            result = client.call_tool("send_email", mcp_args)
        finally:
            client.stop()

        # Check MCP-level error fields
        if "error" in result:
            err = result["error"]
            mcp_error = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        elif isinstance(result, dict) and result.get("isError") is True:
            content_list = result.get("content", [])
            mcp_error = content_list[0].get("text", "") if content_list else "isError=True"
        else:
            # Try to extract the returned message ID
            raw_message_id = ""
            content_list = result.get("content", [])
            if content_list and isinstance(content_list, list):
                text = content_list[0].get("text", "")
                match = _re.search(r"ID:\s*(\S+)", text)
                if match:
                    raw_message_id = match.group(1)

            if not raw_message_id:
                response_text = str(
                    content_list[0].get("text", "") if content_list else result
                )
                if "error" in response_text.lower() or "fail" in response_text.lower():
                    mcp_error = response_text
                else:
                    mcp_error = f"Could not extract message ID from: {response_text[:120]}"

            if not mcp_error:
                print(f"[engine] ✓ Email sent via MCP (message_id={raw_message_id})")
                return {
                    "message_id": raw_message_id,
                    "thread_id": thread_id,
                    "status": "sent",
                    "method_used": "mcp",
                }

    except Exception as exc:
        mcp_error = str(exc)

    print(f"[engine] MCP send failed ({mcp_error[:120]}), trying direct Gmail API fallback...")

    # ------------------------------------------------------------------ #
    # FALLBACK: direct Gmail API                                           #
    # ------------------------------------------------------------------ #
    try:
        import base64
        import email.mime.text as _mime
        from email.utils import formataddr as _fmt

        service = _build_gmail_service()

        mime_msg = _mime.MIMEText(body, "plain", "utf-8")
        mime_msg["To"] = to
        mime_msg["Subject"] = reply_subject
        if thread_id and _re.match(r"^[0-9a-fA-F]{12,32}$", thread_id):
            mime_msg["In-Reply-To"] = thread_id
            mime_msg["References"] = thread_id

        raw = base64.urlsafe_b64encode(mime_msg.as_bytes()).decode("utf-8")
        send_body: dict[str, Any] = {"raw": raw}
        if thread_id and _re.match(r"^[0-9a-fA-F]{12,32}$", thread_id):
            send_body["threadId"] = thread_id

        sent = service.users().messages().send(
            userId="me", body=send_body
        ).execute()

        sent_id = sent.get("id", "")
        print(f"[engine] ✓ Email sent via direct Gmail API (message_id={sent_id})")
        return {
            "message_id": sent_id,
            "thread_id": thread_id,
            "status": "sent",
            "method_used": "direct_api",
        }

    except Exception as fallback_exc:
        return {
            "message_id": "",
            "thread_id": thread_id,
            "status": "failed",
            "error": f"MCP: {mcp_error[:120]} | Direct API: {str(fallback_exc)[:120]}",
            "method_used": "none",
        }


def fetch_threads(max_results: int = 5) -> list[dict[str, str]]:
    """
    Return the last *max_results* inbox threads from Gmail.

    Each thread is represented as a dict with keys:
        thread_id, sender, subject, snippet, date

    The list is ordered newest-first.
    """
    _ensure_gmail_auth()

    client = MCPClient(MCP_SERVER_CMD)
    try:
        client.start()

        # 1) Search for the most recent inbox messages
        search_result = client.call_tool("search_emails", {
            "query": "in:inbox",
            "maxResults": max_results,
        })

        messages = _parse_search_output(search_result)

        threads: list[dict[str, str]] = []
        for msg_id, subject, sender, date in messages:
            # 2) Read the full message to obtain the thread ID
            detail = client.call_tool("read_email", {"messageId": msg_id})
            thread_id = _extract_thread_id(detail)

            # 3) Build a snippet from the body text
            snippet = _make_snippet(detail)

            threads.append({
                "thread_id": thread_id or msg_id,
                "sender": sender,
                "subject": subject,
                "snippet": snippet,
                "date": date,
            })

        return threads

    finally:
        client.stop()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_search_output(
    result: dict[str, Any],
) -> list[tuple[str, str, str, str]]:
    """
    Extract (messageId, subject, sender, date) tuples from the
    search_emails result.

    The server returns all results in a single text block, one message
    per entry separated by blank lines.
    """
    entries: list[tuple[str, str, str, str]] = []
    content = result.get("content") or result.get("text") or []

    if isinstance(content, str):
        content = [content]

    for block in content:
        if isinstance(block, dict):
            block_text = block.get("text", "")
        else:
            block_text = str(block)

        # Split on blank lines to get individual message blocks
        raw_blocks = re.split(r"\n\s*\n", block_text.strip())

        for raw in raw_blocks:
            raw = raw.strip()
            if not raw:
                continue
            msg_id = ""
            subject = ""
            sender = ""
            date = ""
            for line in raw.split("\n"):
                line = line.strip()
                if line.startswith("ID:"):
                    msg_id = line[len("ID:"):].strip()
                elif line.startswith("Subject:"):
                    subject = line[len("Subject:"):].strip()
                elif line.startswith("From:"):
                    sender = line[len("From:"):].strip()
                elif line.startswith("Date:"):
                    date = line[len("Date:"):].strip()
            if msg_id:
                entries.append((msg_id, subject, sender, date))
    return entries


def _extract_thread_id(detail: dict[str, Any]) -> str:
    """Pull 'Thread ID: xxx' from the read_email response."""
    content = detail.get("content") or detail.get("text") or ""
    if isinstance(content, list):
        content = "\n".join(
            c.get("text", "") if isinstance(c, dict) else str(c)
            for c in content
        )
    for line in content.split("\n"):
        line = line.strip()
        if line.startswith("Thread ID:"):
            return line[len("Thread ID:"):].strip()
    return ""


def _make_snippet(detail: dict[str, Any], max_chars: int = 120) -> str:
    """Build a plain-text snippet from the email body."""
    content = detail.get("content") or detail.get("text") or ""

    if isinstance(content, list):
        parts: list[str] = []
        for c in content:
            if isinstance(c, dict):
                parts.append(c.get("text", ""))
            else:
                parts.append(str(c))
        body = "\n".join(parts)
    else:
        body = str(content)

    # Remove metadata lines and the HTML note
    clean_lines: list[str] = []
    for line in body.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("Thread ID:", "Subject:", "From:", "To:", "Date:", "ID:", "[Note:")):
            continue
        clean_lines.append(stripped)

    text = " ".join(clean_lines)
    # Strip HTML tags
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    # Decode common HTML entities
    amp = chr(38)
    lt = chr(60)
    gt = chr(62)
    text = text.replace(amp + "amp;", amp)
    text = text.replace(amp + "lt;", lt)
    text = text.replace(amp + "gt;", gt)
    text = text.replace(amp + "nbsp;", " ")
    text = text.replace(amp + "quot;", chr(34))
    text = text.replace(amp + "#39;", chr(39))

    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0] + " ..."
    return text


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Also authenticate Calendar API to save the token
    try:
        from calendar_engine import _ensure_calendar_auth
        _ensure_calendar_auth()
        print("[engine] Calendar auth token saved.")
    except Exception as e:
        print(f"[engine] Calendar auth skipped: {e}")

    threads = fetch_threads()
    results = triage_inbox(threads)
    print(json.dumps(results, indent=2, ensure_ascii=False))
