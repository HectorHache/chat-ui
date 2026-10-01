#!/usr/bin/env node
/**
 * Chat·hector.app — local model bridge (127.0.0.1:8484)
 *
 * OpenAI-compatible endpoint fronting:
 *   1. Opus upstream (primary)     — Claude Opus 4.8 via Anthropic /v1/messages
 *                                (its OpenAI route is Cloudflare-blocked here);
 *                                dual free keys (primary, secondary) with primary->secondary failover.
 *   2. Vertex Gemini (fallback)— Google SDK-style JWT auth, auto-refresh,
 *                                EU region (`eu` short name; Google changed
 *                                regional hostnames — `eu-aiplatform` and
 *                                `europe-west4-aiplatform` hosts are INVALID).
 *                                Also serves the Vision picker slot directly.
 *
 * Zero npm dependencies: node:http + global fetch + node:crypto.
 * Endpoints:
 *   GET  /health              → {status, opus, vertex, uptime}
 *   GET  /v1/models           → curated picker: Fast/Daily/Xtra (Opus) + Vision
 *   POST /v1/chat/completions → OpenAI format; Opus tiers translate Anthropic
 *                                SSE, Vertex translates Gemini SSE. the upstream down ->
 *                                Vertex Gemini fallback (no user-facing error).
 *   POST /v1/audio/speech      → TTS via edge-tts (mp3).
 *   POST /v1/audio/transcriptions → STT (B40): Groq whisper-large-v3-turbo
 *                                primary, local faster-whisper worker
 *                                (127.0.0.1:8499) fallback on any failure.
 *   POST /v1/embeddings         → RAG embeddings (B42): Vertex
 *                                gemini-embedding-001 (1536 dims), v1→v2
 *                                failover like chat. OpenAI format.
 *
 * Env additions (B40): GROQ_API_KEY (optional — if unset, STT goes straight
 * to the local worker).
 *
 * Env (from ui/.env): OPUS_API_KEY, OPUS_API_KEY_2 (Opus upstream),
 *                     GOOGLE_APPLICATION_CREDENTIALS, VERTEX_PROJECT_ID,
 *                     VERTEX_REGION (default `eu`), BRIDGE_API_KEY.
 * The bridge accepts requests with `Authorization: Bearer <BRIDGE_API_KEY>`
 * (OWUI connection uses it); if BRIDGE_API_KEY is unset, no auth required
 * (still loopback-bound only).
 */

import http from 'node:http';
import crypto from 'node:crypto';
import fs from 'node:fs';
import { spawn } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, '..');

// ---------- env ----------
function loadDotEnv(file) {
  const env = {};
  try {
    const txt = fs.readFileSync(file, 'utf8');
    for (const line of txt.split('\n')) {
      const m = line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$/);
      if (!m) continue;
      let v = m[2].trim();
      if ((v.startsWith('"') && v.endsWith('"')) || (v.startsWith("'") && v.endsWith("'"))) v = v.slice(1, -1);
      env[m[1]] = v;
    }
  } catch { /* .env optional when env vars injected via plist */ }
  return env;
}
const ENV = loadDotEnv(path.join(ROOT, '.env'));
const OPUS_KEYS = [
  { name: 'primary',   key: process.env.OPUS_API_KEY   || ENV.OPUS_API_KEY   || '' },
  { name: 'secondary', key: process.env.OPUS_API_KEY_2 || ENV.OPUS_API_KEY_2 || '' },
].filter((k) => k.key);
const VERTEX_REGION = process.env.VERTEX_REGION || ENV.VERTEX_REGION || 'eu';
// Vertex failover: v1 = primary project, v2 = secondary project. Requests try
// v1 first; on auth/HTTP/budget failure they fall through to v2 (and back to v1
// on the next request — no sticky pinning, so a recovered v1 resumes serving).
const VERTEX_PROVIDERS = [
  {
    name: '1',
    credFile: process.env.GOOGLE_APPLICATION_CREDENTIALS || ENV.GOOGLE_APPLICATION_CREDENTIALS || path.join(ROOT, '.vertUI.json'),
    project: process.env.VERTEX_PROJECT_ID || ENV.VERTEX_PROJECT_ID || '',
  },
  {
    name: '2',
    credFile: process.env.VERTEX_CREDENTIALS_2 || ENV.VERTEX_CREDENTIALS_2 || path.join(ROOT, '.vertUIv2.json'),
    project: process.env.VERTEX_PROJECT_ID_2 || ENV.VERTEX_PROJECT_ID_2 || '',
  },
].filter((p) => p.credFile && fs.existsSync(p.credFile) && p.project);
const VERTEX_PROJECT = VERTEX_PROVIDERS[0]?.project || '';
const BRIDGE_KEY = process.env.BRIDGE_API_KEY || ENV.BRIDGE_API_KEY || '';
const PORT = Number(process.env.BRIDGE_PORT || 8484);
const HOST = process.env.BRIDGE_HOST || ENV.BRIDGE_HOST || '127.0.0.1';

// ---------- Opus upstream (Anthropic /v1/messages, dual-key failover) ----------
// its OpenAI /chat/completions is Cloudflare-blocked (403) from this host, but
// its Anthropic-native /v1/messages returns 200. We translate OpenAI<->Anthropic
// here. Both keys (primary, secondary) are free; requests try primary first, then secondary; if BOTH
// fail we fall back to Vertex Gemini so the user never sees an error.
const OPUS_BASE = process.env.OPUS_BASE || ENV.OPUS_BASE || '';
const OPUS_VERSION = '2023-06-01';
const OPUS_MODEL = 'claude-opus-4-8'; // the only free model the upstream advertises
const VERTEX_MODEL = 'gemini-3.8-flash'; // curated Gemini id == Vertex publisher model

