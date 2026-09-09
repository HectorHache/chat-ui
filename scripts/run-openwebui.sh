#!/bin/bash
# Chat · hector.app — Open WebUI launcher
# Sources the local secrets file (mode 600) then execs the real binary,
# so launchd never holds the secrets and .env stays the single source.
set -a
source /Users/mick/Documents/Workspaces/ui/.env
set +a
export DATA_DIR=/Users/mick/Documents/Workspaces/ui/data
export WEBUI_SECRET_KEY_FILE=/Users/mick/Documents/Workspaces/ui/.webui_secret_key
exec /Users/mick/Documents/Workspaces/ui/env/bin/open-webui serve --host 127.0.0.1 --port 8390
