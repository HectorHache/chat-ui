# Chat·hector — local Open WebUI stack

Personal chat stack behind `chat.hector.app` (tailnet-only): self-hosted
Open WebUI + model bridge + whisper + watchdog + launchd wiring.

**Repo scope:** code and config only. Runtime state (databases, vector stores,
audio, venvs, keys, logs) is never committed — see `.gitignore`.

| Piece | What it is |
|---|---|
| `bridge.mjs` | OpenAI-compatible model bridge (AgentRouter + Vertex fallback) |
| `scripts/` | launchd generators, watchdog, whisper worker, backup, auto-update, tools |
| `tools/` | small utilities (household tools, RAG smoke, tool registration) |
| `Caddyfile` | TLS terminator config (root daemon, DNS-01 via Cloudflare) |

Setup docs live in the local workspace; this repo is the sanitized public
mirror. MIT licensed.
