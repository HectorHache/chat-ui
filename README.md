# Chat·hector — local Open WebUI stack

A private chat stack behind `chat.hector.app` (tailnet-only): a self-hosted Open WebUI with a custom model bridge, voice transcription, a watchdog and launchd wiring. Real users at home, daily use, nothing leaves the device.

Repo scope is code and config only. Runtime state (databases, vector stores, audio, virtualenvs, keys, logs, certificates) is never committed. See `.gitignore`.

| Piece | What it is |
|---|---|
| `bridge.mjs` | OpenAI-compatible model bridge. Primary is Claude Opus 4.8 over the Anthropic messages API; the fallback is Vertex Gemini 3.8 (EU), which also serves the vision slot. Adds TTS, speech-to-text and embeddings. Zero npm dependencies. |
| `scripts/` | launchd generators, watchdog, whisper worker, backup, auto-update, tool and function registration. |
| `tools/` | the household tool library: calendar, weather, prices, shopping, reminders, recipes and more. |
| `Caddyfile` | TLS terminator config, DNS-01 over Cloudflare. |

## How the bridge routes

- `GET /v1/models` returns a small curated picker: Fast, Daily and Xtra (all Opus, tuned by answer length and effort) plus a Vision model.
- `POST /v1/chat/completions` speaks OpenAI format. Opus requests are translated to and from the Anthropic messages API. If the Opus upstream is unavailable, the bridge falls back to Vertex Gemini on its own, so a chat never errors out.
- It also serves `/v1/audio/speech` (TTS), `/v1/audio/transcriptions` (speech-to-text) and `/v1/embeddings` (retrieval).

Keys and project IDs are read from environment variables, with an optional local `.env`, never hardcoded. The bridge binds to loopback only.

## License

MIT. See `LICENSE`.
