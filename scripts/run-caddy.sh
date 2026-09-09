#!/bin/bash
# Chat · hector.app — Caddy (TLS terminator) launcher
# Sources the local secrets file (mode 600) then execs the custom Caddy
# binary (built with caddy-dns/porkbun for DNS-01 challenges).
set -a
source /Users/mick/Documents/Workspaces/ui/.env
set +a
exec /Users/mick/Documents/Workspaces/ui/bin/caddy run --config /Users/mick/Documents/Workspaces/ui/Caddyfile
