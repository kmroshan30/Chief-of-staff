from gmail_fetch import fetch_threads
from triage import triage_inbox
from digest import format_digest

def main():
    print("📥 Fetching your last 20 threads...")
    threads = fetch_threads(max_results=20)
    print(f"✓ Got {len(threads)} threads.\n")

    print("🤖 Classifying with multi-provider fallback (Gemini → Groq → OpenRouter)...")
    results = triage_inbox(threads)
    print("✓ Classification complete.\n")

    digest = format_digest(results)
    print(digest)

    with open("digest_output.txt", "w", encoding="utf-8") as f:
        f.write(digest)
    print("✓ Saved to digest_output.txt")

if __name__ == "__main__":
    main()