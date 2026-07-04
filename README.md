# Chief of Staff — Draft Desk

An AI-powered email triage and drafting agent that reads your inbox, understands context, drafts replies in your own voice, and books meetings — with a human-in-the-loop approval step before anything is ever sent.

Built as part of an MCP (Model Context Protocol) cohort project, exploring how multiple AI services and tool servers can be orchestrated together into a single working agent.

## What it does

- **Fetches and triages emails** from Gmail, classifying what needs a reply, what can wait, and what's noise
- **Drafts replies** using Gemini/Groq, matched to a custom tone profile trained on the user's own writing style
- **Human-in-the-loop approval** — no email goes out without explicit sign-off, keeping a human in control of the final send
- **Books meetings** directly to Google Calendar when a reply requires scheduling
- **Sends replies** through a Gmail MCP server, with direct Gmail API access as a fallback path
- **Logs actions** for traceability — every draft, approval, and send is recorded

## Architecture

```
Gmail (fetch) → Triage (Gemini/Groq) → Draft Machine (tone-matched draft)
                                              ↓
                                    Approval Gate (human review)
                                              ↓
                          Gmail MCP Server (send) ──→ Calendar Engine (if scheduling)
                                              ↓
                                        Task Logger
```

The core idea: rather than one model doing everything, each step is handled by the right tool — a triage model, a drafting model, an MCP server for sending, and a direct API as a safety net. The human stays in the loop at the one point that matters most: before anything leaves the inbox.

## Tech stack

| Layer | Tool |
|---|---|
| Email | Gmail API + Gmail MCP Server |
| Scheduling | Google Calendar API |
| Drafting / reasoning | Gemini (`gemini-2.5-flash`), Groq |
| Interface | Streamlit |
| Orchestration | Python |

## Project structure

```
chief_of_staff/
├── app.py                  # Streamlit interface
├── main.py                 # Entry point
├── triage.py                # Email classification
├── draft_machine.py         # Reply generation
├── engine.py                 # Core orchestration logic
├── approval_gate.py          # Human-in-the-loop approval
├── calendar_engine.py        # Google Calendar booking
├── gmail_fetch.py             # Direct Gmail API access
├── gemini_client.py           # Gemini API wrapper
├── openrouter_client.py       # OpenRouter API wrapper
├── context_builder.py         # Builds context for drafting
├── digest.py                   # Daily/periodic email digest
├── task_logger.py               # Action logging
├── tone_profile.json             # User's writing style profile
└── gmail-mcp-server/               # MCP server for Gmail actions
```

## Setup

1. **Clone the repo**
   ```bash
   git clone https://github.com/kmroshan30/Chief-of-staff.git
   cd chief_of_staff
   ```

2. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

3. **Set up environment variables**

   Create a `.env.local` file (not committed — see `.gitignore`):
   ```
   GEMINI_API_KEY=your_key_here
   GROQ_API_KEY=your_key_here
   ```

4. **Set up Google credentials**

   - Create a Google Cloud project and enable the Gmail API and Calendar API
   - Download OAuth credentials as `credentials.json` (project root, not committed)
   - For the MCP server, place OAuth keys at `gmail-mcp-server/gcp-oauth.keys.json` (not committed)

5. **Run the app**
   ```bash
   streamlit run app.py
   ```

   On first run, you'll be prompted to authorize Gmail and Calendar access in the browser.

## Why this project

This was built to explore **AI orchestration** — how to compose multiple models, APIs, and tool servers (MCP) into one coherent agent rather than relying on a single model call. Key design decisions along the way:

- MCP-based sending as the primary path (keeping the architecture consistent with the MCP cohort's focus), with direct Gmail API as fallback
- A dedicated tone profile so drafts sound like the user, not like a generic AI assistant
- An explicit approval gate, since automated email sending without human review is a real-world risk

## Notes

- Secrets (`credentials.json`, `token.json`, `.env.local`, `gcp-oauth.keys.json`) are excluded via `.gitignore` and must be set up locally
- Two OAuth scopes are involved: Gmail (`gmail.modify`, `gmail.readonly`) and Calendar — Calendar authorization runs separately on first "Book Meeting" use
