"""
draft_desk.py
The Draft Desk — Chief of Staff AI Agent
Orchestrates: Gmail fetch → triage → context injection → drafting → approval gate
Run: streamlit run draft_desk.py
"""

import os
import json
import re
import streamlit as st
from dotenv import load_dotenv

# Load .env.local first
load_dotenv(".env.local")

# Startup key check (printed to terminal, not shown in UI)
groq_key = os.environ.get("GROQ_API_KEY", "")
gemini_key = os.environ.get("GEMINI_API_KEY", "")
print(f"[Init] GROQ_API_KEY: {'found ✓' if groq_key else 'missing ✗'}")
print(f"[Init] GEMINI_API_KEY: {'found ✓' if gemini_key else 'missing ✗'}")

# Also check st.secrets as fallback
try:
    if not groq_key:
        groq_key = st.secrets.get("GROQ_API_KEY", "")
        if groq_key:
            os.environ["GROQ_API_KEY"] = groq_key
    if not gemini_key:
        gemini_key = st.secrets.get("GEMINI_API_KEY", "")
        if gemini_key:
            os.environ["GEMINI_API_KEY"] = gemini_key
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not openrouter_key:
        openrouter_key = st.secrets.get("OPENROUTER_API_KEY", "")
        if openrouter_key:
            os.environ["OPENROUTER_API_KEY"] = openrouter_key
except (FileNotFoundError, AttributeError):
    pass  # No secrets.toml — keys come from .env.local only

openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
print(f"[Init] OPENROUTER_API_KEY: {'found ✓' if openrouter_key else 'missing ✗'}")

# Import project modules
from context_builder import assemble_context        # assemble_context(thread) -> {"system": str, "user": str}
from draft_machine import draft_reply               # draft_reply(thread, model_choice) -> str
from approval_gate import save_approved_draft       # save_approved_draft(thread, draft_text) -> None
from engine import fetch_threads                    # fetch_threads(max_results) -> list[dict]
import triage                                       # triage.triage_inbox(threads) -> list[dict] with "priority" key
import digest                                       # digest.format_digest(results) -> str
from task_logger import log_action, get_action_log

# ---------------------------------------------------------------------------
# Sample threads fallback (used when Gmail is unavailable)
# ---------------------------------------------------------------------------
SAMPLE_THREADS = [
    {
        "subject": "Q3 Budget Review — Final Call",
        "priority": "urgent",
        "messages": [
            {"from": "Ananya Mehta <ananya.mehta@company.com>", "date": "2026-06-17",
             "body": "Hi Rahul, we need to finalise the Q3 budget by Friday. Can you share revised numbers by EOD tomorrow? We're interested in engineering resourcing costs."},
            {"from": "Rahul Sharma", "date": "2026-06-17",
             "body": "Hey Ananya, reviewing headcount projections with Engineering. Numbers to you by tomorrow afternoon."},
            {"from": "Ananya Mehta <ananya.mehta@company.com>", "date": "2026-06-18",
             "body": "Thanks Rahul. CFO pushed the deadline to Thursday EOD. Please include contractor costs for Q3."}
        ]
    },
    {
        "subject": "Proposed changes to the onboarding flow",
        "priority": "needs-reply",
        "messages": [
            {"from": "Priya K. <priya.k@company.com>", "date": "2026-06-15",
             "body": "Hey Rahul, I've drafted a revised onboarding flow that cuts step 3 entirely. Thoughts?"},
            {"from": "Rahul Sharma", "date": "2026-06-15",
             "body": "Looks promising — let me take a closer look and get back to you tomorrow."},
            {"from": "Priya K. <priya.k@company.com>", "date": "2026-06-16",
             "body": "Sure, no rush. I've attached the latest mockups for reference."}
        ]
    }
]

PRIORITY_ICONS = {
    "urgent": "🔴",
    "needs-reply": "🟡",
    "fyi": "🔵",
    "ignore": "⚪"
}

PHASES = ["inbox", "draft", "approval", "export"]
PHASE_LABELS = {
    "inbox": "Inbox & Triage",
    "draft": "Draft Generation",
    "approval": "Approval Gate",
    "export": "Export Proof",
}


# ---------------------------------------------------------------------------
# Helper: extract recipient info from a thread dict
# ---------------------------------------------------------------------------

def _get_recipient_info(thread: dict) -> tuple[str, str]:
    """
    Extract (display_name, email_address) from a thread dict.
    Works for both Gmail threads (top-level 'sender') and sample threads ('messages' list).
    """
    raw = thread.get("sender", "") or ""
    if not raw:
        messages = thread.get("messages", [])
        raw = messages[-1].get("from", "") if messages else ""
    email_match = re.search(r"<([^>]+)>", raw)
    recipient = email_match.group(1) if email_match else raw
    display_name = raw.replace(f"<{recipient}>", "").strip(" <>") if email_match else raw
    return display_name, recipient


def _get_thread_sender(thread: dict) -> str:
    """Get the original sender display safely."""
    raw = thread.get("sender", "") or ""
    if not raw:
        messages = thread.get("messages", [])
        if messages:
            raw = messages[0].get("from", "")
    # Strip email brackets for display
    email_match = re.search(r"<([^>]+)>", raw)
    if email_match:
        return raw.replace(f"<{email_match.group(1)}>", "").strip(" <>") or email_match.group(1)
    return raw


def _pick_first_drafted_thread(
    triaged: list[dict], drafts: dict[str, str]
) -> tuple[dict | None, str | None]:
    """Return the first triaged thread that has a generated draft."""
    for thread in triaged:
        thread_id = thread.get("thread_id", "")
        if thread_id in drafts:
            return thread, drafts[thread_id]
    return None, None


def _activate_draft_for_thread(thread: dict, draft_text: str) -> None:
    """Set session state so the approval gate can review a specific draft."""
    st.session_state.active_thread = thread
    st.session_state.draft = draft_text
    st.session_state.gate_status = "none"
    st.session_state.edited_draft = ""


# #region agent log
def _debug_log(location: str, message: str, data: dict, hypothesis_id: str) -> None:
    import time
    try:
        with open("debug-72abbe.log", "a", encoding="utf-8") as _f:
            _f.write(json.dumps({
                "sessionId": "72abbe",
                "location": location,
                "message": message,
                "data": data,
                "hypothesisId": hypothesis_id,
                "timestamp": int(time.time() * 1000),
                "runId": "post-fix",
            }) + "\n")
    except OSError:
        pass