// Simplified family picker (le version 1). Fast/Daily/Xtra are all Opus 4.8 on
// Opus upstream, chat-only (no tools). NOTE: Opus upstream exposes NO reasoning/thinking
// parameter at all (provider-confirmed supported-params list is plain OpenAI:
// temperature/top_p/max_tokens/stop/seed/... with nothing for reasoning; and a
// budget sweep never moved output_tokens). So the Opus tiers cannot differ in
// hidden reasoning depth - they differ only in ANSWER style via a per-tier
// system steer + a max_tokens ceiling. `effort` below is used ONLY for the
// Vertex-Gemini fallback (Gemini genuinely honors thinkingLevel). Vision keeps
// the full household toolset + images.
const OPUS_TIERS = {
  'opus-fast':  { effort: null,     max: 4000,  steer: 'Answer in at most 2 short sentences. No lists, no headings, no code blocks, no examples, no elaboration. Give only the essential answer.' },
  'opus-daily': { effort: 'medium', max: 8000,  steer: 'Give a clear, direct answer in a short paragraph. Be helpful but concise and avoid long digressions unless asked.' },
  'opus-xtra':  { effort: 'high',   max: 28000, steer: 'Give a thorough, rigorous answer. Explain your reasoning, cover key trade-offs and edge cases, and use short headings or bullet points where they aid clarity.' },
};
const CURATED = [
  { id: 'opus-fast',        name: '⚡ Fast',    group: 'Opus',   provider: 'opus' },
  { id: 'opus-daily',       name: '💬 Daily',   group: 'Opus',   provider: 'opus' },
  { id: 'opus-xtra',        name: '🧠 Xtra',    group: 'Opus',   provider: 'opus' },
  { id: 'gemini-3.8-flash', name: '👁️ Vision',  group: 'Gemini', provider: 'vertex' },
];

// ---------- Vertex JWT auth (auto-refresh) ----------
const vtokens = {}; // provider name -> { value, expiresAt }
function loadCreds(prov) {
  return JSON.parse(fs.readFileSync(prov.credFile, 'utf8'));
}
function b64url(buf) {
  return Buffer.from(buf).toString('base64url');
}
async function vertexToken(prov) {
  const cache = vtokens[prov.name];
  if (cache && cache.value && Date.now() < cache.expiresAt - 60_000) return cache.value;
  const creds = loadCreds(prov);
  const now = Math.floor(Date.now() / 1000);
  const header = b64url(JSON.stringify({ alg: 'RS256', typ: 'JWT' }));
  const claims = b64url(JSON.stringify({
    iss: creds.client_email,
    scope: 'https://www.googleapis.com/auth/cloud-platform',
    aud: 'https://oauth2.googleapis.com/token',
    iat: now,
    exp: now + 3600,
  }));
  const signingInput = `${header}.${claims}`;
  const key = crypto.createPrivateKey(creds.private_key);
  const sig = crypto.sign('sha256', Buffer.from(signingInput), key);
  const assertion = `${signingInput}.${b64url(sig)}`;
  const res = await fetch('https://oauth2.googleapis.com/token', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ grant_type: 'urn:ietf:params:oauth:grant-type:jwt-bearer', assertion }),
  });
  if (!res.ok) throw new Error(`Vertex token [${prov.name}]: HTTP ${res.status} ${await res.text().catch(() => '')}`);
  const j = await res.json();
  vtokens[prov.name] = { value: j.access_token, expiresAt: Date.now() + (j.expires_in || 3600) * 1000 };
  return vtokens[prov.name].value;
}

// ---------- Vertex chat (non-stream & stream) ----------
function vertexUrl(prov, stream) {
  const base = `https://aiplatform.googleapis.com/v1/projects/${prov.project}/locations/${VERTEX_REGION}/publishers/google/models/${VERTEX_MODEL}`;
  return stream ? `${base}:streamGenerateContent?alt=sse` : `${base}:generateContent`;
}

/** Convert OpenAI tools array → Gemini functionDeclarations. */

// ---------- Vertex schema sanitizer ----------
// OpenAI-style JSON Schema (as sent by agent CLIs/OWUI) contains constructs Vertex's
// Schema proto rejects: array types ("type": ["string","null"]), exclusiveMin/Max,
// anyOf/oneOf/allOf, $defs/$ref, additionalProperties, title, etc.
// Whitelist + normalize to the Vertex subset: type, format, description, nullable,
// enum, items, properties, required, default, minimum, maximum, minItems, maxItems,
// minLength, maxLength.
const VERTEX_SCHEMA_KEYS = new Set([
  'type', 'format', 'description', 'nullable', 'enum', 'items', 'properties',
  'required', 'default', 'minimum', 'maximum', 'minItems', 'maxItems',
  'minLength', 'maxLength',
]);

