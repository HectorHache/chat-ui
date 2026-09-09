#!/bin/bash
# Chat · hector.app — regenerate launchd plists + root config from .env
# Usage: scripts/gen-plists.sh
# Emits:
#   ~/Library/LaunchAgents/org.hache.chat.openwebui.plist    (user agent, env from .env)
#   /opt/homebrew/etc/chat-hache/Caddyfile                    (TCC-safe copy for root daemon)
#   /opt/homebrew/etc/chat-hache/certs/*.pem                  (TCC-safe cert copies)
#   /tmp/org.hache.chat.caddy.root.plist                     (root daemon plist;
#                                                             install: sudo cp + bootout/bootstrap)
# .env stays the single source of truth for secrets.
#
# TCC note (verified 2026-08-31): the ROOT daemon cannot read/write anything under
# ~/Documents (macOS TCC). Its config, certs and logs therefore live outside:
#   config+certs -> /opt/homebrew/etc/chat-hache/   (brew dir, user-writable, root-readable)
#   logs         -> /var/log/chat-hache/            (created with sudo once)

set -euo pipefail
cd "$(dirname "$0")/.."
ENV_FILE="$(pwd)/.env"
OWUI_BIN="$(pwd)/env/bin/open-webui"
CADDY_BIN="$(pwd)/bin/caddy"
SRC_CADDY_CONF="$(pwd)/Caddyfile"
ROOT_ETC="/opt/homebrew/etc/chat-hache"
ROOT_CONF="${ROOT_ETC}/Caddyfile"
DATA_DIR="$(pwd)/data"
LOGS="$(pwd)/logs"
ROOT_LOGS="/var/log/chat-hache"
SECRET_KEY_FILE="$(pwd)/.webui_secret_key"

# 1) Stage TCC-safe copies for the root daemon
mkdir -p "${ROOT_ETC}/certs"
cp -f "${SRC_CADDY_CONF}" "${ROOT_CONF}"
cp -f "$(pwd)/certs/localhost.pem" "${ROOT_ETC}/certs/"
cp -f "$(pwd)/certs/localhost-key.pem" "${ROOT_ETC}/certs/"
chmod 644 "${ROOT_ETC}/certs/"*
echo "root config staged: ${ROOT_CONF}"

# Build EnvironmentVariables XML block from a given list of keys
env_block() {
  local keys=("$@")
  local first=1
  for k in "${keys[@]}"; do
    local v
    v=$(grep -E "^${k}=" "$ENV_FILE" | head -1 | sed -E 's/^[^=]*=//' | sed -E 's/[[:space:]]*#.*$//')
    if [ "${v#\"}" != "$v" ] && [ "${v%\"}" != "$v" ]; then v="${v#\"}"; v="${v%\"}"; fi
    if [ "${v#\'}" != "$v" ] && [ "${v%\'}" != "$v" ]; then v="${v#\'}"; v="${v%\'}"; fi
    if [ "${v#\"}" != "$v" ] && [ "${v%\"}" != "$v" ]; then v="${v#\"}"; v="${v%\"}"; fi
    if [ "${v#\'}" != "$v" ] && [ "${v%\'}" != "$v" ]; then v="${v#\'}"; v="${v%\'}"; fi
    if [ -n "${v:-}" ]; then
      if [ "$first" -eq 1 ]; then
        printf '\t<key>EnvironmentVariables</key>\n\t<dict>\n'
        first=0
      fi
      printf '\t\t<key>%s</key>\n\t\t<string>%s</string>\n' "$k" "$v"
    fi
  done
  if [ "$first" -eq 0 ]; then
    printf '\t</dict>\n'
  fi
}

# ---- Open WebUI (user LaunchAgent) ----
cat > "$HOME/Library/LaunchAgents/org.hache.chat.openwebui.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>org.hache.chat.openwebui</string>
	<key>ProgramArguments</key>
	<array>
		<string>${OWUI_BIN}</string>
		<string>serve</string>
		<string>--host</string>
		<string>127.0.0.1</string>
		<string>--port</string>
		<string>8390</string>
	</array>
