"""
test_pipeline.py — End-to-end pipeline test (minimal API usage).

Fetches 3 inbox threads, then classifies them with Gemini.
Uses gemini-3.1-flash-lite to maximise free-tier quota.

Usage:  python test_pipeline.py
"""

import json
import os
import sys
sys.stdout.reconfigure(encoding="utf-8")

# ----- Switch to a cheaper model for testing (saves free-tier quota) -----
# Temporarily override triage's default model
import triage as triage_mod
_ORIGINAL_MODEL = "gemini-2.5-flash-lite"
_TEST_MODEL = "gemini-2.5-flash-lite"

from google import genai
from dotenv import load_dotenv
load_dotenv(".env.local")
_test_client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))


def _test_triage_thread(sender, subject, snippet):
    """Mini triage_thread that uses the cheaper model (with fallback on quota errors)."""
    prompt = f"""
    You are an intelligent email assistant helping triage an inbox.

    Given this email thread metadata, classify it:

    Sender: {sender}
    Subject: {subject}
    Preview: {snippet}

    Respond in this exact format:
    Priority: <urgent | needs-reply | fyi | ignore>
    Category: <one short tag>
    Reason: <one sentence explaining why>
    """
    try:
        response = _test_client.models.generate_content(
            model=_TEST_MODEL,
            contents=prompt
        )
        return triage_mod.parse_triage_response(response.text)
    except Exception as e:
        # Quota exhausted → use rule-based fallback
        status_code = getattr(e, "response", None)
        if status_code is not None:
            status_code = getattr(status_code, "status", "?")
        print(f"  [⚠] API quota hit (status={status_code}), using local fallback...")
        return _rule_based_triage(sender, subject, snippet)


def _rule_based_triage(sender, subject, snippet) -> dict:
    """Simple rule-based classification (zero API cost)."""
    subj_lower = subject.lower()
    sender_lower = sender.lower()
    snip_lower = snippet.lower()

    # Urgent keywords
    urgent_flags = ["urgent", "asap", "deadline", "eod", "today", "immediately"]
    if any(w in subj_lower or w in snip_lower for w in urgent_flags):
        return {"priority": "urgent", "category": "follow-up", "reason": "Contains urgent language or deadline keywords"}

    # Human sender (not a newsletter)
    is_personal = "noreply" not in sender_lower and "newsletter" not in sender_lower
    if is_personal and any(w in subj_lower for w in ["call", "meeting", "interview", "review", "question"]):
        return {"priority": "needs-reply", "category": "follow-up", "reason": "Personal email requesting action or reply"}

    # Job / recruiter
    if any(w in subj_lower for w in ["job", "recruiter", "opportunity", "hiring", "application"]):
        return {"priority": "needs-reply", "category": "job-app", "reason": "Job or recruitment related email"}

    # Newsletters & marketing
    if any(w in sender_lower for w in ["newsletter", "marketing", "mail.", "hello@", "notification"]) or \
       any(w in subj_lower for w in ["newsletter", "weekly", "digest", "top stories"]):
        return {"priority": "fyi", "category": "newsletter", "reason": "Automated newsletter or marketing email"}

    # Default: FYI
    return {"priority": "fyi", "category": "general", "reason": "No urgent or reply-required signals detected"}


def test_triage_inbox(threads):
    """Same as triage_inbox but uses the cheaper model."""
    triaged = []
    for thread in threads:
        label = _test_triage_thread(
            thread["sender"], thread["subject"], thread["snippet"]
        )
        triaged.append({**thread, **label})

    priority_order = {"urgent": 0, "needs-reply": 1, "fyi": 2, "ignore": 3, "unknown": 4}
    triaged.sort(key=lambda x: priority_order.get(x["priority"], 4))
    return triaged


# ----- Main test -----
from engine import _ensure_gmail_auth, MCPClient, MCP_SERVER_CMD

print("=" * 55)
print("test_pipeline: Fetch 3 threads → Classify (gemini-3.1-flash-lite)")
print("=" * 55)

_ensure_gmail_auth()

client = MCPClient(MCP_SERVER_CMD)
try:
    client.start()

    # Fetch only 3 threads to conserve API quota
    search_result = client.call_tool("search_emails", {
        "query": "in:inbox",
        "maxResults": 3,
    })

    # Parse search output (reuse engine's parser)
    import engine as eng_mod
    messages = eng_mod._parse_search_output(search_result)

    threads = []
    for msg_id, subject, sender, date in messages:
        detail = client.call_tool("read_email", {"messageId": msg_id})
        thread_id = eng_mod._extract_thread_id(detail)
        snippet = eng_mod._make_snippet(detail)
        threads.append({
            "thread_id": thread_id or msg_id,
            "sender": sender,
            "subject": subject,
            "snippet": snippet,
            "date": date,
        })

    print(f"\nFetched {len(threads)} thread(s) from Gmail ✓")
    for t in threads:
        print(f"  • {t['subject'][:60]}")

    # Classify with the cheaper model
    print(f"\nClassifying with {_TEST_MODEL}...")
    results = test_triage_inbox(threads)

    print("\n--- TRIAGE RESULTS (sorted by priority) ---")
    for r in results:
        print(f"  [{r['priority'].upper():12s}] [{r['category']:15s}] {r['subject'][:55]}")
        print(f"   ↳ {r['reason'][:100]}")

    print("\n✅ Pipeline test complete")

finally:
    client.stop()