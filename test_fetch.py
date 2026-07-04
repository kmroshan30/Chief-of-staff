from gmail_fetch import fetch_threads

threads = fetch_threads(max_results=5)
for t in threads:
    print(f"[{t['thread_id'][:8]}] {t['sender']}")
    print(f"  Subject: {t['subject']}")
    print(f"  Snippet: {t['snippet'][:60]}...")
    print()