$(env_block DATA_DIR WEBUI_SECRET_KEY_FILE OAUTH_CLIENT_ID OAUTH_CLIENT_SECRET OPENID_PROVIDER_URL WEBUI_URL ENABLE_OAUTH_SIGNUP ENABLE_PASSWORD_AUTH OPENAI_API_BASE_URLS OPENAI_API_KEYS WEBUI_NAME CORS_ALLOW_ORIGIN)
	<key>WorkingDirectory</key>
	<string>$(pwd)</string>
	<key>RunAtLoad</key>
	<true/>
	<key>KeepAlive</key>
	<true/>
	<key>StandardOutPath</key>
	<string>${LOGS}/openwebui.out.log</string>
	<key>StandardErrorPath</key>
	<string>${LOGS}/openwebui.err.log</string>
</dict>
</plist>
EOF
echo "openwebui plist -> $HOME/Library/LaunchAgents/org.hache.chat.openwebui.plist"

# ---- Model bridge (user LaunchAgent, 127.0.0.1:8484) ----
# Reads .env itself (loadDotEnv in bridge/server.mjs) — only the node binary
# path and working dir come from the plist. Restart-on-crash via KeepAlive.
BRIDGE_BIN="$(command -v /opt/homebrew/bin/node || command -v node || echo /opt/homebrew/bin/node)"
cat > "$HOME/Library/LaunchAgents/org.hache.chat.bridge.plist" <<BRIDGEEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>org.hache.chat.bridge</string>
	<key>ProgramArguments</key>
	<array>
		<string>${BRIDGE_BIN}</string>
		<string>${PWD}/bridge/server.mjs</string>
	</array>
	<key>WorkingDirectory</key>
	<string>${PWD}</string>
	<key>RunAtLoad</key>
	<true/>
	<key>KeepAlive</key>
	<true/>
	<key>StandardOutPath</key>
	<string>${LOGS}/bridge.out.log</string>
	<key>StandardErrorPath</key>
	<string>${LOGS}/bridge.err.log</string>
</dict>
</plist>
BRIDGEEOF
echo "bridge plist -> $HOME/Library/LaunchAgents/org.hache.chat.bridge.plist"

# ---- Whisper local STT worker (B40) ----
cat > "$HOME/Library/LaunchAgents/org.hache.chat.whisper.plist" <<WHISPEREOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>org.hache.chat.whisper</string>
	<key>ProgramArguments</key>
	<array>
		<string>${PWD}/env/bin/python</string>
		<string>${PWD}/scripts/whisper_worker.py</string>
		<string>--preload</string>
	</array>
	<key>WorkingDirectory</key>
	<string>${PWD}</string>
	<key>RunAtLoad</key>
	<true/>
	<key>KeepAlive</key>
	<true/>
	<key>StandardOutPath</key>
	<string>${LOGS}/whisper.out.log</string>
	<key>StandardErrorPath</key>
	<string>${LOGS}/whisper.err.log</string>
</dict>
</plist>
WHISPEREOF
echo "whisper plist -> $HOME/Library/LaunchAgents/org.hache.chat.whisper.plist"

# ---- Caddy (root LaunchDaemon, staged in /tmp; needs sudo to install) ----
cat > /tmp/org.hache.chat.caddy.root.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>org.hache.chat.caddy</string>
	<key>ProgramArguments</key>
	<array>
		<string>${CADDY_BIN}</string>
		<string>run</string>
		<string>--config</string>
		<string>${ROOT_CONF}</string>
	</array>
$(env_block PORKBUN_API_KEY PORKBUN_API_SECRET CHAT_TAILNET_IP XDG_DATA_HOME)
	<key>WorkingDirectory</key>
	<string>/</string>
	<key>RunAtLoad</key>
	<true/>
	<key>KeepAlive</key>
	<true/>
	<key>StandardOutPath</key>
	<string>${ROOT_LOGS}/caddy.out.log</string>
	<key>StandardErrorPath</key>
	<string>${ROOT_LOGS}/caddy.err.log</string>
</dict>
</plist>
EOF
echo "caddy root plist -> /tmp/org.hache.chat.caddy.root.plist"
