"""
context_builder.py
Assembles the full prompt context for an email reply drafting agent.
"""

import json


def load_tone_profile(path="tone_profile.json") -> dict:
    """Reads and returns the tone profile dict from a JSON file."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_past_replies(path="past_replies.json") -> list:
    """Reads and returns the list of past reply examples from a JSON file."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def format_thread_history(thread: dict) -> str:
    """
    Takes a thread dict with "subject" and "messages" (list of {from, date, body})
    and formats it as a readable string showing who said what in order.

    Also handles Gmail-style threads from engine.fetch_threads() that carry
    sender / snippet / date at the top level instead of a messages list.
    """
    subject = thread.get("subject", "(no subject)")
    messages = thread.get("messages", [])

    lines = [f"Subject: {subject}", ""]
    if messages:
        for msg in messages:
            sender = msg.get("from", "Unknown")
            date = msg.get("date", "")
            body = (
                msg.get("body")
                or msg.get("content")
                or msg.get("text")
                or msg.get("snippet")
                or ""
            )
            lines.append(f"From: {sender}")
            if date:
                lines.append(f"Date: {date}")
            lines.append(body)
            lines.append("")  # blank line between messages
    else:
        sender = thread.get("sender", "Unknown")
        date = thread.get("date", "")
        body = (
            thread.get("body")
            or thread.get("snippet")
            or ""
        )
        lines.append(f"From: {sender}")
        if date:
            lines.append(f"Date: {date}")
        if body:
            lines.append(body)
        lines.append("")

    result = "\n".join(lines)
    # #region agent log
    _debug_log_context(
        "context_builder.py:format_thread_history",
        {
            "has_messages": bool(messages),
            "has_snippet": bool(thread.get("snippet")),
            "body_chars": len(result),
        },
    )
    # #endregion
    return result


# #region agent log
def _debug_log_context(location: str, data: dict) -> None:
    import json
    import time
    try:
        with open("debug-72abbe.log", "a", encoding="utf-8") as _f:
            _f.write(json.dumps({
                "sessionId": "72abbe",
                "location": location,
                "message": "thread history formatted",
                "data": data,
                "hypothesisId": "B",
                "timestamp": int(time.time() * 1000),
                "runId": "post-fix",
            }) + "\n")
    except OSError:
        pass
# #endregion


def build_system_prompt(tone_profile: dict, past_replies: list) -> str:
    """
    Builds the system prompt that includes:
      - The persona (name, role, tone, formality)
      - Writing rules from the quirks list
      - 2-3 past reply examples formatted as "Here's how {name} writes:"
    """
    name = tone_profile["name"]
    role = tone_profile["role"]
    tone = tone_profile["tone"]
    formality = tone_profile["formality"]
    quirks = tone_profile["quirks"]

    parts = []
    parts.append(f"You are {name}, a {role}.")
    parts.append(f"Your writing tone is {tone} and your style is {formality}.")
    parts.append("")
    parts.append("Writing rules:")
    for q in quirks:
        parts.append(f"  - {q}")
    parts.append("")

    # Include up to 3 past reply examples
    examples = past_replies[:3]
    for ex in examples:
        parts.append(f"Here's how {name} writes:")
        parts.append(
            ex.get("body") or ex.get("content") or ex.get("text") or ""
        )
        parts.append("")

    return "\n".join(parts)


def build_user_prompt(thread_formatted: str) -> str:
    """Builds the user message asking for a reply draft."""
    return (
        "Below is the email thread. Write a reply in the same style described above.\n"
        "\n"
        f"{thread_formatted}\n"
        "\n"
        "Draft the reply now:"
    )


def assemble_context(
    thread: dict,
    tone_path: str = "tone_profile.json",
    replies_path: str = "past_replies.json",
) -> dict:
    """
    Main function: loads everything and returns a dict:
    {"system": system_prompt, "user": user_prompt}

    Never raises — returns an error message string in the user prompt
    if a KeyError or any other exception occurs.
    """
    try:
        tone_profile = load_tone_profile(tone_path)
        past_replies = load_past_replies(replies_path)
        thread_formatted = format_thread_history(thread)
        system_prompt = build_system_prompt(tone_profile, past_replies)
        user_prompt = build_user_prompt(thread_formatted)
        return {"system": system_prompt, "user": user_prompt}
    except KeyError as e:
        return {
            "system": "",
            "user": f"Draft failed: missing field {e}",
        }
    except Exception as e:
        return {
            "system": "",
            "user": f"Draft failed: {e}",
        }


# ---------------------------------------------------------------------------
# Demo / test run when executed directly
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    sample_thread = {
        "subject": "Proposed changes to the onboarding flow",
        "messages": [
            {
                "from": "Priya K.",
                "date": "2026-06-15",
                "body": "Hey Rahul, I've drafted a revised onboarding flow that cuts step 3 entirely. Thoughts?"
            },
            {
                "from": "Rahul Sharma",
                "date": "2026-06-15",
                "body": "Looks promising — let me take a closer look and get back to you tomorrow."
            },
            {
                "from": "Priya K.",
                "date": "2026-06-16",
                "body": "Sure, no rush. I've attached the latest mockups for reference."
            }
        ]
    }

    result = assemble_context(sample_thread)

    print("=" * 60)
    print("SYSTEM PROMPT")
    print("=" * 60)
    print(result["system"])
    print()
    print("=" * 60)
    print("USER PROMPT")
    print("=" * 60)
    print(result["user"])
    print("=" * 60)