function sanitizeVertexSchema(node, refs, depth = 0) {
  if (depth > 12 || node == null) return undefined;
  if (typeof node !== 'object') return node;
  if (Array.isArray(node)) {
    const arr = node.map((x) => sanitizeVertexSchema(x, refs, depth + 1)).filter((x) => x !== undefined);
    return arr.length ? arr : undefined;
  }
  // resolve $ref against root $defs/definitions if possible
  if (typeof node.$ref === 'string' && refs) {
    const m = node.$ref.match(/#\/?\$defs\/(.+)|#\/definitions\/(.+)/);
    const key = m ? (m[1] || m[2]) : null;
    if (key && refs[key]) return sanitizeVertexSchema(refs[key], refs, depth + 1);
    return { type: 'string' }; // unresolvable ref -> permissive fallback
  }
  const out = {};
  let type = node.type;
  let nullable = !!node.nullable;
  if (Array.isArray(type)) {
    nullable = nullable || type.includes('null');
    type = type.find((t) => t !== 'null') || 'string';
  }
  if (type) out.type = type;
  if (nullable) out.nullable = true;
  for (const k of VERTEX_SCHEMA_KEYS) {
    if (k === 'type' || k === 'nullable' || k === 'items' || k === 'properties' || k === 'required') continue;
    if (k === 'enum') {
      if (Array.isArray(node[k]) && node[k].every((v) => typeof v === 'string' || typeof v === 'number')) {
        out[k] = node[k].map(String);
      }
      continue;
    }
    if (node[k] !== undefined && node[k] !== null) out[k] = node[k];
  }
  if (Array.isArray(node.required) && node.required.every((r) => typeof r === 'string')) {
    out.required = node.required;
  }
  if (node.properties && typeof node.properties === 'object') {
    const props = {};
    for (const [k, v] of Object.entries(node.properties)) {
      const sv = sanitizeVertexSchema(v, refs, depth + 1);
      if (sv !== undefined) props[k] = sv;
    }
    if (Object.keys(props).length) out.properties = props;
  }
  const items = sanitizeVertexSchema(node.items, refs, depth + 1);
  if (items) out.items = items;
  // anyOf/oneOf/allOf: merge branches (object properties) or fall back to first branch
  if (!type && !out.properties) {
    for (const k of ['anyOf', 'oneOf', 'allOf']) {
      const arr = node[k];
      if (Array.isArray(arr) && arr.length) {
        const merged = { type: 'object', properties: {}, required: [] };
        let mergedAny = false;
        for (const branch of arr) {
          const sb = sanitizeVertexSchema(branch, refs, depth + 1);
          if (sb && sb.type === 'object' && sb.properties) {
            Object.assign(merged.properties, sb.properties);
            if (Array.isArray(sb.required)) merged.required.push(...sb.required);
            mergedAny = true;
          }
        }
        if (mergedAny) return merged;
        const first = sanitizeVertexSchema(arr[0], refs, depth + 1);
        if (first) return first;
        break;
      }
    }
  }
  return out;
}

function vertexRefs(parameters) {
  const refs = {};
  if (parameters && typeof parameters === 'object') {
    for (const k of ['$defs', 'definitions']) {
      if (parameters[k] && typeof parameters[k] === 'object') Object.assign(refs, parameters[k]);
    }
  }
  return refs;
}

function toVertexTools(tools) {
  const decls = [];
  for (const t of tools || []) {
    const fn = t?.function || t || {};
    if (!fn.name) continue;
    const params = fn.parameters || { type: 'object', properties: {} };
    const refs = vertexRefs(params);
    decls.push({
      name: fn.name,
      description: fn.description || '',
      parameters: sanitizeVertexSchema(params, refs) || { type: 'object', properties: {} },
    });
  }
  return decls.length ? [{ functionDeclarations: decls }] : undefined;
}

// Google's documented dummy thought-signature sentinel: the Gemini 3 validator
// accepts it when a real signature is unavailable (verified live: a dummy on the
// FIRST functionCall part clears the 400). Used only as a last-resort fallback.
const DUMMY_SIG = 'context_engineering_is_the_way_to_go';

/** Convert OpenAI messages → Gemini contents (text + base64 images + tools). */
function toVertexBody(messages, opts) {
  const system = messages.filter((m) => m.role === 'system').map((m) => m.content).join('\n\n');
  const contents = [];
  const pendingNames = new Map(); // tool_call_id -> function name (learned from assistant tool_calls)
  // Gemini 3 thinking models require every replayed functionCall to carry its
  // original `thought_signature`. OWUI replays the WHOLE chat history on every
  // turn, so resolved historical tool rounds must NOT be re-sent as functionCall
  // parts (we lack their signatures after a bridge restart) — we only render the
  // LIVE round that follows the last real user message. Old tool rounds are
  // dropped; their outcome already lives in the assistant text replies.
  let lastUserIdx = -1;
  for (let i = 0; i < (messages || []).length; i++) {
    const m = messages[i];
    if (m && m.role === 'user' && typeof m.content === 'string') lastUserIdx = i;
  }
  for (let i = 0; i < (messages || []).length; i++) {
    const m = messages[i];
    if (m.role === 'system') continue;
    const historicalTool =
      i < lastUserIdx && (m.role === 'tool' || (m.role === 'assistant' && Array.isArray(m.tool_calls) && m.tool_calls.length > 0));
    if (historicalTool) continue;
    const parts = [];

    // assistant tool_calls -> Gemini functionCall parts. Gemini 3 requires the
    // FIRST functionCall part of each model turn to carry its thought_signature
    // (parallel siblings need none). Replay the real captured signature when we
    // have it; if it was lost (streamed detached and never captured, or history
    // predates the registry) fall back to Google's documented dummy sentinel so
    // the turn is accepted (verified live) instead of failing the whole request
    // with HTTP 400 "Function call is missing a thought_signature".
    if (m.role === 'assistant' && Array.isArray(m.tool_calls)) {
      const fcParts = [];
      for (const tc of m.tool_calls) {
        const fn = tc?.function || {};
        if (!fn.name) continue;
        let args = {};
        try { args = JSON.parse(fn.arguments || '{}'); } catch { /* keep {} */ }
        if (tc.id) pendingNames.set(tc.id, fn.name);
        const part = { functionCall: { name: fn.name, args } };
        const sig = tc.id ? toolCallSigs.get(tc.id) : undefined;
        if (sig) part.thoughtSignature = sig;
        parts.push(part);
        fcParts.push(part);
      }
      if (fcParts.length && !fcParts[0].thoughtSignature) fcParts[0].thoughtSignature = DUMMY_SIG;
    }

    const content = Array.isArray(m.content)
      ? m.content
      : m.content == null ? [] : [{ type: 'text', text: m.content }];
    for (const c of content) {
      if (typeof c === 'string') {
        parts.push({ text: c });
      } else if (c.type === 'text') {
        parts.push({ text: c.text });
      } else if (c.type === 'image_url' && c.image_url?.url?.startsWith('data:image')) {
        const mm = c.image_url.url.match(/^data:(image\/[a-z+]+);base64,(.+)$/i);
        if (mm) parts.push({ inlineData: { mimeType: mm[1], data: mm[2] } });
      } else if (c.type === 'image') {
        parts.push({ inlineData: { mimeType: c.image?.mimeType || 'image/png', data: c.image?.data || '' } });
      }
    }

    // tool result(s) -> Gemini functionResponse part(s), role user. Gemini
    // requires the functionResponses for ONE function-call turn to arrive as a
    // SINGLE user content whose part count == that turn's functionCall count,
    // otherwise parallel tool calls (e.g. summarize_url + save_recipe) fail with
    // "number of function response parts must equal number of function call
    // parts". So contiguous role:'tool' messages are merged into one user turn.
    if (m.role === 'tool') {
      const name = pendingNames.get(m.tool_call_id) || 'unknown_tool';
      const output = typeof m.content === 'string' ? m.content : JSON.stringify(m.content ?? '');
      const part = { functionResponse: { name, response: { output } } };
      const last = contents[contents.length - 1];
      const openRun =
        last && last.role === 'user' && Array.isArray(last.parts) &&
        last.parts.length && last.parts.every((p) => p && p.functionResponse);
      if (openRun) {
        last.parts.push(part);
      } else {
        contents.push({ role: 'user', parts: [part] });
      }
      continue;
    }

    if (parts.length) contents.push({ role: m.role === 'assistant' ? 'model' : 'user', parts });
  }
  const body = { contents, generationConfig: {} };
  if (opts.max_tokens) body.generationConfig.maxOutputTokens = opts.max_tokens;
  // Gemini 3.8 family: temperature/top_p/top_k are deprecated (ignored by the
  // backend) and integer thinking budgets are deprecated in favor of the
  // thinking_level enum — see cloud docs "Developer's guide to Gemini 3.8 Flash".
  // We deliberately drop them from the payload instead of forwarding no-ops.
  if (opts.reasoning_effort) {
    const level = opts.reasoning_effort === 'low' ? 'LOW' : opts.reasoning_effort === 'high' ? 'HIGH' : 'MEDIUM';
    body.generationConfig.thinkingConfig = { thinkingLevel: level };
  }
  const tools = toVertexTools(opts.tools);
  if (tools) body.tools = tools;
  return body;
}

let vprovActive = null; // last provider that served a request


async function vertexFetch(prov, messages, opts, stream) {
  const token = await vertexToken(prov);
  const url = vertexUrl(prov, stream);
  const resp = await fetch(url, {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    body: JSON.stringify(toVertexBody(messages, opts)),
  });
  if (!resp.ok) throw new Error(`Vertex [${prov.name}] chat: HTTP ${resp.status} ${(await resp.text()).slice(0, 300)}`);
  vprovActive = prov.name;
  return resp;
}

async function vertexChat(messages, opts, stream, res) {
  let resp = null;
  let lastErr = null;
  for (const prov of VERTEX_PROVIDERS) {
    try {
      resp = await vertexFetch(prov, messages, opts, stream);
      break;
    } catch (e) {
      lastErr = e;
      console.error(`[bridge] vertex provider ${prov.name} failed: ${e.message}`);
    }
  }
  if (!resp) throw lastErr;

  if (!stream) {
    const j = await resp.json();
    const cand = j?.candidates?.[0] || {};
    const parts = cand?.content?.parts || [];
    const text = parts.map((p) => p.text || '').join('');
    const toolCalls = parts
      .filter((p) => p.functionCall)
      .map((p, i) => {
        const id = p.functionCall.id || `call_vx_${i}`;
        if (p.thoughtSignature) setSig(id, p.thoughtSignature);
        return { id, type: 'function', function: { name: p.functionCall.name, arguments: JSON.stringify(p.functionCall.args || {}) } };
      });
    const finishReason = toolCalls.length
      ? 'tool_calls'
      : cand?.finishReason === 'STOP' ? 'stop' : (cand?.finishReason || 'stop').toLowerCase();
    const usage = j?.usageMetadata || {};
    const message = { role: 'assistant', content: text || null };
    if (toolCalls.length) message.tool_calls = toolCalls;
    const out = {
      id: `chatcmpl-vx-${crypto.randomBytes(8).toString('hex')}`,
      object: 'chat.completion',
      created: Math.floor(Date.now() / 1000),
      model: VERTEX_MODEL,
      choices: [{ index: 0, message, finish_reason: finishReason }],
      usage: {
        prompt_tokens: usage.promptTokenCount || 0,
        completion_tokens: usage.candidatesTokenCount || 0,
        total_tokens: usage.totalTokenCount || 0,
      },
    };
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(out));
    return out;
  }

  // stream: translate Gemini SSE → OpenAI SSE
  res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', Connection: 'keep-alive' });
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  const send = (obj) => res.write(`data: ${JSON.stringify(obj)}\n\n`);
  const created = Math.floor(Date.now() / 1000);
  let first = true;
  let sawToolCalls = false;
  let turnSig = null;   // most recent thoughtSignature seen this response (Gemini 3 may stream it detached, on a later/empty part)
  let firstFcId = null; // id of the first functionCall this response — the only part that must carry the signature on replay
  let usg = null; // real usage from Gemini usageMetadata (stream end)
  let accChars = 0; // fallback completion-token estimate if usageMetadata absent
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, idx).trim();
        buf = buf.slice(idx + 1);
        if (!line.startsWith('data:')) continue;
        let j;
        try { j = JSON.parse(line.slice(5).trim()); } catch { continue; }
        const um = j?.usageMetadata;
        if (um && um.totalTokenCount != null) {
          usg = { p: um.promptTokenCount || 0, c: um.candidatesTokenCount || 0, t: um.totalTokenCount || 0 };
        }
        const parts = j?.candidates?.[0]?.content?.parts || [];
        const text = parts.map((p) => p.text || '').join('');
        const thought = parts.map((p) => p.thought || '').join('');
        const fcParts = parts.filter((p) => p.functionCall);
        for (const p of parts) { if (p.thoughtSignature) turnSig = p.thoughtSignature; }
        accChars += text.length + thought.length + fcParts.reduce((n, fc) => n + (fc?.functionCall?.args ? JSON.stringify(fc.functionCall.args).length : 0), 0);
        if (first) {
          send({ id: `chatcmpl-vx-${created}`, object: 'chat.completion.chunk', created, model: VERTEX_MODEL, choices: [{ index: 0, delta: { role: 'assistant' }, finish_reason: null }] });
          first = false;
        }
        if (thought) send({ id: `chatcmpl-vx-${created}`, object: 'chat.completion.chunk', created, model: VERTEX_MODEL, choices: [{ index: 0, delta: { reasoning_content: thought }, finish_reason: null }] });
        if (text) send({ id: `chatcmpl-vx-${created}`, object: 'chat.completion.chunk', created, model: VERTEX_MODEL, choices: [{ index: 0, delta: { content: text }, finish_reason: null }] });
        for (let fi = 0; fi < fcParts.length; fi++) {
          const fc = fcParts[fi].functionCall;
          sawToolCalls = true;
          const cid = fc.id || `call_vx_${fi}`;
          if (firstFcId === null) firstFcId = cid;
          if (fcParts[fi].thoughtSignature) setSig(cid, fcParts[fi].thoughtSignature);
          send({ id: `chatcmpl-vx-${created}`, object: 'chat.completion.chunk', created, model: VERTEX_MODEL, choices: [{ index: 0, delta: { tool_calls: [{ index: fi, id: cid, type: 'function', function: { name: fc.name, arguments: JSON.stringify(fc.args || {}) } }] }, finish_reason: null }] });
        }
      }
    }
    // If the signature streamed in detached from its functionCall part (Gemini's
    // "empty part in the final chunk" behaviour), bind it to the first tool call
    // so the next turn replays a real signature instead of the dummy fallback.
    if (firstFcId && turnSig && !toolCallSigs.has(firstFcId)) setSig(firstFcId, turnSig);
    send({ id: `chatcmpl-vx-${created}`, object: 'chat.completion.chunk', created, model: VERTEX_MODEL, choices: [{ index: 0, delta: {}, finish_reason: sawToolCalls ? 'tool_calls' : 'stop' }] });
    send({ id: `chatcmpl-vx-${created}`, object: 'chat.completion.chunk', created, model: VERTEX_MODEL, choices: [], usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 } });
    res.end();
  } catch (e) {
    res.write(`data: ${JSON.stringify({ error: { message: String(e.message) } })}\n\n`);
    res.end();
  }
}

