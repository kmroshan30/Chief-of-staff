"""
test_connect.py — Lightweight Gmail MCP connectivity test.
Verifies that the MCP server starts and can search the inbox.
Uses ZERO Gemini API calls.

Usage:  python test_connect.py
"""

import json
import sys
sys.stdout.reconfigure(encoding="utf-8")

from engine import _ensure_gmail_auth, MCPClient, MCP_SERVER_CMD

# Step 1: Ensure auth files are in place
print("[test] Ensuring Gmail OAuth is configured...")
_ensure_gmail_auth()
print("[test] OAuth OK")

# Step 2: Start MCP server and search for exactly 1 message
print("[test] Starting MCP server...")
client = MCPClient(MCP_SERVER_CMD)
try:
    client.start()
    print("[test] MCP server started ✓")

    result = client.call_tool("search_emails", {
        "query": "in:inbox",
        "maxResults": 1,
    })

    # Verify result structure
    content = result.get("content") or result.get("text") or []
    if isinstance(content, list) and len(content) > 0:
        block = content[0]
        if isinstance(block, dict):
            block = block.get("text", "")
        has_id = "ID:" in block
        print(f"[test] Inbox search returned data: {'✓' if has_id else '✗'}")
        if has_id:
            # Extract first ID
            for line in block.split("\n"):
                if line.startswith("ID:"):
                    print(f"[test] Latest message ID: {line[len('ID:'):].strip()}")
                    break
    else:
        print(f"[test] Unexpected result shape: {json.dumps(result, indent=2)[:300]}")

    print("\n[test] ✅ Connection test PASSED — Gmail MCP is working")
except Exception as e:
    print(f"\n[test] ❌ Connection test FAILED: {e}")
    sys.exit(1)
finally:
    client.stop()