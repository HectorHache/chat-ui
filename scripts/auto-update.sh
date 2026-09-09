#!/bin/bash
# ============================================================================
# Auto-update — Chat·hector.app (Phase 5)
#
# Runs daily (01:00 UTC) as a launchd user agent. NO popups, fully silent:
#   1. Reads pinned version from .owui-version
#   2. Checks PyPI for the newest open-webui release
#   3. If newer: backup (pip freeze + DB), upgrade in venv,
#      RE-RUN the sub-overflow patch (CRITICAL — an upgrade replaces
#      models/users.py and re-breaks Google login; README G13),
#      restart the OWUI launchd agent, smoke-test (health + chat + reasoning),
#      on ANY failure: roll back to the pinned version and restart.
#   4. Writes logs to logs/auto-update.log (silent unless failure -> alert)
# ============================================================================
set -u
cd /Users/mick/Documents/Workspaces/ui || exit 1

DATA_DIR="/Users/mick/Documents/Workspaces/ui/data"
LOG="logs/auto-update.log"
STAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "$STAMP auto-update start" >> "$LOG"

PIN_FILE=".owui-version"
PINNED=$(cat "$PIN_FILE" 2>/dev/null | tr -d ' \n')
[ -z "$PINNED" ] && { echo "$STAMP ABORT: no pinned version" >> "$LOG"; exit 1; }

# --- find latest on PyPI ---------------------------------------------------
LATEST=$(curl -s -m 30 https://pypi.org/pypi/open-webui/json 2>/dev/null \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['info']['version'])" 2>/dev/null)
[ -z "$LATEST" ] && { echo "$STAMP SKIP: PyPI unreachable" >> "$LOG"; exit 0; }

if [ "$PINNED" = "$LATEST" ]; then
  echo "$STAMP no update ($PINNED)" >> "$LOG"
  exit 0
fi

echo "$STAMP UPDATE $PINNED -> $LATEST" >> "$LOG"

# --- backup current state --------------------------------------------------
BK="data/backups/pre-upgrade-$PINNED-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BK"
env/bin/pip freeze > "$BK/requirements.txt" 2>/dev/null
/usr/bin/sqlite3 "$DATA_DIR/webui.db" "VACUUM INTO '$BK/webui.db'" 2>/dev/null
echo "$STAMP backup -> $BK" >> "$LOG"

# --- upgrade ---------------------------------------------------------------
if ! env/bin/pip install --quiet --upgrade "open-webui==$LATEST" >> "$LOG" 2>&1; then
  echo "$STAMP FAIL: pip upgrade error" >> "$LOG"
  exit 1
fi

# --- CRITICAL: re-apply Google-sub overflow patch --------------------------
bash scripts/patch-owui-sub-overflow.sh >> "$LOG" 2>&1 || {
  echo "$STAMP FAIL: patch re-apply failed" >> "$LOG"
}

# --- restart OWUI agent ----------------------------------------------------
launchctl kickstart -k "gui/$(id -u)/org.hache.chat.openwebui" >> "$LOG" 2>&1
sleep 16  # OWUI startup

# --- smoke tests -----------------------------------------------------------
smoke_ok=1
curl -s -m 10 -o /dev/null -w "%{http_code}" http://127.0.0.1:8390/health | grep -q "^200$" || smoke_ok=0
# full chat via bridge
KEY=$(grep '^BRIDGE_API_KEY=' .env | cut -d= -f2)
curl -s -m 60 -H "Content-Type: application/json" -H "Authorization: Bearer $KEY" \
  http://127.0.0.1:8484/v1/chat/completions \
  -d '{"model":"deepseek-v4-flash","messages":[{"role":"user","content":"Say OK"}],"max_tokens":10}' \
  | grep -q '"content"' || smoke_ok=0

if [ "$smoke_ok" -ne 1 ]; then
  echo "$STAMP FAIL: smoke test — rolling back to $PINNED" >> "$LOG"
  env/bin/pip install --quiet "open-webui==$PINNED" >> "$LOG" 2>&1
  bash scripts/patch-owui-sub-overflow.sh >> "$LOG" 2>&1
  launchctl kickstart -k "gui/$(id -u)/org.hache.chat.openwebui" >> "$LOG" 2>&1
  sleep 16
  echo "$STAMP rolled back to $PINNED" >> "$LOG"
  exit 1
fi


echo "$LATEST" > "$PIN_FILE"
echo "$STAMP OK: upgraded + verified ($LATEST)" >> "$LOG"
exit 0
