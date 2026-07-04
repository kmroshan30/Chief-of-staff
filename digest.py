ICONS = {
    "urgent": "🔴",
    "needs-reply": "🟡",
    "fyi": "🔵",
    "ignore": "⚪"
}

def format_digest(results: list) -> str:
    counts = {}
    for r in results:
        counts[r.get("priority", "unknown")] = counts.get(r.get("priority", "unknown"), 0) + 1

    lines = [
        "=" * 40,
        "   YOUR INBOX DIGEST",
        f"   {len(results)} threads · generated now",
        "=" * 40,
        ""
    ]

    priority_order = ["urgent", "needs-reply", "fyi", "ignore"]

    for priority in priority_order:
        items = [r for r in results if r.get("priority") == priority]
        if not items:
            continue
        icon = ICONS.get(priority, "")
        lines.append(f"{icon} {priority.upper()} ({len(items)})")
        for item in items:
            # sender: top-level for Gmail threads, messages[0]['from'] for sample threads
            sender = item.get("sender", "")
            if not sender:
                msgs = item.get("messages", [])
                sender = msgs[0].get("from", "Unknown") if msgs else "Unknown"
            lines.append(f"  ▸ {item.get('subject', '(no subject)')}")
            lines.append(f"    {sender}")
            lines.append(f"    → {item.get('reason', '')}")
        lines.append("")

    return "\n".join(lines)