// ---------- Opus upstream (OpenAI <-> Anthropic translation) ----------

// OpenAI chat messages -> Anthropic {system, messages}. Opus tiers are chat-only:
// tool definitions and tool/assistant-tool_call turns are dropped (Vision handles
// tools). Images (data URLs) are forwarded as Anthropic image blocks.
function toAnthropicBody(messages, opts) {
  const sysParts = [];
  const amsgs = [];
  for (const m of messages || []) {
    if (!m) continue;
    if (m.role === 'system') {
      const t = typeof m.content === 'string' ? m.content
        : Array.isArray(m.content) ? m.content.map((c) => (typeof c === 'string' ? c : c.text || '')).join('') : '';
      if (t.trim()) sysParts.push(t);
      continue;
    }
    if (m.role !== 'user' && m.role !== 'assistant') continue; // drop tool turns
    if (m.role === 'assistant' && Array.isArray(m.tool_calls) && m.tool_calls.length && !m.content) continue;
    let content;
    if (typeof m.content === 'string') {
      content = m.content;
    } else if (Array.isArray(m.content)) {
      const blocks = [];
      for (const c of m.content) {
        if (typeof c === 'string') { if (c) blocks.push({ type: 'text', text: c }); }
        else if (c.type === 'text' && c.text) blocks.push({ type: 'text', text: c.text });
        else if (c.type === 'image_url' && c.image_url?.url?.startsWith('data:image')) {
          const mm = c.image_url.url.match(/^data:(image\/[a-z+]+);base64,(.+)$/i);
          if (mm) blocks.push({ type: 'image', source: { type: 'base64', media_type: mm[1], data: mm[2] } });
        } else if (c.type === 'image' && c.image?.data) {
          blocks.push({ type: 'image', source: { type: 'base64', media_type: c.image.mimeType || 'image/png', data: c.image.data } });
        }
      }
      content = blocks;
    } else content = '';
    amsgs.push({ role: m.role, content });
  }
  // Anthropic requires non-empty, user-first messages. Drop empties and merge
  // consecutive same-role turns.
  const cleaned = [];
  const isEmpty = (c) => (typeof c === 'string' ? !c.trim() : (!Array.isArray(c) || c.length === 0));
  for (const msg of amsgs) {
    if (isEmpty(msg.content)) continue;
    const prev = cleaned[cleaned.length - 1];
    if (prev && prev.role === msg.role) {
      const toArr = (x) => (Array.isArray(x) ? x : [{ type: 'text', text: String(x) }]);
      prev.content = [...toArr(prev.content), ...toArr(msg.content)];
    } else cleaned.push({ role: msg.role, content: msg.content });
  }
  while (cleaned.length && cleaned[0].role !== 'user') cleaned.shift();
  if (!cleaned.length) cleaned.push({ role: 'user', content: 'Hello' });
  if (opts.steer) sysParts.push(opts.steer);
  // Opus upstream exposes NO reasoning/thinking parameter (provider-confirmed param
  // list + a budget sweep that never moved output_tokens), so we send none. The
  // tiers differ only by the system steer above + this max_tokens ceiling.
  const body = { model: OPUS_MODEL, max_tokens: opts.max_tokens || 4096, messages: cleaned };
  if (sysParts.length) body.system = sysParts.join('\n\n');
  return body;
}