# #endregion


# ---------------------------------------------------------------------------
# Cached helpers
# ---------------------------------------------------------------------------

@st.cache_resource
def _get_send_reply():
    """Return the send_reply callable, cached to avoid re-imports."""
    from engine import send_reply
    return send_reply


@st.cache_resource
def _get_calendar_engine():
    """Return the calendar_engine module, cached to avoid re-imports."""
    import calendar_engine as ce
    return ce


@st.cache_resource
def _get_draft_reply():
    """Return the draft_reply callable, cached to avoid re-imports."""
    from draft_machine import draft_reply
    return draft_reply


@st.cache_data(ttl=3600)
def _cached_parse_meeting_request(thread_json: str) -> str:
    """
    Cache Gemini parse results keyed by thread JSON string.
    Returns JSON string of parsed result to avoid re-parsing the same thread.
    """
    import calendar_engine as ce
    thread = json.loads(thread_json)
    result = ce.parse_meeting_request(thread)
    return json.dumps(result)


# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
st.set_page_config(page_title="The Draft Desk", page_icon="🖋️", layout="wide")

st.markdown("""
<style>
    .stApp { background-color: #1a1a2e; color: #e0e0e0; }
    section[data-testid="stSidebar"] { background-color: #16213e; }
    h1, h2, h3 { color: #e0e0e0; }
    .stMarkdown p { color: #c0c0c0; }
    .msg-card {
        background: #16213e;
        border-left: 3px solid #0f3460;
        border-radius: 6px;
        padding: 12px 16px;
        margin-bottom: 10px;
        color: #e0e0e0;
    }
    .msg-sender { color: #e94560; font-weight: 600; }
    .msg-date { color: #888; font-size: 0.8rem; margin-left: 8px; }
    .msg-body { margin-top: 6px; line-height: 1.5; white-space: pre-wrap; }
    .draft-box {
        background: #16213e;
        border: 1px solid #0f3460;
        border-radius: 8px;
        padding: 20px;
        color: #e0e0e0;
        font-size: 1rem;
        line-height: 1.6;
        white-space: pre-wrap;
    }
    .approved-banner {
        background: #1b4332; border: 1px solid #2d6a4f;
        color: #95d5b2; padding: 12px 20px;
        border-radius: 8px; font-weight: 600; text-align: center;
    }
    .rejected-banner {
        background: #4a1c1c; border: 1px solid #8b2d2d;
        color: #e5989b; padding: 12px 20px;
        border-radius: 8px; font-weight: 600; text-align: center;
    }
    .digest-output {
        background-color: #111; color: #0f0;
        font-family: monospace; padding: 15px;
        border-radius: 5px; white-space: pre;
    }
    .stats-text {
        color: #888;
        font-size: 0.8rem;
        line-height: 1.6;
    }
    .nav-btn-active {
        font-weight: 700;
    }
    .sidebar-subtitle {
        color: #a0a0a0;
        font-size: 0.85rem;
        margin-top: -10px;
        margin-bottom: 0px;
    }
</style>
""", unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Session State Init
# ---------------------------------------------------------------------------
defaults = {
    # Phase navigation
    "phase": "inbox",
    # Threads & triage
    "threads": [],
    "triaged": set(),
    "gmail_used": False,
    "fetch_done": False,
    # Drafting
    "drafts": {},
    "active_thread": None,
    "draft": None,
    "model_choice": "groq",
    # Approval gate
    "gate_status": "none",    # none | editing | approved | rejected
    "edited_draft": "",
    "approved": set(),
    "rejected": set(),
    "sent": set(),
    "booked": {},
    # Track last processed thread to avoid stale state
    "last_drafted_thread_id": None,
    # Pipeline automation
    "pipeline_running": False,
    "pipeline_log": [],
}
for k, v in defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v


# ---------------------------------------------------------------------------
# Pipeline helpers
# ---------------------------------------------------------------------------

def load_sample_threads() -> list[dict]:
    """Return the sample threads with a unique thread_id for each."""
    threads = []
    for i, t in enumerate(SAMPLE_THREADS):
        thread = dict(t)
        thread.setdefault("thread_id", f"sample_{i}")
        threads.append(thread)
    return threads


def fetch_threads_via_engine(max_results: int = 10) -> list[dict]:
    """Fetch raw threads from Gmail via engine.py (no triage — pipeline handles that)."""
    from engine import fetch_threads as _engine_fetch
    raw = _engine_fetch(max_results=max_results)
    return raw


# ---------------------------------------------------------------------------
# Full pipeline: fetch → triage → draft → approval gate
# ---------------------------------------------------------------------------

def run_full_pipeline() -> list[str]:
    """
    Execute the full pipeline: fetch threads, triage, draft replies for
    urgent and needs-reply threads, and advance to the Approval Gate phase.

    Returns a list of log strings capturing each step's outcome.
    """
    log: list[str] = []

    # (1) Determine the thread source from session state
    source = st.session_state.get("source", "gmail")
    log.append(f"[pipeline] Source: {source}")

    # (2) Fetch threads
    try:
        if source == "gmail":
            fetched = fetch_threads_via_engine(max_results=10)
            log.append(f"[pipeline] Fetched {len(fetched)} thread(s) via Gmail engine")
        else:
            fetched = load_sample_threads()
            log.append(f"[pipeline] Loaded {len(fetched)} sample thread(s)")

        if not fetched:
            log.append("[pipeline] WARNING: no threads fetched, aborting pipeline")
            st.session_state.threads = []
            st.session_state.fetch_done = False
            return log
    except Exception as e:
        log.append(f"[pipeline] ERROR fetching threads: {e}")
        st.session_state.threads = []
        st.session_state.fetch_done = False
        return log

    # (3) Triage the threads (adds priority / category / reason keys)
    try:
        triaged = triage.triage_inbox(fetched)
        st.session_state.threads = triaged
        st.session_state.triaged = set(
            t.get("thread_id", "") for t in triaged
        )
        st.session_state.fetch_done = True
        log.append(f"[pipeline] Triaged {len(triaged)} thread(s)")
    except Exception as e:
        log.append(f"[pipeline] ERROR during triage: {e}")
        # Still store whatever we have
        st.session_state.threads = fetched
        st.session_state.fetch_done = True
        return log

    # (4) Reset all downstream session state
    try:
        st.session_state.drafts = {}
        st.session_state.approved = set()
        st.session_state.rejected = set()
        st.session_state.sent = set()
        st.session_state.booked = {}
        st.session_state.draft = None
        st.session_state.active_thread = None
        st.session_state.gate_status = "none"
        st.session_state.edited_draft = ""
        log.append("[pipeline] Reset downstream state (drafts, approved, rejected, sent, booked)")
    except Exception as e:
        log.append(f"[pipeline] ERROR resetting state: {e}")

    # (5) Draft replies for urgent + needs-reply threads
    priority_threads = [
        t for t in triaged
        if t.get("priority") in ("urgent", "needs-reply")
    ]
    log.append(f"[pipeline] {len(priority_threads)} thread(s) need a draft (urgent + needs-reply)")

    draft_fn = _get_draft_reply()
    model_choice = st.session_state.get("model_choice", "groq")

    for t in priority_threads:
        thread_id = t.get("thread_id", "")
        subject = t.get("subject", "(no subject)")
        try:
            draft_text = draft_fn(t, model_choice)
            st.session_state.drafts[thread_id] = draft_text
            log.append(f"[pipeline] ✓ Drafted reply for: \"{subject}\" (thread_id={thread_id})")
        except Exception as e:
            log.append(f"[pipeline] ✗ Draft FAILED for: \"{subject}\" (thread_id={thread_id}): {e}")
            # Continue to the next thread — one failure doesn't stop the pipeline

    # (6) Set phase to Approval Gate and activate first draft for review
    try:
        active, draft_text = _pick_first_drafted_thread(triaged, st.session_state.drafts)
        if active and draft_text:
            _activate_draft_for_thread(active, draft_text)
            log.append(
                f"[pipeline] Activated draft for: \"{active.get('subject', '')}\" "
                f"(thread_id={active.get('thread_id', '')})"
            )
        st.session_state.phase = "approval"
        st.session_state.current_phase = "approval"
        log.append("[pipeline] Set phase to 'Approval Gate'")
        # #region agent log
        _debug_log(
            "app.py:run_full_pipeline",
            "pipeline complete",
            {
                "drafts_count": len(st.session_state.drafts),
                "draft_set": st.session_state.draft is not None,
                "active_thread_id": (st.session_state.active_thread or {}).get("thread_id", ""),
            },
            "A",
        )
        # #endregion
    except Exception as e:
        log.append(f"[pipeline] ERROR setting phase: {e}")

    return log


# ---------------------------------------------------------------------------
# Pipeline execution UI (live progress)
# ---------------------------------------------------------------------------

def _render_pipeline_execution() -> None:
    """
    Run the full pipeline logic inline with live progress UI using st.status().
    Does NOT call run_full_pipeline() — duplicates the logic so it can update
    the Streamlit UI at each step.
    """
    pipeline_log: list[str] = []
    source = st.session_state.get("source", "gmail")
    pipeline_log.append(f"[pipeline] Source: {source}")

    status = st.status("Running full pipeline...", expanded=True)

    # --- Step 1: Fetch ---
    status.update(label="📥 Step 1/3: Fetching threads...")
    fetched: list[dict] = []
    fetch_ok = False
    try:
        if source == "gmail":
            fetched = fetch_threads_via_engine(max_results=10)
            pipeline_log.append(f"[pipeline] Fetched {len(fetched)} thread(s) via Gmail engine")
        else:
            fetched = load_sample_threads()
            pipeline_log.append(f"[pipeline] Loaded {len(fetched)} sample thread(s)")

        if not fetched:
            status.write("❌ No threads fetched — aborting")
            status.update(state="error")
            st.session_state.pipeline_log = pipeline_log
            st.session_state.pipeline_running = False
            return

        fetch_ok = True
        status.write(f"✅ Fetched {len(fetched)} thread(s)")
    except Exception as e:
        pipeline_log.append(f"[pipeline] ERROR fetching threads: {e}")
        status.write(f"❌ Fetch failed: {e}")
        status.update(state="error")
        st.session_state.threads = []
        st.session_state.fetch_done = False
        st.session_state.pipeline_log = pipeline_log
        st.session_state.pipeline_running = False
        return

    # --- Step 2: Triage ---
    status.update(label="🔍 Step 2/3: Triaging threads...")
    triaged: list[dict] = []
    try:
        triaged = triage.triage_inbox(fetched)
        st.session_state.threads = triaged
        st.session_state.triaged = set(
            t.get("thread_id", "") for t in triaged
        )
        st.session_state.fetch_done = True
        pipeline_log.append(f"[pipeline] Triaged {len(triaged)} thread(s)")
        status.write(f"✅ Triaged {len(triaged)} thread(s)")
    except Exception as e:
        pipeline_log.append(f"[pipeline] ERROR during triage: {e}")
        status.write(f"❌ Triage failed: {e}")
        status.update(state="error")
        st.session_state.threads = fetched
        st.session_state.fetch_done = True
        st.session_state.pipeline_log = pipeline_log
        st.session_state.pipeline_running = False
        return

    # Reset downstream state before drafting
    st.session_state.drafts = {}
    st.session_state.approved = set()
    st.session_state.rejected = set()
    st.session_state.sent = set()
    st.session_state.booked = {}
    st.session_state.draft = None
    st.session_state.active_thread = None
    st.session_state.gate_status = "none"
    st.session_state.edited_draft = ""

    # --- Step 3: Draft loop ---
    status.update(label="✍️ Step 3/3: Generating drafts...")

    priority_threads = [
        t for t in triaged
        if t.get("priority") in ("urgent", "needs-reply")
    ]
    pipeline_log.append(
        f"[pipeline] {len(priority_threads)} thread(s) need a draft (urgent + needs-reply)"
    )

    draft_fn = _get_draft_reply()
    model_choice = st.session_state.get("model_choice", "groq")

    draft_success_count = 0
    draft_fail_count = 0

    for t in priority_threads:
        thread_id = t.get("thread_id", "")
        subject = t.get("subject", "(no subject)")
        try:
            draft_text = draft_fn(t, model_choice)
            st.session_state.drafts[thread_id] = draft_text
            pipeline_log.append(
                f"[pipeline] ✓ Drafted reply for: \"{subject}\" (thread_id={thread_id})"
            )
            status.write(f"✅ Drafted: \"{subject}\"")
            draft_success_count += 1
        except Exception as e:
            pipeline_log.append(
                f"[pipeline] ✗ Draft FAILED for: \"{subject}\" (thread_id={thread_id}): {e}"
            )
            status.write(f"❌ Draft failed: \"{subject}\" — {e}")
            draft_fail_count += 1
            # Continue to the next thread

    # Summary line
    if draft_fail_count > 0:
        status.write(
            f"⚠️ {draft_success_count} drafted, {draft_fail_count} failed"
        )
    else:
        status.write(f"✅ All {draft_success_count} draft(s) completed successfully")

    status.update(
        label=f"✅ Pipeline complete — {draft_success_count} draft(s) generated",
        state="complete",
    )

    # --- Post-pipeline cleanup ---
    st.session_state.pipeline_log = pipeline_log
    active, draft_text = _pick_first_drafted_thread(triaged, st.session_state.drafts)
    if active and draft_text:
        _activate_draft_for_thread(active, draft_text)
        pipeline_log.append(
            f"[pipeline] Activated draft for: \"{active.get('subject', '')}\" "
            f"(thread_id={active.get('thread_id', '')})"
        )
    st.session_state.phase = "approval"
    st.session_state.current_phase = "approval"
    st.session_state.pipeline_running = False
    # #region agent log
    _debug_log(
        "app.py:_render_pipeline_execution",
        "pipeline UI complete",
        {
            "drafts_count": len(st.session_state.drafts),
            "draft_set": st.session_state.draft is not None,
            "active_thread_id": (st.session_state.active_thread or {}).get("thread_id", ""),
        },
        "A",
    )
    # #endregion
    st.rerun()


# ---------------------------------------------------------------------------
# Phase render stubs
# ---------------------------------------------------------------------------

def render_inbox_phase():
    """Inbox & Triage phase — fetch and triage threads."""
    st.header("📥 Inbox & Triage")
    st.markdown("Fetch and triage your inbox threads.")
    st.markdown("---")

    if st.button("📬 Fetch Threads", type="primary", use_container_width=False):
        with st.spinner("Fetching threads..."):
            source = st.session_state.get("source", "gmail")
            if source == "gmail":
                raw_threads = fetch_threads(max_results=10)
                gmail_used = bool(raw_threads)
            else:
                raw_threads = load_sample_threads()
                gmail_used = False

            if raw_threads:
                enriched = triage.triage_inbox(raw_threads)
                st.session_state.threads = enriched
                st.session_state.gmail_used = gmail_used
            else:
                fallback = load_sample_threads()
                st.session_state.threads = triage.triage_inbox(fallback)
                st.session_state.gmail_used = False

            st.session_state.fetch_done = True
            st.session_state.triaged = set(
                t.get("thread_id", "") for t in st.session_state.threads
            )
            # #region agent log
            _debug_log(
                "app.py:render_inbox_phase",
                "threads fetched",
                {
                    "count": len(st.session_state.threads),
                    "has_reason": all("reason" in t for t in st.session_state.threads),
                    "has_thread_id": all(t.get("thread_id") for t in st.session_state.threads),
                },
                "F",
            )
            # #endregion
            st.rerun()

    if st.session_state.fetch_done:
        count = len(st.session_state.threads)
        source_label = "Gmail" if st.session_state.gmail_used else "Sample"
        st.success(f"✅ {source_label}: {count} thread(s) loaded")

        # Show digest-style summary
        try:
            digest_output = digest.format_digest(st.session_state.threads)
            st.markdown(
                f'<div class="digest-output">{digest_output}</div>',
                unsafe_allow_html=True
            )
        except Exception as e:
            st.error(f"Digest error: {e}")
    else:
        st.info("Click **📬 Fetch Threads** above to begin.")


def render_draft_phase():
    """Draft Generation phase — select thread and generate a draft."""
    st.header("✍️ Draft Generation")
    st.markdown("Select a thread and generate a draft reply.")
    st.markdown("---")

    if not st.session_state.fetch_done or not st.session_state.threads:
        st.info("👈 Go to **Inbox & Triage** first to fetch threads.")
        return

    threads = st.session_state.threads

    # Filter to actionable threads
    priority_threads = [
        t for t in threads
        if t.get("priority") in ("urgent", "needs-reply")
    ]

    if not priority_threads:
        st.info("No urgent or needs-reply threads. Check the Inbox & Triage tab.")
        return

    col_left, col_right = st.columns([1, 1])

    with col_left:
        st.subheader("📧 Thread")

        labels = [
            f"{PRIORITY_ICONS.get(t.get('priority','fyi'), '❔')} {t.get('subject', '(no subject)')}"
            for t in priority_threads
        ]
        selected_label = st.selectbox("Select thread to draft:", labels, key="draft_thread_selector")
        selected_idx = labels.index(selected_label)
        active_thread = priority_threads[selected_idx]

        # Show thread metadata
        display_sender = _get_thread_sender(active_thread)
        st.markdown(f"**From:** {display_sender}")
        st.markdown(f"**Subject:** {active_thread.get('subject', '')}")
        st.markdown(f"**Priority:** {PRIORITY_ICONS.get(active_thread.get('priority','fyi'))} {active_thread.get('priority','').upper()}")
        st.markdown("---")

        # Show messages — works for both Gmail threads (top-level fields) and sample threads (messages list)
        msgs = active_thread.get("messages", [])
        if msgs:
            for msg in msgs:
                st.markdown(f"""
                <div class="msg-card">
                    <span class="msg-sender">{msg.get('from','Unknown')}</span>
                    <span class="msg-date">{msg.get('date','')}</span>
                    <div class="msg-body">{msg.get('body','')}</div>
                </div>
                """, unsafe_allow_html=True)
        else:
            # Gmail thread: show snippet as body
            st.markdown(f"""
            <div class="msg-card">
                <span class="msg-sender">{display_sender}</span>
                <span class="msg-date">{active_thread.get('date', '')}</span>
                <div class="msg-body">{active_thread.get('snippet', '')}</div>
            </div>
            """, unsafe_allow_html=True)

        # Show recipient info on who we'd be replying to
        disp_name, recipient_email = _get_recipient_info(active_thread)
        st.markdown(f"**Replying to:** {disp_name} <{recipient_email}>")

        if st.button("✍️ Draft Reply", type="primary", use_container_width=True):
            model_choice = st.session_state.get("model_choice", "groq")
            # Validate the right API key is present for the chosen model
            _key_map = {
                "groq":        ("GROQ_API_KEY",        "Groq"),
                "gemini":      ("GEMINI_API_KEY",       "Gemini"),
                "openrouter":  ("OPENROUTER_API_KEY",   "OpenRouter"),
            }
            _env_var, _model_name = _key_map.get(model_choice, ("GROQ_API_KEY", "Groq"))
            if not os.environ.get(_env_var):
                st.error(f"❌ {_model_name} API key not found. Add {_env_var} to .env.local.")
                st.stop()

            with st.spinner(f"Drafting with {model_choice.title()}..."):
                try:
                    draft_text = draft_reply(active_thread, model_choice)
                    thread_id = active_thread.get("thread_id", "")
                    # Reset approval state for fresh draft
                    st.session_state.draft = draft_text
                    st.session_state.active_thread = active_thread
                    st.session_state.gate_status = "none"
                    st.session_state.edited_draft = ""
                    st.session_state.drafts[thread_id] = draft_text
                    st.session_state.last_drafted_thread_id = thread_id
                    # Clear old approval state for this thread
                    if thread_id in st.session_state.approved:
                        st.session_state.approved.discard(thread_id)
                    if thread_id in st.session_state.rejected:
                        st.session_state.rejected.discard(thread_id)
                    if thread_id in st.session_state.sent:
                        st.session_state.sent.discard(thread_id)
                    st.rerun()
                except Exception as e:
                    st.error(f"Draft failed: {e}")

    with col_right:
        st.subheader("📝 Draft Preview")
        if st.session_state.draft is None:
            st.info("Select a thread and click **✍️ Draft Reply** to generate.")
        else:
            # Show who this draft is responding to
            active = st.session_state.active_thread
            if active:
                disp, email = _get_recipient_info(active)
                st.markdown(f"**To:** {disp} <{email}>")
                st.markdown(f"**Subject:** Re: {active.get('subject', '')}")
                st.markdown("---")
            st.markdown(
                f'<div class="draft-box">{st.session_state.draft}</div>',
                unsafe_allow_html=True
            )
            st.markdown("---")
            if st.button("➡️ Proceed to Approval Gate", type="primary", use_container_width=True):
                st.session_state.phase = "approval"
                st.rerun()


def render_approval_phase():
    """Approval Gate phase — approve, edit, reject, and send drafts."""
    st.header("✅ Approval Gate")
    st.markdown("Approve, edit, reject, or send your drafted replies.")
    st.markdown("---")

    # --- Pipeline execution log (visible if pipeline was just run) ---
    pipeline_log = st.session_state.get("pipeline_log", [])
    if pipeline_log:
        with st.expander("📋 Pipeline Execution Log", expanded=False):
            for entry in pipeline_log:
                if "ERROR" in entry or "FAILED" in entry:
                    st.write(f"❌ {entry}")
                else:
                    st.write(f"✅ {entry}")
            if st.button("Clear log", key="clear_pipeline_log"):
                st.session_state.pipeline_log = []
                st.rerun()
        st.markdown("---")

    drafts = st.session_state.get("drafts", {})
    threads_with_drafts = [
        t for t in st.session_state.threads
        if t.get("thread_id") in drafts
    ]

    if st.session_state.draft is None and threads_with_drafts:
        first = threads_with_drafts[0]
        _activate_draft_for_thread(first, drafts[first.get("thread_id", "")])

    if st.session_state.draft is None:
        if drafts:
            st.warning(
                f"{len(drafts)} draft(s) exist but could not be matched to loaded threads. "
                "Re-run the pipeline or fetch threads again."
            )
        else:
            st.info("👈 Go to **Draft Generation** first to create a draft, or run **🚀 Run Full Pipeline**.")
        return

    if len(threads_with_drafts) > 1:
        draft_labels = [
            f"{PRIORITY_ICONS.get(t.get('priority', 'fyi'), '❔')} "
            f"{t.get('subject', '(no subject)')}"
            for t in threads_with_drafts
        ]
        current_id = (st.session_state.active_thread or {}).get("thread_id", "")
        current_idx = next(
            (i for i, t in enumerate(threads_with_drafts) if t.get("thread_id") == current_id),
            0,
        )
        picked_label = st.selectbox(
            "Select draft to review:",
            draft_labels,
            index=current_idx,
            key="approval_draft_selector",
        )
        picked_thread = threads_with_drafts[draft_labels.index(picked_label)]
        picked_id = picked_thread.get("thread_id", "")
        if picked_id != current_id:
            _activate_draft_for_thread(picked_thread, drafts[picked_id])
            st.rerun()

    # #region agent log
    _debug_log(
        "app.py:render_approval_phase",
        "approval gate active",
        {
            "draft_set": st.session_state.draft is not None,
            "drafts_count": len(drafts),
            "active_thread_id": (st.session_state.active_thread or {}).get("thread_id", ""),
        },
        "A",
    )
    # #endregion

    status = st.session_state.gate_status
    active_thread = st.session_state.active_thread
    thread_id = active_thread.get("thread_id", "") if active_thread else ""

    # --- GATE: none (initial state after drafting) ---
    if status == "none":
        st.subheader("📝 Draft Review")
        # Show recipient info
        if active_thread:
            disp, email = _get_recipient_info(active_thread)
            st.markdown(f"**To:** {disp} **<{email}>**")
            st.markdown(f"**Subject:** Re: {active_thread.get('subject', '')}")
            st.markdown("---")
        st.markdown(
            f'<div class="draft-box">{st.session_state.draft}</div>',
            unsafe_allow_html=True
        )
        st.markdown("---")
        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button("✅ Approve", use_container_width=True, type="primary"):
                save_approved_draft(
                    st.session_state.active_thread,
                    st.session_state.draft
                )
                st.session_state.gate_status = "approved"
                st.session_state.approved.add(thread_id)
                st.rerun()
        with c2:
            if st.button("✏️ Edit", use_container_width=True):
                st.session_state.gate_status = "editing"
                st.session_state.edited_draft = st.session_state.draft
                st.rerun()
        with c3:
            if st.button("❌ Reject", use_container_width=True):
                st.session_state.gate_status = "rejected"
                st.session_state.rejected.add(thread_id)
                st.rerun()

    # --- GATE: editing ---
    elif status == "editing":
        st.subheader("✏️ Edit Your Reply")
        edited = st.text_area(
            "Edit reply",
            value=st.session_state.edited_draft,
            height=220,
            label_visibility="collapsed"
        )
        st.session_state.edited_draft = edited
        e1, e2 = st.columns(2)
        with e1:
            if st.button("✅ Confirm Edit", type="primary", use_container_width=True):
                st.session_state.draft = edited
                save_approved_draft(st.session_state.active_thread, edited)
                st.session_state.gate_status = "approved"
                st.session_state.approved.add(thread_id)
                st.rerun()
        with e2:
            if st.button("↩️ Cancel", use_container_width=True):
                st.session_state.gate_status = "none"
                st.rerun()

    # --- GATE: approved ---
    elif status == "approved":
        already_sent = thread_id in st.session_state.sent
        thread_category = active_thread.get("_category", "") or active_thread.get("category", "")
        is_meeting_request = thread_category == "meeting-request"
        already_booked = thread_id in st.session_state.booked

        if already_sent:
            st.markdown(
                '<div class="approved-banner">📬 Sent</div>',
                unsafe_allow_html=True
            )
        else:
            st.markdown(
                '<div class="approved-banner">✅ Approved & saved to approved_drafts.json</div>',
                unsafe_allow_html=True
            )

        # Show recipient + draft
        disp, recipient = _get_recipient_info(active_thread)
        st.markdown(f"**To:** {disp} **<{recipient}>**")
        st.markdown(f"**Subject:** Re: {active_thread.get('subject', '')}")
        st.markdown("---")
        st.markdown(
            f'<div class="draft-box">{st.session_state.draft}</div>',
            unsafe_allow_html=True
        )

        if not already_sent:
            subject = active_thread.get("subject", "")
            body = st.session_state.draft

            # --- Recipient info card ---
            st.markdown("### 📬 Send Confirmation")
            with st.container():
                st.markdown(f"""
                <div style="background: #1a2e1a; border: 1px solid #2d6a4f; border-radius: 8px; padding: 16px; margin-bottom: 12px;">
                    <div style="color: #95d5b2; font-weight: 700; margin-bottom: 8px;">📤 Sending Email</div>
                    <table style="width: 100%; border-collapse: collapse; color: #e0e0e0;">
                        <tr><td style="padding: 4px 8px; font-weight: 600; width: 100px; color: #a0a0a0;">To:</td>
                            <td style="padding: 4px 8px;"><{recipient}> {disp}</td></tr>
                        <tr><td style="padding: 4px 8px; font-weight: 600; color: #a0a0a0;">Subject:</td>
                            <td style="padding: 4px 8px;">Re: {subject}</td></tr>
                        <tr><td style="padding: 4px 8px; font-weight: 600; color: #a0a0a0;">Thread ID:</td>
                            <td style="padding: 4px 8px;"><code style="color: #95d5b2;">{thread_id}</code></td></tr>
                    </table>
                </div>
                """, unsafe_allow_html=True)

            # --- Body preview ---
            with st.expander("📄 Preview email body", expanded=True):
                st.markdown(
                    f'<div class="draft-box" style="background: #1a1a2e; max-height: 250px; overflow-y: auto;">{body}</div>',
                    unsafe_allow_html=True
                )

            # --- For meeting-request threads: Send + Book Meeting side by side ---
            if is_meeting_request and not already_booked:
                st.markdown("---")
                st.markdown("**📅 Meeting Request**")
                col_send, col_book = st.columns(2)

                with col_send:
                    st.markdown("**Send Reply**")
                    st.markdown("By clicking below, this email will be sent immediately via your Gmail account.")
                    if st.button("✉️ Send Reply", type="primary", use_container_width=True):
                        with st.spinner("Sending reply..."):
                            try:
                                send_reply_fn = _get_send_reply()
                                result = send_reply_fn(
                                    thread_id=thread_id,
                                    to=recipient,
                                    subject=active_thread.get("subject", ""),
                                    body=st.session_state.draft,
                                )
                                if result.get("status") == "sent":
                                    st.session_state.sent.add(thread_id)
                                    log_action(
                                        action_type="sent",
                                        thread_subject=active_thread.get("subject", ""),
                                        detail=recipient,
                                        action_id=result.get("message_id", ""),
                                    )
                                    _method = result.get('method_used', 'mcp')
                                    _method_label = '(via MCP)' if _method == 'mcp' else '(via Direct Gmail API)'
                                    st.success(f"✅ Reply sent {_method_label}! ID: `{result.get('message_id', '')}`")
                                    st.rerun()
                                else:
                                    st.error(f"❌ Send failed: {result.get('error', str(result))}")
                            except Exception as e:
                                st.error(f"❌ Send failed: {e}")

                with col_book:
                    st.markdown("**Book Calendar Event**")
                    st.markdown("Parse the thread and create a Google Calendar event.")
                    st.caption("💡 First time: a browser window will open to authorise Google Calendar access.")
                    if st.button("📅 Book Meeting", type="primary", use_container_width=True):
                        with st.spinner("Parsing meeting request with Gemini..."):
                            try:
                                # Use cached parsing to minimize Gemini calls
                                thread_json = json.dumps(active_thread, default=str)
                                parsed_json = _cached_parse_meeting_request(thread_json)
                                parsed = json.loads(parsed_json)

                                if "parsing_error" in parsed:
                                    st.error(f"❌ Parsing failed: {parsed['parsing_error']}")
                                else:
                                    # Show extracted details in an info box
                                    st.info(
                                        f"**Extracted Details:**\n\n"
                                        f"**Topic:** {parsed.get('topic', '(unknown)')}\n\n"
                                        f"**Proposed Times:** {', '.join(parsed.get('proposed_times', [])) or 'none'}\n\n"
                                        f"**Attendees:** {', '.join(parsed.get('attendees', [])) or 'none'}\n\n"
                                        f"**Duration:** {parsed.get('duration_minutes', 30)} min"
                                    )

                                    proposed = parsed.get("proposed_times", [])
                                    if not proposed:
                                        st.warning("⚠️ No proposed times found in the thread.")
                                    else:
                                        ce = _get_calendar_engine()
                                        duration = parsed.get("duration_minutes", 30)

                                        with st.spinner("Checking calendar availability..."):
                                            free_slot = ce.find_free_slot(proposed, duration)

                                        if free_slot is None:
                                            st.warning("⚠️ None of the proposed times are free on your calendar.")
                                        else:
                                            attendees = parsed.get("attendees", [])
                                            topic = parsed.get("topic", active_thread.get("subject", "Meeting"))

                                            with st.spinner("Creating calendar event..."):
                                                created = ce.create_event(
                                                    summary=topic,
                                                    start_time=free_slot,
                                                    duration_minutes=duration,
                                                    attendees=attendees,
                                                    description=f"Auto-created from email thread: {active_thread.get('subject', '')}",
                                                )

                                            st.session_state.booked[thread_id] = created
                                            if created.get("id"):
                                                log_action(
                                                    action_type="booked",
                                                    thread_subject=active_thread.get("subject", ""),
                                                    detail=parsed.get("topic", active_thread.get("subject", "")),
                                                    action_id=created["id"],
                                                )
                                            event_link = created.get("htmlLink", "")
                                            st.success(f"✅ Event created! [📅 View in Calendar]({event_link})")
                                            st.rerun()
                            except Exception as e:
                                err_str = str(e)
                                if "scope" in err_str.lower() or "auth" in err_str.lower() or "credentials" in err_str.lower():
                                    st.error(
                                        f"❌ Google Calendar auth failed: {e}\n\n"
                                        "Run `python calendar_engine.py` in the terminal once to complete authorisation."
                                    )
                                else:
                                    st.error(f"❌ Book Meeting failed: {e}")

                if st.button("❌ Cancel", use_container_width=True):
                    st.session_state.gate_status = "none"
                    st.rerun()

            elif already_booked:
                booked_event = st.session_state.booked.get(thread_id, {})
                event_link = booked_event.get("htmlLink", "")
                st.markdown("---")
                if event_link:
                    st.success(f"✅ Meeting booked! [📅 View in Calendar]({event_link})")
                else:
                    st.success("✅ Meeting booked!")

                col_confirm, col_cancel = st.columns(2)
                with col_confirm:
                    confirm_send = st.button("✉️ Send Reply", type="primary", use_container_width=True)
                with col_cancel:
                    cancel_send = st.button("❌ Cancel", use_container_width=True)

                if confirm_send:
                    with st.spinner("Sending reply..."):
                        try:
                            send_reply_fn = _get_send_reply()
                            result = send_reply_fn(
                                thread_id=thread_id,
                                to=recipient,
                                subject=active_thread.get("subject", ""),
                                body=st.session_state.draft,
                            )
                            if result.get("status") == "sent":
                                st.session_state.sent.add(thread_id)
                                log_action(
                                    action_type="sent",
                                    thread_subject=active_thread.get("subject", ""),
                                    detail=recipient,
                                    action_id=result.get("message_id", ""),
                                )
                                _method = result.get('method_used', 'mcp')
                                _method_label = '(via MCP)' if _method == 'mcp' else '(via Direct Gmail API)'
                                st.success(f"✅ Reply sent {_method_label}! ID: `{result.get('message_id', '')}`")
                                st.rerun()
                            else:
                                st.error(f"❌ Send failed: {result.get('error', str(result))}")
                        except Exception as e:
                            st.error(f"❌ Send failed: {e}")

                if cancel_send:
                    st.session_state.gate_status = "none"
                    st.rerun()

            else:
                # Non-meeting thread: standard send flow with <to> notation
                st.markdown("---")
                st.markdown(f"**⚠️ Final Permission Required — sending to <{recipient}>**")
                st.markdown("By clicking **Confirm Send**, this email will be sent immediately via your Gmail account.")

                col_confirm, col_cancel = st.columns(2)
                with col_confirm:
                    confirm_send = st.button("✅ Confirm Send", type="primary", use_container_width=True)
                with col_cancel:
                    cancel_send = st.button("❌ Cancel", use_container_width=True)

                if confirm_send:
                    with st.spinner("Sending reply..."):
                        try:
                            send_reply_fn = _get_send_reply()
                            result = send_reply_fn(
                                thread_id=thread_id,
                                to=recipient,
                                subject=active_thread.get("subject", ""),
                                body=st.session_state.draft,
                            )
                            if result.get("status") == "sent":
                                st.session_state.sent.add(thread_id)
                                log_action(
                                    action_type="sent",
                                    thread_subject=active_thread.get("subject", ""),
                                    detail=recipient,
                                    action_id=result.get("message_id", ""),
                                )
                                _method = result.get('method_used', 'mcp')
                                _method_label = '(via MCP)' if _method == 'mcp' else '(via Direct Gmail API)'
                                st.success(f"✅ Reply sent {_method_label}! ID: `{result.get('message_id', '')}`")
                                st.rerun()
                            else:
                                st.error(f"❌ Send failed: {result.get('error', str(result))}")
                        except Exception as e:
                            st.error(f"❌ Send failed: {e}")

                if cancel_send:
                    st.session_state.gate_status = "none"
                    st.rerun()

        # --- After send / already sent: option to draft next thread ---
        if st.button("🔄 Draft Another Thread", use_container_width=True):
            # Reset all draft state for fresh start
            st.session_state.draft = None
            st.session_state.gate_status = "none"
            st.session_state.active_thread = None
            st.session_state.edited_draft = ""
            st.session_state.phase = "draft"
            st.rerun()

        # Also allow going back to inbox to fetch fresh threads
        if st.button("📬 Back to Inbox", use_container_width=True):
            st.session_state.draft = None
            st.session_state.gate_status = "none"
            st.session_state.active_thread = None
            st.session_state.phase = "inbox"
            st.rerun()

    # --- GATE: rejected ---
    elif status == "rejected":
        st.markdown(
            '<div class="rejected-banner">❌ Draft discarded.</div>',
            unsafe_allow_html=True
        )
        if st.button("🔄 Try Again", use_container_width=True):
            st.session_state.draft = None
            st.session_state.gate_status = "none"
            st.rerun()


def render_export_phase():
    """Export Proof phase — show approved drafts, sent items, etc."""
    st.header("📤 Export Proof")
    st.markdown("Review and export proof of approved drafts and sent replies.")
    st.markdown("---")

    # --- Approved Drafts ---
    from approval_gate import load_approved_drafts, APPROVED_DRAFTS_FILE
    approved = load_approved_drafts()

    st.subheader("✅ Approved Drafts")
    if not approved:
        st.info("No approved drafts yet. Approve a draft in the Approval Gate to see it here.")
    else:
        for i, entry in enumerate(approved):
            subject = entry.get("subject", "(no subject)")
            timestamp = entry.get("timestamp", "")
            draft_text = entry.get("approved_draft", "")
            messages = entry.get("messages", [])

            with st.expander(f"📧 {subject}  ·  {timestamp[:16].replace('T', ' ')}", expanded=False):
                # Original thread messages
                if messages:
                    st.markdown("**Original Thread:**")
                    for msg in messages:
                        sender = msg.get("from", "Unknown")
                        date_str = msg.get("date", "")
                        body = msg.get("body", "")
                        st.markdown(
                            f'<div class="msg-card">'
                            f'<span class="msg-sender">{sender}</span>'
                            f'<span class="msg-date">{date_str}</span>'
                            f'<div class="msg-body">{body}</div>'
                            f'</div>',
                            unsafe_allow_html=True,
                        )
                    st.markdown("---")

                # Approved draft
                st.markdown("**Approved Draft:**")
                st.markdown(
                    f'<div class="draft-box">{draft_text}</div>',
                    unsafe_allow_html=True,
                )

        st.markdown("---")

        # Download button for full JSON export
        import json as _json
        export_bytes = _json.dumps(approved, indent=2, ensure_ascii=False).encode("utf-8")
        st.download_button(
            label="⬇️ Download approved_drafts.json",
            data=export_bytes,
            file_name="approved_drafts.json",
            mime="application/json",
            use_container_width=False,
        )

    st.markdown("---")

    # --- Action Log ---
    st.subheader("📋 Action Log")

    entries = get_action_log()

    if not entries:
        st.info("No actions logged yet.")
    else:
        # Header row
        h1, h2, h3, h4 = st.columns([1, 3, 3, 2])
        h1.markdown("**Type**")
        h2.markdown("**Subject**")
        h3.markdown("**Detail**")
        h4.markdown("**Time**")
        st.markdown('<hr style="margin: 4px 0 8px 0; border-color: #0f3460;">', unsafe_allow_html=True)

        for entry in reversed(entries):  # newest first
            action_type = entry.get("action_type", "")
            subject = entry.get("thread_subject", "(no subject)")
            detail = entry.get("detail", "")
            timestamp = entry.get("timestamp", "")
            action_id = entry.get("id", "")

            # Format icon + label
            if action_type == "sent":
                type_label = "📨 SENT"
            elif action_type == "booked":
                type_label = "📅 BOOKED"
            else:
                type_label = action_type.upper()

            # Format timestamp: "Jun 29 02:30 PM"
            try:
                from datetime import datetime as _dt
                ts = _dt.fromisoformat(timestamp)
                formatted_ts = ts.strftime("%b %d %I:%M %p")
            except (ValueError, TypeError):
                formatted_ts = timestamp[:16].replace("T", " ")

            c1, c2, c3, c4 = st.columns([1, 3, 3, 2])
            c1.markdown(type_label)
            c2.markdown(f"**{subject}**")
            c3.markdown(f"`{detail}`")
            c4.caption(formatted_ts)


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
with st.sidebar:
    st.title("🤝 The Draft Desk")
    st.markdown('<p class="sidebar-subtitle">Chief of Staff — Draft workflow</p>', unsafe_allow_html=True)
    st.markdown("---")

    # --- PIPELINE SECTION ---
    if st.button("🚀 Run Full Pipeline", type="primary", use_container_width=True):
        st.session_state.pipeline_running = True
        st.rerun()
    st.caption("Fetches, triages, and drafts — stops at Approval Gate.")
    st.markdown("---")

    # --- SOURCE SECTION ---
    st.markdown("**Source**")
    source = st.radio(
        "Source selection",
        ["Sample threads", "Gmail via engine.py"],
        index=1,
        label_visibility="collapsed",
        key="source_radio",
    )
    st.session_state["source"] = "sample" if "Sample" in source else "gmail"

    if st.session_state["source"] == "gmail":
        st.caption("Pulls live threads via engine.fetch_threads().")

    # Model selector (inline)
    model_label = st.radio(
        "🤖 Drafting Model",
        ["Groq — llama-3.3-70b", "Gemini — auto fallback", "OpenRouter — auto fallback"],
        index=0,
        key="model_radio",
    )
    if "Groq" in model_label:
        st.session_state["model_choice"] = "groq"
    elif "Gemini" in model_label:
        st.session_state["model_choice"] = "gemini"
    else:
        st.session_state["model_choice"] = "openrouter"

    # Show fallback chain caption for Gemini
    if st.session_state["model_choice"] == "gemini":
        from gemini_client import GEMINI_MODELS, GEMINI_MODEL_LABELS
        st.caption(
            "Fallback order: "
            + " → ".join(GEMINI_MODEL_LABELS[m] for m in GEMINI_MODELS)
        )
    # Show fallback chain caption for OpenRouter
    elif st.session_state["model_choice"] == "openrouter":
        from openrouter_client import OPENROUTER_MODELS, OPENROUTER_MODEL_LABELS
        st.caption(
            "Fallback order: "
            + " → ".join(OPENROUTER_MODEL_LABELS[m] for m in OPENROUTER_MODELS)
        )

    st.markdown("---")

    # --- NAVIGATION SECTION ---
    st.markdown("**Navigation**")

    for phase_key in PHASES:
        label = PHASE_LABELS[phase_key]
        is_active = st.session_state.get("phase") == phase_key
        btn_label = f"► {label}" if is_active else f"   {label}"

        if st.button(btn_label, use_container_width=True, key=f"nav_{phase_key}"):
            st.session_state.phase = phase_key
            st.rerun()

    st.markdown("---")

    # --- STATS FOOTER ---
    st.markdown("**Stats**")
    n_threads = len(st.session_state.threads)
    n_triaged = len(st.session_state.triaged)
    n_drafts = len(st.session_state.drafts)
    n_approved = len(st.session_state.approved)
    n_rejected = len(st.session_state.rejected)

    st.markdown(f"""
    <div class="stats-text">
        Loaded: {n_threads} thread(s)<br>
        Triaged: {n_triaged}<br>
        Drafts: {n_drafts}<br>
        Approved: {n_approved}<br>
        Rejected: {n_rejected}
    </div>
    """, unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Main Area — Phase Dispatch
# ---------------------------------------------------------------------------
if st.session_state.get("pipeline_running", False):
    _render_pipeline_execution()
else:
    current_phase = st.session_state.get("phase", "inbox")

    if current_phase == "inbox":
        render_inbox_phase()
    elif current_phase == "draft":
        render_draft_phase()
    elif current_phase == "approval":
        render_approval_phase()
    elif current_phase == "export":
        render_export_phase()