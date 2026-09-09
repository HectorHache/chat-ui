#!/bin/bash
# ============================================================================
# Watchdog — Chat·hector.app (Phase 5, B24)
#
# Probes the REAL endpoints (not process listings — root daemons are invisible
# to non-root sandboxes; B24) and writes a status file + alert log.
#
# Checks:
#   1. https://chat.hector.app/health        (Caddy TLS + OWUI, tailnet)
#   2. https://localhost:8383/health        (Caddy TLS + OWUI, local)
#   3. http://127.0.0.1:8484/health         (model bridge: AR + Vertex)
#   4. Credit-expiry warning window (2026-11-15 .. 2026-11-22) — once/day
#
# Outputs:
#   <DATA_DIR>/watchdog/status.json    — latest check (read by anything)
#   <DATA_DIR>/watchdog/alerts.log     — append-only alert trail
#
# Notifications: macOS notification center via osascript (best-effort, only on
# transition healthy->unhealthy or credit-warning day). No external deps.
# ============================================================================
set -u

DATA_DIR="${DATA_DIR:-/Users/mick/Documents/Workspaces/ui/data}"
WD_DIR="$DATA_DIR/watchdog"
mkdir -p "$WD_DIR"

STATUS_FILE="$WD_DIR/status.json"
ALERT_LOG="$WD_DIR/alerts.log"

CHAT_HEALTH="https://chat.hector.app/health"
LOCAL_HEALTH="https://localhost:8383/health"
BRIDGE_HEALTH="http://127.0.0.1:8484/health"

NOW_EPOCH=$(date +%s)
NOW_ISO=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
TODAY=$(date +%Y-%m-%d)

# --- helpers ---------------------------------------------------------------
probe() { # probe <url> <timeout> -> 0/1
  local url="$1" timeout="${2:-8}"
  curl -s -m "$timeout" -o /dev/null -w "%{http_code}" "$url" 2>/dev/null | grep -q "^200$"
}

notify() { # notify <title> <message>
  osascript -e "display notification \"$2\" with title \"$1\"" 2>/dev/null || true
}

log_alert() { # log_alert <line>
  echo "$NOW_ISO $1" >> "$ALERT_LOG"
}

# --- checks ----------------------------------------------------------------
chat_ok=0; probe "$CHAT_HEALTH" 8 && chat_ok=1
local_ok=0; probe "$LOCAL_HEALTH" 8 && local_ok=1
bridge_ok=0
BRIDGE_KEY=$(grep '^BRIDGE_API_KEY=' /Users/mick/Documents/Workspaces/ui/.env | cut -d= -f2)
bridge_json=$(curl -s -m 8 -H "Authorization: Bearer $BRIDGE_KEY" "$BRIDGE_HEALTH" 2>/dev/null || echo "")
case "$bridge_json" in
  *'"status":"ok"'*) bridge_ok=1 ;;
esac

overall="ok"
if [ "$chat_ok" -eq 1 ] && [ "$local_ok" -eq 1 ] && [ "$bridge_ok" -eq 1 ]; then
  overall="ok"
else
  overall="degraded"
fi

# --- credit-expiry warning (2026-11-15 .. 2026-11-22, once per day) --------
credit_warn=0
CREDIT_START="2026-11-15"
CREDIT_END="2026-11-22"
if [[ "$TODAY" > "$CREDIT_START" || "$TODAY" == "$CREDIT_START" ]] && [[ "$TODAY" < "$CREDIT_END" || "$TODAY" == "$CREDIT_END" ]]; then
  marker="$WD_DIR/credit-warn-$TODAY.marker"
  if [ ! -f "$marker" ]; then
    credit_warn=1
    touch "$marker"
    msg="AgentRouter credits expire 2026-11-22 — top up soon (daily reminder)."
    log_alert "CREDIT-WARN $msg"
    notify "Chat·hache credits" "$msg"
  fi
fi

# --- status file -----------------------------------------------------------
cat > "$STATUS_FILE" <<EOF
{
  "checked_at": "$NOW_ISO",
  "overall": "$overall",
  "chat_hector_app": ${chat_ok},
  "localhost_8383": ${local_ok},
  "bridge_8484": ${bridge_ok},
  "credit_warning_active": ${credit_warn}
}
EOF

# --- alert on transition (compare with previous status) --------------------
if [ "$overall" = "degraded" ]; then
  prev=$(cat "$WD_DIR/previous-overall" 2>/dev/null || echo "ok")
  if [ "$prev" != "degraded" ]; then
    msg="chat.hector.app=${chat_ok} localhost:8383=${local_ok} bridge:8484=${bridge_ok}"
    log_alert "DEGRADED $msg"
    notify "Chat·hache watchdog" "Service degraded: $msg"
  fi
fi
echo "$overall" > "$WD_DIR/previous-overall"

exit 0