let jwActive = null; // last the upstream key that served a request

async function jwFetch(k, body, stream) {
  const resp = await fetch(`${OPUS_BASE}/messages`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', 'x-api-key': k.key, 'anthropic-version': OPUS_VERSION },
    body: JSON.stringify({ ...body, stream: !!stream }),
    signal: AbortSignal.timeout(180_000),
  });
  if (!resp.ok) throw new Error(`the upstream[${k.name}] HTTP ${resp.status} ${(await resp.text().catch(() => '')).slice(0, 200)}`);
  return resp;
}

// Try py then zen; throws (before writing any response) only if BOTH keys fail,
// letting the caller fall back to Vertex.
async function jwChat(messages, opts, stream, res) {
  const body = toAnthropicBody(messages, opts);
  let resp = null; let lastErr = null;
  for (const k of OPUS_KEYS) {
    try { resp = await jwFetch(k, body, stream); jwActive = k.name; break; }
    catch (e) { lastErr = e; console.error(`[bridge] the upstream key ${k.name} failed: ${e.message}`); }
  }
  if (!resp) throw lastErr || new Error('the upstream: no keys configured');

  if (!stream) {
    const j = await resp.json();
    const blocks = j.content || [];
    const text = blocks.filter((b) => b.type === 'text').map((b) => b.text).join('');
    const thinking = blocks.filter((b) => b.type === 'thinking').map((b) => b.thinking).join('');
    const usage = j.usage || {};
    const message = { role: 'assistant', content: text || null };
    if (thinking) message.reasoning_content = thinking;
    const out = {
      id: `chatcmpl-jw-${crypto.randomBytes(8).toString('hex')}`,
      object: 'chat.completion',
      created: Math.floor(Date.now() / 1000),
      model: OPUS_MODEL,
      choices: [{ index: 0, message, finish_reason: j.stop_reason === 'end_turn' ? 'stop' : (j.stop_reason || 'stop') }],
      usage: {
        prompt_tokens: usage.input_tokens || 0,
        completion_tokens: usage.output_tokens || 0,
        total_tokens: (usage.input_tokens || 0) + (usage.output_tokens || 0),
      },
    };
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(out));
    return;
  }

  // stream: Anthropic SSE -> OpenAI SSE
  res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', Connection: 'keep-alive' });
  const created = Math.floor(Date.now() / 1000);
  const id = `chatcmpl-jw-${created}`;
  const send = (delta, finish = null) => res.write(`data: ${JSON.stringify({ id, object: 'chat.completion.chunk', created, model: OPUS_MODEL, choices: [{ index: 0, delta, finish_reason: finish }] })}\n\n`);
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  let finish = 'stop';
  try {
    send({ role: 'assistant' });
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, idx).trim();
        buf = buf.slice(idx + 1);
        if (!line.startsWith('data:')) continue;
        let ev; try { ev = JSON.parse(line.slice(5).trim()); } catch { continue; }
        if (ev.type === 'content_block_delta') {
          const d = ev.delta || {};
          if (d.type === 'text_delta' && d.text) send({ content: d.text });
          else if (d.type === 'thinking_delta' && d.thinking) send({ reasoning_content: d.thinking });
        } else if (ev.type === 'message_delta' && ev.delta?.stop_reason) {
          finish = ev.delta.stop_reason === 'end_turn' ? 'stop' : ev.delta.stop_reason;
        }
      }
    }
    send({}, finish);
    res.write(`data: ${JSON.stringify({ id, object: 'chat.completion.chunk', created, model: OPUS_MODEL, choices: [], usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 } })}\n\n`);
    res.write('data: [DONE]\n\n');
    res.end();
  } catch (e) {
    res.write(`data: ${JSON.stringify({ error: { message: String(e.message) } })}\n\n`);
    res.end();
  }
}

