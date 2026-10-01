#!/bin/bash
# Chat · hector.app — Caddy (TLS terminator) launcher
# Sources the local secrets file (mode 600) then execs the custom Caddy
# binary (built with caddy-dns/porkbun for DNS-01 challenges).
set -a
source $HOME/Documents/Workspaces/ui/.env
set +a
exec $HOME/Documents/Workspaces/ui/bin/caddy run --config $HOME/Documents/Workspaces/ui/Caddyfile
