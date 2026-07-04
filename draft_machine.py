"""
draft_machine.py
Generates email replies using Groq (llama-3.3-70b-versatile) or Gemini
with automatic model fallback (2.5 Flash Lite → 2.5 Flash → 3 Flash).
"""

import os
import requests
from dotenv import load_dotenv

from context_builder import assemble_context


# Load environment variables
load_dotenv(".env.local")


MODEL_NAME_GROQ = "llama-3.3-70b-versatile"
# Gemini model shown in metadata — reflects whichever model the fallback picked last
MODEL_NAME_GEMINI = "gemini-2.5-flash-lite"  # default; overridden at runtime by fallback
MODEL_NAME_OPENROUTER = "mistralai/mistral-7b-instruct:free"  # default; overridden at runtime
MODEL_NAME = MODEL_NAME_GROQ  # backward-compatible alias for approval_gate


SAMPLE_THREADS = [
    {
        "subject": "Q3 Budget Review — Final Call",
        "messages": [
            {
                "from": "Ananya Mehta",
                "date": "2026-06-17",
                "body": "Hi Rahul, we need to finalise the Q3 budget by Friday. Can you share your team's revised numbers by EOD tomorrow? We're particularly interested in the engineering resourcing costs."
            },
            {
                "from": "Rahul Sharma",
                "date": "2026-06-17",
                "body": "Hey Ananya, I'm reviewing the latest headcount projections with Engineering. I'll have the numbers to you by tomorrow afternoon."
            },
            {
                "from": "Ananya Mehta",
                "date": "2026-06-18",
                "body": "Thanks Rahul. Just a reminder — the CFO has pushed the deadline to Thursday EOD. Also, please include any contractor costs you're anticipating for Q3."
            }
        ]
    },
    {
        "subject": "Sprint Demo — Feedback on New Dashboard",
        "messages": [
            {
                "from": "Priya Kapoor",
                "date": "2026-06-19",
                "body": "Hey Rahul, we just wrapped the sprint demo for the new analytics dashboard. The team loved the real-time filtering feature. A few stakeholders asked about export-to-PDF — is that on the roadmap for this quarter?"
            },
            {
                "from": "Rahul Sharma",
                "date": "2026-06-19",
                "body": "Great to hear the demo went well! PDF export is slated for Q1 next year, but I can check with Engineering if we can pull it forward. Let me get back to you by Monday."
            },
            {
                "from": "Priya Kapoor",
                "date": "2026-06-20",
                "body": "That would be amazing. The client demo is in 3 weeks, so if we can at least have a beta version by then, it'd be a huge win. Happy to deprioritise something else if needed."
            }
        ]
    },
    {
        "subject": "Office Relocation — Floor Plan Confirmation",
        "messages": [
            {
                "from": "Meera Joshi",
                "date": "2026-06-18",
                "body": "Hi Rahul, the new office floor plans are ready for review. We've allocated the Product team to the 4th floor west wing. Could you confirm headcount so we can finalise seating by end of week?"
            },
            {
                "from": "Rahul Sharma",
                "date": "2026-06-18",
                "body": "Thanks Meera. Current headcount is 12, but we're hiring 2 more PMs who start in August. Can we keep 2 hot desks reserved for them?"
            },
            {
                "from": "Meera Joshi",
                "date": "2026-06-19",
                "body": "Absolutely — I'll mark 2 hot desks in the layout. Also, would you prefer open-plan or partitioned desks for the new joiners? Let me know by tomorrow so I can lock in the furniture order."
            }
        ]
    }
]

DRAFTING_RULES = """\
Drafting rules — follow these strictly:

a) ONE-ASK RULE: every email has exactly ONE clear question or ONE clear response.
b) LENGTH CONTROL: match thread energy, max 5 sentences, use numbered points if needed.
c) NO AI FILLER: never say "I hope this finds you well", "Thank you for reaching out", etc.
d) STRUCTURE: acknowledge briefly -> give response -> ONE clear next step.
"""

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"


def draft_reply(thread: dict, model_choice: str = "groq") -> str:
    """
    Takes a thread dict and model choice ("groq" or "gemini"),
    builds context via assemble_context(), appends drafting rules,
    calls the selected LLM, and returns only the draft reply text.

    Raises ValueError on missing API keys, and raises on API errors
    (includes HTTP status code if applicable).
    """
    context = assemble_context(thread)
    system_prompt = context["system"]
    user_prompt = context["user"]

    # Append drafting rules to the user prompt
    full_user_prompt = f"{user_prompt}\n\n{DRAFTING_RULES}"

    if model_choice == "groq":
        return _draft_with_groq(system_prompt, full_user_prompt)
    elif model_choice == "gemini":
        return _draft_with_gemini(system_prompt, full_user_prompt)
    elif model_choice == "openrouter":
        return _draft_with_openrouter(system_prompt, full_user_prompt)
    else:
        raise ValueError(f"Unknown model_choice: '{model_choice}'. Use 'groq', 'gemini', or 'openrouter'.")