// ---------- HTTP server ----------
// thoughtSignature registry: tool_call id -> signature (Gemini 3 requires
// echoing the signature with functionCall parts in multi-turn tool loops).
// PERSISTED to a file so replays survive a bridge restart (OWUI resends the
// whole chat history every turn; a restart used to orphan signatures → 400).
const toolCallSigs = new Map();
const SIGFILE = path.join(ROOT, 'logs', 'bridge.toolcallsigs.json');
let _sigTimer = null;
function persistSigs() {
  if (_sigTimer) clearTimeout(_sigTimer);
  _sigTimer = setTimeout(() => {
    try {
      const obj = {};
      for (const [k, v] of toolCallSigs) obj[k] = v;
      fs.writeFileSync(SIGFILE, JSON.stringify(obj));
    } catch (_) { /* ignore */ }
  }, 600);
}
function setSig(id, sig) {
  if (!id || sig == null) return;
  toolCallSigs.set(id, sig);
  persistSigs();
}
function loadSigs() {
  try {
    const obj = JSON.parse(fs.readFileSync(SIGFILE, 'utf8'));
    for (const k of Object.keys(obj)) toolCallSigs.set(k, obj[k]);
  } catch (_) { /* first run / empty */ }
}
loadSigs();
let started = Date.now();
let health = { opus: 'unknown', vertex: 'unknown', vertex1: 'unknown', vertex2: 'unknown', vertexActive: null, lastCheck: 0 };

async function checkHealth() {
  const out = {};
  try {
    const k = OPUS_KEYS[0];
    if (!k) { out.opus = 'nokey'; }
    else {
      const r = await fetch(`${OPUS_BASE}/models`, { headers: { 'x-api-key': k.key, 'anthropic-version': OPUS_VERSION }, signal: AbortSignal.timeout(8000) });
      out.opus = r.ok ? 'ok' : `http_${r.status}`;
    }
  } catch (e) { out.opus = 'down'; }
  for (const prov of VERTEX_PROVIDERS) {
    try {
      await vertexToken(prov);
      out[`vertex${prov.name}`] = 'ok';
    } catch (e) { out[`vertex${prov.name}`] = 'down'; }
  }
  out.vertex = VERTEX_PROVIDERS.some((p) => out[`vertex${p.name}`] === 'ok') ? 'ok' : 'down';
  out.vertexActive = vprovActive;
  return out;
}

