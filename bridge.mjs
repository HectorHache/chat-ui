#!/usr/bin/env node
/**
 * Chat·hector.app — local model bridge (127.0.0.1:8484)
 *
 * OpenAI-compatible endpoint fronting:
 *   1. AgentRouter (primary)   — WAF headers + AR_API_KEY in ONE place
 *   2. Vertex Gemini (fallback)— Google SDK-style JWT auth, auto-refresh,
 *                                EU region (`eu` short name; Google changed
 *                                regional hostnames — `eu-aiplatform` and
 *                                `europe-west4-aiplatform` hosts are INVALID).
 *
 * Zero npm dependencies: node:http + global fetch + node:crypto.
 * Endpoints:
 *   GET  /health              → {status, agentrouter, vertex, uptime}
 *   GET  /v1/models           → curated 6-model list (OpenAI format)
 *   POST /v1/chat/completions → OpenAI format; streams SSE for AgentRouter
 *                                (passthrough) and Vertex (translated).
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
 * Env (from ui/.env): AR_API_KEY, GOOGLE_APPLICATION_CREDENTIALS,
 * VERTEX_PROJECT_ID, VERTEX_REGION (default `eu`), BRIDGE_API_KEY.
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
const AR_API_KEY = process.env.AR_API_KEY || ENV.AR_API_KEY || '';
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

// ---------- AgentRouter ----------
const AR_BASE = 'https://agentrouter.org/v1';
const AR_HEADERS = {
  'User-Agent': 'claude-cli/2.1.158 (external, sdk-cli)',
  'x-app': 'cli',
  'anthropic-beta': 'claude-code-20250219,interleaved-thinking-2025-05-14',
};
const AR_MODELS = ['deepseek-v4-flash', 'claude-opus-4-8', 'claude-opus-5', 'gpt-5.6-sol', 'glm-5.3'];
const VERTEX_MODEL = 'gemini-3.8-flash'; // our curated Gemini id == Vertex publisher model

const CURATED = [
  { id: 'deepseek-v4-flash',     name: 'DeepSeek V4 Flash (fast)',      group: 'Fast',      provider: 'agentrouter' },
  { id: 'claude-opus-4-8',       name: 'Claude Opus 4.8 (thinking)',    group: 'Thinking',  provider: 'agentrouter' },
  { id: 'claude-opus-5',         name: 'Claude Opus 5 (thinking)',      group: 'Thinking',  provider: 'agentrouter' },
  { id: 'gpt-5.6-sol',           name: 'GPT-5.6 Sol (thinking)',        group: 'Thinking',  provider: 'agentrouter' },
  { id: 'glm-5.3',               name: 'GLM 5.3 (creative)',            group: 'Creative',  provider: 'agentrouter' },
  { id: 'gemini-3.8-flash',      name: 'Gemini 3.8 Flash (vision)',     group: 'Gemini',    provider: 'vertex' },
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

    // assistant tool_calls -> Gemini functionCall parts
    if (m.role === 'assistant' && Array.isArray(m.tool_calls)) {
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
      }
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
  if (typeof opts.temperature === 'number') body.generationConfig.temperature = opts.temperature;
  if (opts.top_p) body.generationConfig.topP = opts.top_p;
  if (system) body.systemInstruction = { parts: [{ text: system }] };
  if (opts.reasoning_effort) body.generationConfig.thinkingConfig = { thinkingBudget: opts.reasoning_effort === 'low' ? 1024 : 8192 };
  const tools = toVertexTools(opts.tools);
  if (tools) body.tools = tools;
  return body;
}

let vprovActive = null; // last provider that served a request

// ---------- Language detector (B43) ----------
// Routes Spanish / non-English content to Vertex, because AgentRouter
// content-blocks anything it can't serve in English. AgentRouter sees the WHOLE
// conversation history, so if ANY user turn is Spanish the entire request is
// Spanish to it and would be content-blocked. Therefore we classify EVERY user
// textual turn and route to Vertex if any of them is non-English. We stay on
// AgentRouter only when all user turns are confidently English; anything
// ambiguous/unknown routes to Vertex (the safe direction — Vertex handles both
// languages, AgentRouter handles only English).
const _normW = (w) => String(w).toLowerCase()
  .replace(/[áàäâ]/g, 'a').replace(/[éèëê]/g, 'e').replace(/[íìïî]/g, 'i')
  .replace(/[óòöô]/g, 'o').replace(/[úùüû]/g, 'u').replace(/ñ/g, 'n');
const ES_SET = new Set(`
  hola que como estas esta estamos dame danos da receta recetas magdalena magdalenas
  bizcocho tortilla natillas croquetas empanadillas ingrediente ingredientes harina azucar
  huevo huevos aceite horno cebolla ajo yogur yogurt leche agua pan sal pimienta queso tomate
  guarda guardar guardada guardado guardame puedes podrias quiero necesito necesitamos tenemos
  tengo tienes dime muestrame gracias favor bien mal hoy manana ayer pero tambien por con para
  sin sobre el la los las un una y o mi mis tu tus del al cuando donde porque cual cuales haz ver
  hazme anade agrega mezcla hornea precalienta echa vierte pela corta cuece espera cuanto cuantos
  cuanta minutos grados tapa remueve deja saca pon hazte nuez nueces
`.split(/\s+/).filter(Boolean).map(_normW));
const EN_SET = new Set(`
  the a an and or but if of to in for with on at by from is are was were be been have has had
  do does did will would can could should may might you your yours we our us he she it its they
  them this that these those what which who whom how why when where there here me my not no
  yes ok okay sure thanks thank please hello hi hey give tell show list store save saved add
  want need make got good great fine right one two three recipes recipe ingredients ingredient
  muffins pecan
`.split(/\s+/).filter(Boolean).map(_normW));
const ACCENT = /[áéíóúüñ]/i;
function isSpanishTurn(text) {
  const raw = String(text || '');
  if (!raw.trim()) return false;
  if (ACCENT.test(raw)) return true;
  const toks = raw.toLowerCase().match(/[a-záéíóúüñ]+/g) || [];
  if (!toks.length) return false;
  let es = 0; let en = 0;
  for (const w of toks) {
    const n = _normW(w);
    if (ES_SET.has(n)) es++;
    else if (EN_SET.has(n)) en++;
  }
  if (es === 0 && en === 0) return true; // unknown/ambiguous -> treat as non-English
  return es >= en; // Spanish wins ties; English only if clearly dominant
}
function shouldRouteToVertex(messages) {
  let sawUserText = false;
  for (const m of messages || []) {
    if (m && m.role === 'user' && typeof m.content === 'string') {
      sawUserText = true;
      if (isSpanishTurn(m.content)) return true;
    }
  }
  return !sawUserText; // no user text present -> default to Vertex (safe)
}


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
          if (fcParts[fi].thoughtSignature) setSig(cid, fcParts[fi].thoughtSignature);
          send({ id: `chatcmpl-vx-${created}`, object: 'chat.completion.chunk', created, model: VERTEX_MODEL, choices: [{ index: 0, delta: { tool_calls: [{ index: fi, id: cid, type: 'function', function: { name: fc.name, arguments: JSON.stringify(fc.args || {}) } }] }, finish_reason: null }] });
        }
      }
    }
    send({ id: `chatcmpl-vx-${created}`, object: 'chat.completion.chunk', created, model: VERTEX_MODEL, choices: [{ index: 0, delta: {}, finish_reason: sawToolCalls ? 'tool_calls' : 'stop' }] });
    send({ id: `chatcmpl-vx-${created}`, object: 'chat.completion.chunk', created, model: VERTEX_MODEL, choices: [], usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 } });
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
let health = { agentrouter: 'unknown', vertex: 'unknown', vertex1: 'unknown', vertex2: 'unknown', vertexActive: null, lastCheck: 0 };

async function checkHealth() {
  const out = {};
  try {
    const r = await fetch(`${AR_BASE}/models`, { headers: { Authorization: `Bearer ${AR_API_KEY}`, ...AR_HEADERS }, signal: AbortSignal.timeout(8000) });
    out.agentrouter = r.ok ? 'ok' : `http_${r.status}`;
  } catch (e) { out.agentrouter = 'down'; }
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
        const py = process.env.TTS_PYTHON || '/Users/mick/Documents/Workspaces/ui/env/bin/python';
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
      const model = body.model || 'deepseek-v4-flash';
      const messages = body.messages || [];
      const stream = !!body.stream;

      // Vertex-native model → always Vertex
      if (model === VERTEX_MODEL) {
        await vertexChat(messages, body, stream, res);
        return;
      }

      // ---- Language router (B43) ----
      // AgentRouter's catalog is effectively English-only: it returns
      // `content-blocked` on Spanish/non-English content (verified live across
      // deepseek/glm/claude). Gemini (Vertex) accepts any language AND — since
      // our schema sanitizer + thoughtSignature loop landed — completes full
      // function-calling rounds in those languages too. So when a request is
      // predominantly non-English we route the WHOLE conversation to Vertex,
      // even tool requests (previously tool requests were barred from Vertex
      // because schemas/history broke the loop; that's now fixed). English
      // stays on the user-selected AgentRouter model.
      if (shouldRouteToVertex(messages)) {
        console.error(`[bridge] language router: routing ${model} -> Vertex (non-English content)`);
        await vertexChat(messages, { ...body, max_tokens: body.max_tokens || 2048 }, stream, res);
        return;
      }

      // AgentRouter model → AR first, Vertex fallback on failure
      const arUrl = `${AR_BASE}/chat/completions`;
      const arHeaders = {
        Authorization: `Bearer ${AR_API_KEY}`,
        'Content-Type': 'application/json',
        ...AR_HEADERS,
      };
      try {
        const arResp = await fetch(arUrl, { method: 'POST', headers: arHeaders, body: JSON.stringify(body), signal: AbortSignal.timeout(180_000) });
        if (!arResp.ok) {
          let detail = '';
          try { detail = (await arResp.text()).slice(0, 500); } catch (_) { /* ignore */ }
          throw new Error(`AgentRouter HTTP ${arResp.status}${detail ? ': ' + detail : ''}`);
        }
        if (stream) {
          // transparent SSE passthrough
          res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', Connection: 'keep-alive' });
          const reader = arResp.body.getReader();
          const decoder = new TextDecoder();
          try {
            while (true) {
              const { done, value } = await reader.read();
              if (done) break;
              res.write(decoder.decode(value, { stream: true }));
            }
            res.end();
          } catch (e) {
            res.end();
          }
          return;
        }
        // non-stream passthrough
        const j = await arResp.json();
        res.writeHead(200, { 'Content-Type': 'application/json' });
        return res.end(JSON.stringify(j));
      } catch (e) {
        // Vertex/Gemini cannot safely continue a FUNCTION-CALLING conversation:
        //  (a) several of our tool JSON schemas (e.g. exclusiveMinimum / nested
        //      "type") are rejected by Gemini's function_declarations validator,
        //  (b) history from a provider-switch mid function-loop breaks Gemini's
        //      "function response parts == function call parts" rule.
        // So for tool requests we do NOT fall back to Vertex — we surface the real
        // AgentRouter error instead (previously it was swallowed and replaced by a
        // misleading Vertex 400). Plain (tool-less) requests still fall back fine.
        const hadTools = Array.isArray(body.tools) && body.tools.length > 0;
        const hadToolTurn = (messages || []).some(
          (m) => m.role === 'tool' || ((m.role === 'assistant') && Array.isArray(m.tool_calls) && m.tool_calls.length > 0)
        );
        if (hadTools || hadToolTurn) {
          console.error(`[bridge] agentrouter failed on a function-calling request (no vertex fallback): ${e.message}`);
          res.writeHead(502, { 'Content-Type': 'application/json' });
          return res.end(
            JSON.stringify({
              error: {
                message:
                  'AgentRouter upstream error on a tool request (no Vertex fallback): ' + String(e.message) +
                  ' — please retry; if it persists the model gateway is the problem.',
              },
            })
          );
        }
        console.error(`[bridge] agentrouter failed, falling back to Vertex: ${e.message}`);
        const fbMsg = messages.map((m) => ({ ...m, content: typeof m.content === 'string' ? m.content : JSON.stringify(m.content) }));
        await vertexChat(fbMsg, { ...body, max_tokens: body.max_tokens || 2048 }, stream, res);
        return;
      }
    }

    res.writeHead(404, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: { message: `no route ${req.method} ${pathname}` } }));
  } catch (e) {
    res.writeHead(500, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: { message: String(e.message) } }));
  }
});

server.listen(PORT, HOST, () => {
  console.log(`bridge listening on http://${HOST}:${PORT} (region ${VERTEX_REGION}, vertex=${VERTEX_MODEL}, vertexProviders=${VERTEX_PROVIDERS.map((p) => p.name).join('+')}, agentrouter=${AR_MODELS.join(',')})`);
});