def _draft_with_groq(system_prompt: str, user_prompt: str) -> str:
    """Call Groq API via direct HTTP POST."""
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise ValueError(
            "GROQ_API_KEY not found. Make sure it's set in .env.local"
        )

    response = requests.post(
        GROQ_API_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": MODEL_NAME_GROQ,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.5,
        },
        timeout=60,
    )

    if response.status_code != 200:
        raise Exception(
            f"Groq API error (HTTP {response.status_code}): {response.text}"
        )

    data = response.json()
    return data["choices"][0]["message"]["content"].strip()


def _draft_with_gemini(system_prompt: str, user_prompt: str) -> str:
    """Call Gemini API with automatic model fallback."""
    from gemini_client import generate_with_fallback, GEMINI_MODEL_LABELS

    combined_prompt = f"{system_prompt}\n\n{user_prompt}"
    text, model_used = generate_with_fallback(contents=combined_prompt)

    # Update the module-level label so metadata reflects the actual model used
    global MODEL_NAME_GEMINI
    MODEL_NAME_GEMINI = model_used

    print(f"[draft_machine] Gemini draft used model: {GEMINI_MODEL_LABELS.get(model_used, model_used)}")
    return text


def _draft_with_openrouter(system_prompt: str, user_prompt: str) -> str:
    """Call OpenRouter API with automatic model fallback."""
    from openrouter_client import draft_with_openrouter, OPENROUTER_MODEL_LABELS

    text, model_used = draft_with_openrouter(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
    )

    global MODEL_NAME_OPENROUTER
    MODEL_NAME_OPENROUTER = model_used
    print(f"[draft_machine] OpenRouter draft used: {OPENROUTER_MODEL_LABELS.get(model_used, model_used)}")
    return text


def draft_reply_with_metadata(thread: dict, model_choice: str = "groq") -> dict:
    """
    Returns a dict with:
      - draft: the generated reply text
      - model: model name used
      - subject: thread subject
      - replying_to: name of the last sender in the thread
    """
    draft = draft_reply(thread, model_choice)
    if model_choice == "groq":
        model_name = MODEL_NAME_GROQ
    elif model_choice == "gemini":
        model_name = MODEL_NAME_GEMINI
    else:
        model_name = MODEL_NAME_OPENROUTER
    messages = thread.get("messages", [])
    last_sender = messages[-1].get("from", "Unknown") if messages else "Unknown"

    return {
        "draft": draft,
        "model": model_name,
        "subject": thread.get("subject", "(no subject)"),
        "replying_to": last_sender,
    }


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    sample_thread = {
        "subject": "Q3 Budget Review — Final Call",
        "messages": [
            {
                "from": "Ananya Mehta",
                "date": "2026-06-17",
                "body": "Hi Rahul, we need to finalise the Q3 budget by Friday. Can you share your team's revised numbers by EOD tomorrow? We're particularly interested in the engineering resourcing costs."
            },
            {
                "from": "Rahul Sharma",
                "date": "2026-06-17",
                "body": "Hey Ananya, I'm reviewing the latest headcount projections with Engineering. I'll have the numbers to you by tomorrow afternoon."
            },
            {
                "from": "Ananya Mehta",
                "date": "2026-06-18",
                "body": "Thanks Rahul. Just a reminder — the CFO has pushed the deadline to Thursday EOD. Also, please include any contractor costs you're anticipating for Q3."
            }
        ]
    }

    groq_key = os.environ.get("GROQ_API_KEY", "")
    gemini_key = os.environ.get("GEMINI_API_KEY", "")

    print("=" * 60)
    print("GENERATING DRAFT REPLY")
    print("=" * 60)

    if groq_key:
        result = draft_reply_with_metadata(sample_thread, "groq")
        print(f"[Groq] Model:       {result['model']}")
        print(f"[Groq] Subject:     {result['subject']}")
        print(f"[Groq] Replying to: {result['replying_to']}")
        print("-" * 60)
        print(result["draft"])
        print("=" * 60)
    else:
        print("[Groq] Skipped — no GROQ_API_KEY")

    if gemini_key:
        result = draft_reply_with_metadata(sample_thread, "gemini")
        print(f"[Gemini] Model:       {result['model']}")
        print(f"[Gemini] Subject:     {result['subject']}")
        print(f"[Gemini] Replying to: {result['replying_to']}")
        print("-" * 60)
        print(result["draft"])
        print("=" * 60)
    else:
        print("[Gemini] Skipped — no GEMINI_API_KEY")