// ---------- Vertex embeddings (RAG, B42) ----------
const EMBED_MODEL = 'gemini-embedding-001';
const EMBED_DIMS = 1536;

async function vertexEmbed(prov, texts) {
  const token = await vertexToken(prov);
  const url = `https://aiplatform.googleapis.com/v1/projects/${prov.project}/locations/${VERTEX_REGION}/publishers/google/models/${EMBED_MODEL}:predict`;
  const instances = texts.map((content) => ({ content, task_type: 'RETRIEVAL_DOCUMENT' }));
  const resp = await fetch(url, {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ instances, parameters: { outputDimensionality: EMBED_DIMS } }),
    signal: AbortSignal.timeout(90_000),
  });
  if (!resp.ok) throw new Error(`Vertex [${prov.name}] embed: HTTP ${resp.status} ${(await resp.text()).slice(0, 300)}`);
  const j = await resp.json();
  return (j.predictions || []).map((p) => (p?.embeddings?.values) || []);
}

async function embedTexts(texts) {
  let lastErr = null;
  for (const prov of VERTEX_PROVIDERS) {
    try {
      return await vertexEmbed(prov, texts);
    } catch (e) {
      lastErr = e;
      console.error(`[bridge] vertex embed provider ${prov.name} failed: ${e.message}`);
    }
  }
  throw lastErr;
}

