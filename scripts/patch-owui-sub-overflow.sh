#!/bin/bash
# Chat·hector.app — Open WebUI local patch: Google `sub` INTEGER overflow
#
# BUG: open_webui/models/users.py get_user_by_oauth_sub() binds int(sub) as a
# SQLite query parameter when the sub claim is numeric. Google's `sub` claims
# are 21-digit numbers which overflow SQLite's signed 64-bit INTEGER
# (max 9223372036854775807) → OverflowError: "Python int too large to convert
# to SQLite INTEGER" → OAuth login fails with generic "Error during OAuth
# process". String comparison is what actually matches (sub is stored as a
# JSON string), so the numeric branch is only needed for legacy numeric JSON
# storage — guard it by range.
#
# MUST be re-run after every Open WebUI upgrade (site-packages is replaced).
# Idempotent: detects the patch marker and skips if already applied.
set -euo pipefail

OWUI_DIR="${1:-$(cd "$(dirname "$0")/.." && pwd)/env/lib/python3.11/site-packages}"
F="$OWUI_DIR/open_webui/models/users.py"
MARKER="Guard: Google \`sub\` claims are 21-digit"

if [ ! -f "$F" ]; then
  echo "FATAL: $F not found" >&2
  exit 1
fi

if grep -qF "Guard: Google" "$F"; then
  echo "patch already applied — skipping"
  exit 0
fi

# Upstream fix check (open-webui >= 0.11.2): native 2**63-1 guard in
# get_user_by_oauth_sub — our patch becomes unnecessary.
if grep -qF "2**63 - 1" "$F"; then
  echo "upstream fix present (>=0.11.2) — no patch needed"
  exit 0
fi

OLD='            # SQLite preserves JSON numeric type here; Postgres ->> already compares numeric JSON as text.
            if session.get_bind().dialect.name == '"'"'sqlite'"'"' and sub.isdecimal():
                query = select(User).where(or_(sub_expr == sub, sub_expr == int(sub)))'
NEW='            # SQLite preserves JSON numeric type here; Postgres ->> already compares numeric JSON as text.
            # Guard: Google `sub` claims are 21-digit numbers that overflow SQLite'"'"'s
            # signed 64-bit INTEGER (max 9223372036854775807) when bound as a query
            # parameter. Only add the numeric comparison when it fits.
            if session.get_bind().dialect.name == '"'"'sqlite'"'"' and sub.isdecimal():
                sub_int = int(sub)
                if sub_int <= 9223372036854775807:
                    query = select(User).where(or_(sub_expr == sub, sub_expr == sub_int))'

python3 - "$F" "$OLD" "$NEW" <<'PYEOF'
import sys
path, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
src = open(path).read()
if old not in src:
    print(f"FATAL: pattern not found in {path} — version changed?", file=sys.stderr)
    sys.exit(2)
open(path, 'w').write(src.replace(old, new))
print(f"patched {path}")
PYEOF

# syntax check
python3 -c "import ast; ast.parse(open('$F').read())" && echo "syntax OK"

echo ""
echo "Restart Open WebUI to load the patch:"
echo "  launchctl bootout gui/\$(id -u)/org.hache.chat.openwebui; sleep 1;"
echo "  launchctl bootstrap gui/\$(id -u) ~/Library/LaunchAgents/org.hache.chat.openwebui.plist"
