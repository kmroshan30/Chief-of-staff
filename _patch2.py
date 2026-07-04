import re

content = open('app.py', encoding='utf-8').read()

# The bad pattern introduced by the previous patch — 3 different indent levels
# Find each occurrence and normalise it

# Pattern to match the 3-line block regardless of leading spaces
bad_pattern = re.compile(
    r'( +)_method = result\.get\(.method_used., .mcp.\)\n'
    r' +_method_label = .*?\n'
    r' +st\.success\(f"✅ Reply sent \{_method_label\}! ID: `\{result\.get\(.message_id., .\'\'\)\}`"\)'
)

def fixer(m):
    indent = m.group(1)
    return (
        f"{indent}_method = result.get('method_used', 'mcp')\n"
        f"{indent}_method_label = '(via MCP)' if _method == 'mcp' else '(via Direct Gmail API)'\n"
        f"{indent}st.success(f\"✅ Reply sent {{_method_label}}! ID: `{{result.get('message_id', '')}}`\")"
    )

fixed, n = bad_pattern.subn(fixer, content)
print(f"Fixed {n} occurrences")
open('app.py', 'w', encoding='utf-8').write(fixed)
print("Done")