const server = http.createServer(async (req, res) => {
  try {
    // auth
    if (BRIDGE_KEY) {
      const auth = req.headers.authorization || '';
      if (!auth.startsWith('Bearer ') || auth.slice(7) !== BRIDGE_KEY) {
        res.writeHead(401, { 'Content-Type': 'application/json' });
        return res.end(JSON.stringify({ error: { message: 'unauthorized' } }));
      }
    }

    const url = new URL(req.url, `http://${req.headers.host}`);
    const pathname = url.pathname;

    if (req.method === 'GET' && pathname === '/health') {
      if (Date.now() - health.lastCheck > 30_000) { health = { ...health, ...(await checkHealth()), lastCheck: Date.now() }; }
      const stt = (process.env.GROQ_API_KEY || ENV.GROQ_API_KEY) ? 'groq+local' : 'local-only';
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify({ status: 'ok', ...health, stt, uptime_s: Math.floor((Date.now() - started) / 1000) }));
    }

    if (req.method === 'GET' && pathname === '/v1/models') {
      const data = CURATED.map((m) => ({
        id: m.id,
        object: 'model',
        created: 0,
        owned_by: m.provider,
        name: m.name,
        group: m.group,
      }));
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify({ object: 'list', data }));
    }

    if (req.method === 'POST' && pathname === '/v1/audio/speech') {
      let raw = '';
      for await (const chunk of req) raw += chunk;
      let payload = {};
      try { payload = JSON.parse(raw || '{}'); } catch { /* keep {} */ }
      const text = String(payload.input || '').slice(0, 4000);
      const voice = String(payload.voice || 'nl-NL-MaartenNeural');
      if (!text.trim()) {
        res.writeHead(400, { 'Content-Type': 'application/json' });
        return res.end(JSON.stringify({ error: { message: 'input text is required' } }));
      }
      const tmp = `/tmp/owui-tts-${crypto.randomBytes(8).toString('hex')}.mp3`;
      try {
        const py = process.env.TTS_PYTHON || 'python3';
        await new Promise((resolve, reject) => {
          const proc = spawn(py, ['-m', 'edge_tts', '--voice', voice, '--text', text, '--write-media', tmp], { stdio: 'ignore' });
          proc.on('error', reject);
          proc.on('close', (code) => code === 0 ? resolve() : reject(new Error('edge-tts exit ' + code)));
        });
        const data = fs.readFileSync(tmp);
        fs.unlink(tmp, () => {});
        res.writeHead(200, { 'Content-Type': 'audio/mpeg', 'Content-Length': data.length });
        return res.end(data);
      } catch (e) {
        res.writeHead(500, { 'Content-Type': 'application/json' });
        return res.end(JSON.stringify({ error: { message: 'TTS failed: ' + String(e.message) } }));
      }
    }

    if (req.method === 'POST' && pathname === '/v1/audio/transcriptions') {
      // ---- multipart parse (stdlib only) ----
      const ct = req.headers['content-type'] || '';
      const bm = ct.match(/boundary="?([^";]+)"?/);
      if (!bm) {
        res.writeHead(400, { 'Content-Type': 'application/json' });
        return res.end(JSON.stringify({ error: { message: 'multipart boundary missing' } }));
      }
      const boundary = Buffer.from('--' + bm[1]);
      const chunks = [];
      for await (const chunk of req) chunks.push(chunk);
      const raw = Buffer.concat(chunks);
      const parts = raw.toString('latin1').split(boundary.toString('latin1'));
      let audio = null, filename = 'audio.webm', modelName = 'whisper-large-v3-turbo', language = null, responseFormat = null;
      for (const part of parts) {
        const nl = part.indexOf('\r\n\r\n');
        if (nl < 0) continue;
        const head = part.slice(0, nl);
        const body = part.slice(nl + 4).replace(/\r\n$/, '');
        const fd = head.match(/name="([^"]*)"/);
        if (!fd) continue;
        const fname = fd[1];
        if (fname === 'file') {
          const fm = head.match(/filename="([^"]*)"/);
          if (fm) filename = fm[1];
          audio = Buffer.from(body, 'latin1');
        } else if (fname === 'model') { modelName = body.trim() || modelName; }
        else if (fname === 'language') { language = body.trim() || null; }
        else if (fname === 'response_format') { responseFormat = body.trim() || null; }
      }
      if (!audio || audio.length === 0) {
        res.writeHead(400, { 'Content-Type': 'application/json' });
        return res.end(JSON.stringify({ error: { message: 'missing audio file part' } }));
      }

      const GROQ_API_KEY = process.env.GROQ_API_KEY || ENV.GROQ_API_KEY || '';
      let text = null, groqErr = null, localErr = null;

      // 1) Groq primary
      if (GROQ_API_KEY) {
        try {
          const fd = new FormData();
          fd.append('model', modelName);
          fd.append('file', new Blob([audio], { type: 'application/octet-stream' }), filename);
          if (language) fd.append('language', language);
          const groqResp = await fetch('https://api.groq.com/openai/v1/audio/transcriptions', {
            method: 'POST',
            headers: { Authorization: `Bearer ${GROQ_API_KEY}` },
            body: fd,
            signal: AbortSignal.timeout(60_000),
          });
          if (!groqResp.ok) throw new Error(`Groq HTTP ${groqResp.status}`);
          const j = await groqResp.json();
          text = (j.text || '').trim();
        } catch (e) {
          groqErr = String(e.message);
        }
      }

      // 2) local faster-whisper fallback
      if (text === null) {
        try {
          const fd = new FormData();
          fd.append('file', new Blob([audio], { type: 'application/octet-stream' }), filename);
          if (language) fd.append('language', language);
          const loc = await fetch('http://127.0.0.1:8499/transcribe', {
            method: 'POST',
            body: fd,
            signal: AbortSignal.timeout(180_000),
          });
          if (!loc.ok) {
            const le = await loc.json().catch(() => ({}));
            throw new Error(`local worker HTTP ${loc.status} ${le.error || ''}`);
          }
          const j = await loc.json();
          text = (j.text || '').trim();
        } catch (e) {
          localErr = String(e.message);
        }
      }

      if (text === null) {
        res.writeHead(502, { 'Content-Type': 'application/json' });
        return res.end(JSON.stringify({ error: { message: `STT unavailable (groq: ${groqErr || 'no key'} | local: ${localErr || 'no worker'})` } }));
      }
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify({ text }));
    }

    if (req.method === 'POST' && pathname === '/v1/embeddings') {
      let raw = '';
      for await (const chunk of req) raw += chunk;
      let body = {};
      try { body = JSON.parse(raw || '{}'); } catch { /* keep {} */ }
      const input = body.input;
      const texts = (Array.isArray(input) ? input : [input])
        .filter((t) => typeof t === 'string')
        .map((t) => t.trim().slice(0, 8000))
        .filter((t) => t.length > 0);
      if (texts.length === 0) {
        res.writeHead(400, { 'Content-Type': 'application/json' });
        return res.end(JSON.stringify({ error: { message: 'input strings required' } }));
      }
      try {
        const vectors = [];
        // Vertex predict accepts batches; keep chunks of 25 to stay safe on size
        for (let i = 0; i < texts.length; i += 25) {
          const batch = texts.slice(i, i + 25);
          const got = await embedTexts(batch);
          vectors.push(...got);
        }
        const data = vectors.map((vec, index) => ({ object: 'embedding', index, embedding: vec }));
        const approxTokens = texts.reduce((n, t) => n + Math.ceil(t.length / 4), 0);
        res.writeHead(200, { 'Content-Type': 'application/json' });
        return res.end(JSON.stringify({ object: 'list', data, model: String(body.model || EMBED_MODEL), usage: { prompt_tokens: approxTokens, total_tokens: approxTokens } }));
      } catch (e) {
        res.writeHead(502, { 'Content-Type': 'application/json' });
        return res.end(JSON.stringify({ error: { message: 'embeddings failed: ' + String(e.message) } }));
      }
    }

    if (req.method === 'POST' && pathname === '/v1/chat/completions') {
      let raw = '';
      for await (const chunk of req) raw += chunk;
      const body = JSON.parse(raw || '{}');
      const model = body.model || 'opus-daily';
      const messages = body.messages || [];
      const stream = !!body.stream;

      // Vision -> Vertex Gemini (keeps tools + images), max thinking by default.
      if (model === VERTEX_MODEL) {
        await vertexChat(messages, { ...body, reasoning_effort: body.reasoning_effort || 'high' }, stream, res);
        return;
      }

      // Opus tiers (Fast/Daily/Xtra) + any legacy claude-opus id -> Opus upstream,
      // primary->secondary failover, then Vertex Gemini fallback if BOTH the upstream keys are down.
      const tier = OPUS_TIERS[model] || (model.startsWith('claude-opus') ? OPUS_TIERS['opus-daily'] : null);
      if (tier) {
        const opts = { ...body, steer: tier.steer, max_tokens: tier.max };
        try {
          await jwChat(messages, opts, stream, res);
        } catch (e) {
          console.error(`[bridge] Opus upstream unavailable, Vertex Gemini fallback: ${e.message}`);
          if (res.headersSent) return;
          await vertexChat(messages, { ...body, reasoning_effort: tier.effort || 'medium', max_tokens: 4096 }, stream, res);
        }
        return;
      }

      // Anything else (legacy task models, unknown ids) -> Vertex Gemini.
      await vertexChat(messages, { ...body, max_tokens: body.max_tokens || 2048 }, stream, res);
      return;
    }

    res.writeHead(404, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: { message: `no route ${req.method} ${pathname}` } }));
  } catch (e) {
    res.writeHead(500, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: { message: String(e.message) } }));
  }
});

server.listen(PORT, HOST, () => {
  console.log(`bridge listening on http://${HOST}:${PORT} (region ${VERTEX_REGION}, vertex=${VERTEX_MODEL}, vertexProviders=${VERTEX_PROVIDERS.map((p) => p.name).join('+')}, opus=${OPUS_KEYS.map((k) => k.name).join('+') || 'none'})`);
});
