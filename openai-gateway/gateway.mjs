import http from 'node:http';
import { spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { StringDecoder } from 'node:string_decoder';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const LOG_FILE = process.env.GATEWAY_LOG || path.join(HERE, 'gateway.log');

function logLine(msg) {
  const line = `${new Date().toISOString()} ${msg}\n`;
  try { fs.appendFileSync(LOG_FILE, line); } catch { /* logging must never break a request */ }
  console.log(line.trim());
}

const CFG = {
  host: process.env.GATEWAY_HOST || '127.0.0.1',
  port: Number(process.env.GATEWAY_PORT || 8787),
  key: process.env.GATEWAY_KEY || '',
  allowAnonymous: process.env.GATEWAY_ALLOW_ANONYMOUS === '1',
  cli: process.env.QODER_CLI_PATH || path.join(os.homedir(), '.qoder', 'bin', 'qodercli', 'qodercli.exe'),
  defaultModel: process.env.GATEWAY_DEFAULT_MODEL || 'Qwen3.8-Flash',
  tools: process.env.GATEWAY_TOOLS ?? '',
  maxParallel: Number(process.env.GATEWAY_MAX_PARALLEL || 3),
  timeoutMs: Number(process.env.GATEWAY_TIMEOUT_MS || 180000),
  maxBodyBytes: Number(process.env.GATEWAY_MAX_BODY_BYTES || 4 * 1024 * 1024),
  cwd: process.env.GATEWAY_CWD || process.cwd(),
  emitThinking: process.env.GATEWAY_EMIT_THINKING === '1',
  effort: (process.env.GATEWAY_REASONING_EFFORT || '').trim().toLowerCase(),
  contextWindow: Number(process.env.GATEWAY_CONTEXT_WINDOW || 0),
};

const EFFORTS = ['auto', 'none', 'low', 'medium', 'high', 'xhigh', 'max', 'ultracode'];

const SDK_ENV = [
  'QODER_AGENT_SDK_ENTRYPOINT', 'QODER_AGENT_SDK_VERSION', 'QODER_CLI',
  'QODER_SDK_AUTH_PAYLOAD_FILE', 'QODER_SDK_CUSTOM_BASE_URL_BYOK', 'QODER_SESSION_TYPE',
  'QODER_WORKER_CWD', 'QODER_WORKER_RUNTIME_ASSET_ROOT', 'QODER_CLI_MAX_SUBAGENT_SPAWN_DEPTH',
];

function fail(res, status, message, code = 'gateway_error') {
  logLine(`err ${status} ${code} ${String(message).slice(0, 160)}`);
  const body = JSON.stringify({ error: { message, type: status >= 500 ? 'server_error' : 'invalid_request_error', code } });
  if (!res.headersSent) res.writeHead(status, { 'content-type': 'application/json', 'content-length': Buffer.byteLength(body) });
  res.end(body);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    req.on('data', (c) => {
      size += c.length;
      if (size > CFG.maxBodyBytes) { reject(Object.assign(new Error('request body too large'), { status: 413 })); req.destroy(); return; }
      chunks.push(c);
    });
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    req.on('error', reject);
  });
}

function partText(content) {
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return '';
  return content.map((p) => (typeof p === 'string' ? p : p?.type === 'text' ? p.text : '')).filter(Boolean).join('\n');
}

// qodercli принимает картинки только как image-блоки в stdin-сообщении:
// --attachment в stream-json режиме игнорируется.
function imageBlocks(messages) {
  const blocks = [];
  for (const message of messages) {
    if (!Array.isArray(message?.content)) continue;
    for (const part of message.content) {
      if (!part || typeof part !== 'object') continue;
      if (part.type === 'image' && part.source?.data) {
        blocks.push({
          type: 'image',
          source: {
            type: 'base64',
            media_type: part.source.media_type || 'image/png',
            data: part.source.data,
          },
        });
        continue;
      }
      const match = /^data:(image\/[a-z0-9.+-]+);base64,(.+)$/i.exec(String(part.image_url?.url || '').trim());
      if (match) blocks.push({ type: 'image', source: { type: 'base64', media_type: match[1].toLowerCase(), data: match[2] } });
    }
  }
  return blocks;
}

function toPrompt(messages) {
  const system = messages.filter((m) => m.role === 'system' || m.role === 'developer').map((m) => partText(m.content)).filter(Boolean);
  const turns = messages.filter((m) => m.role !== 'system' && m.role !== 'developer');
  const transcript = turns.map((m) => `${m.role}:\n${partText(m.content)}`).join('\n\n');
  return [
    system.length ? `Instructions from the caller:\n${system.join('\n\n')}\n` : '',
    'You are answering through a plain chat completions API.',
    'Reply only to the final user turn, in the language of that turn.',
    '',
    'Conversation so far:',
    transcript,
    '',
    'Answer the final user turn now:',
  ].join('\n');
}

function cliArgs({ model, maxTokens, effort, contextWindow }) {
  // --include-partial-messages скрыт из --help, но именно он даёт thinking_delta/text_delta
  const args = ['-p', '-m', model, '--input-format', 'stream-json', '--output-format', 'stream-json', '--include-partial-messages', '--no-session-persistence', '--tools', CFG.tools, '--cwd', CFG.cwd];
  if (maxTokens) args.push('--max-output-tokens', String(maxTokens));
  if (effort) args.push('--reasoning-effort', effort);
  if (contextWindow) args.push('--context-window', String(contextWindow));
  return args;
}

function runQoder(args, stdinText, onEvent) {
  return new Promise((resolve, reject) => {
    const env = { ...process.env };
    for (const k of SDK_ENV) delete env[k];
    const child = spawn(CFG.cli, args, { env, cwd: CFG.cwd, windowsHide: true });
    child.stdin.on('error', () => { /* CLI may close stdin before we finish writing */ });
    child.stdin.end(stdinText);
    let stdoutBuf = '';
    let stderr = '';
    const stdoutDecoder = new StringDecoder('utf8');
    const stderrDecoder = new StringDecoder('utf8');
    let settled = false;
    const finish = (fn, arg) => { if (!settled) { settled = true; clearTimeout(timer); fn(arg); } };
    const timer = setTimeout(() => { child.kill('SIGKILL'); finish(reject, Object.assign(new Error(`qodercli timed out after ${CFG.timeoutMs}ms`), { status: 504 })); }, CFG.timeoutMs);

    child.on('error', (err) => finish(reject, Object.assign(new Error(`failed to start qodercli: ${err.message}`), { status: 500 })));
    child.stderr.on('data', (c) => { stderr += stderrDecoder.write(c); });
    child.stdout.on('data', (c) => {
      stdoutBuf += stdoutDecoder.write(c);
      let nl;
      while ((nl = stdoutBuf.indexOf('\n')) >= 0) {
        const line = stdoutBuf.slice(0, nl).trim();
        stdoutBuf = stdoutBuf.slice(nl + 1);
        if (!line) continue;
        let evt;
        try { evt = JSON.parse(line); } catch { continue; }
        onEvent(evt);
      }
    });
    child.on('close', (code) => {
      stdoutBuf += stdoutDecoder.end();
      stderr += stderrDecoder.end();
      if (stdoutBuf.trim()) { try { onEvent(JSON.parse(stdoutBuf.trim())); } catch { /* partial line */ } }
      finish(resolve, { code, stderr });
    });
  });
}

// The CLI re-sends a message snapshot per content block, so deltas are computed against what was already emitted for that message id.
function makeAccumulator() {
  const emitted = new Map();
  const state = { text: '', thinking: '', result: null, usage: null, error: null, model: null, credits: null, billable: null, streamed: false };
  return {
    state,
    push(evt) {
      if (evt.type === 'stream_event') {
        const delta = evt.event?.delta;
        if (!delta) return { text: '', thinking: '' };
        if (delta.type === 'thinking_delta' && delta.thinking) {
          state.streamed = true;
          state.thinking += delta.thinking;
          return { text: '', thinking: delta.thinking };
        }
        if (delta.type === 'text_delta' && delta.text) {
          state.streamed = true;
          state.text += delta.text;
          return { text: delta.text, thinking: '' };
        }
        return { text: '', thinking: '' };
      }
      if (evt.type === 'assistant') {
        const blocks = evt.message?.content || [];
        const id = evt.message?.id || evt.uuid;
        if (evt.message?.model) state.model = evt.message.model;
        if (evt.message?.usage?.credits != null) state.credits = evt.message.usage.credits;
        if (evt.message?.usage?.billable != null) state.billable = evt.message.usage.billable;
        // при партиальных дельтах snapshots дублируют уже отданный текст — берём из них только метаданные
        if (state.streamed) return { text: '', thinking: '' };
        let text = '';
        let thinking = '';
        for (const b of blocks) {
          if (b.type === 'text') text += b.text || '';
          else if (b.type === 'thinking') thinking += b.thinking || '';
        }
        const prev = emitted.get(id) || { text: '', thinking: '' };
        const textDelta = text && text.startsWith(prev.text) ? text.slice(prev.text.length) : text && !prev.text.endsWith(text) ? text : '';
        const thinkDelta = thinking && thinking.startsWith(prev.thinking) ? thinking.slice(prev.thinking.length) : '';
        emitted.set(id, { text: prev.text + textDelta, thinking: prev.thinking + thinkDelta });
        state.text += textDelta;
        state.thinking += thinkDelta;
        return { text: textDelta, thinking: thinkDelta };
      }
      if (evt.type === 'result') {
        state.result = evt.result || '';
        state.usage = evt.usage || null;
        if (evt.is_error || evt.subtype !== 'success') state.error = evt.result || evt.subtype || 'agent error';
        return { text: '', thinking: '' };
      }
      return { text: '', thinking: '' };
    },
  };
}

let running = 0;
const waiting = [];
function acquireSlot() {
  return new Promise((resolve, reject) => {
    const start = () => {
      if (running < CFG.maxParallel) {
        running += 1;
        resolve(() => { running -= 1; const next = waiting.shift(); if (next) next(); });
      } else waiting.push(start);
    };
    if (waiting.length >= CFG.maxParallel * 8) { reject(Object.assign(new Error('gateway queue full'), { status: 429 })); return; }
    start();
  });
}

let modelsCache = { at: 0, list: [] };
async function listModels() {
  if (Date.now() - modelsCache.at < 300000 && modelsCache.list.length) return modelsCache.list;
  const env = { ...process.env };
  for (const k of SDK_ENV) delete env[k];
  const out = await new Promise((resolve, reject) => {
    const child = spawn(CFG.cli, ['--list-models'], { env, cwd: CFG.cwd, windowsHide: true });
    let buf = '';
    child.on('error', reject);
    child.stdout.on('data', (c) => { buf += c; });
    child.on('close', (code) => (code === 0 ? resolve(buf) : reject(new Error(`--list-models exit ${code}: ${buf.slice(0, 300)}`))));
  });
  const names = out.split(/\r?\n/).map((l) => l.trim()).filter((l) => l && l !== 'MODEL');
  modelsCache = { at: Date.now(), list: names };
  return names;
}

// CLI отдаёт usage=0 для этой модели, поэтому считаем по символам (кириллица ~2.5 симв./токен, латиница ~4).
function estimateTokens(text) {
  let cyr = 0;
  let other = 0;
  for (const ch of text) {
    if (/[\u0400-\u04FF]/.test(ch)) cyr += 1;
    else other += 1;
  }
  return Math.max(1, Math.round(cyr / 2.5 + other / 4));
}

function usageOf(usage, promptText, resultText, state) {
  const realIn = usage?.input_tokens ?? 0;
  const realOut = usage?.output_tokens ?? 0;
  const estimated = realIn === 0 || realOut === 0;
  const inTok = realIn || estimateTokens(promptText || '');
  const outTok = realOut || estimateTokens(resultText || '');
  const out = { prompt_tokens: inTok, completion_tokens: outTok, total_tokens: inTok + outTok, estimated };
  if (state?.credits != null) out.credits = state.credits;
  if (state?.billable != null) out.billable = state.billable;
  return out;
}

async function handleChat(req, res) {
  let payload;
  try { payload = JSON.parse(await readBody(req) || '{}'); }
  catch (err) { return fail(res, err.status || 400, 'invalid JSON body', 'invalid_json'); }

  const messages = Array.isArray(payload.messages) ? payload.messages : null;
  if (!messages?.length) return fail(res, 400, 'messages[] is required', 'missing_messages');
  const model = payload.model && payload.model !== 'default' ? payload.model : CFG.defaultModel;
  const wantStream = payload.stream === true;
  const effort = String(payload.reasoning_effort ?? req.headers['x-reasoning-effort'] ?? CFG.effort ?? '').trim().toLowerCase();
  if (effort && !EFFORTS.includes(effort)) {
    return fail(res, 400, `unknown reasoning_effort "${effort}"; known: ${EFFORTS.join(', ')}`, 'unknown_effort');
  }
  const contextWindow = Math.trunc(Number(payload.context_window ?? req.headers['x-context-window'] ?? CFG.contextWindow ?? 0)) || 0;
  if (contextWindow < 0) return fail(res, 400, 'context_window must be positive', 'invalid_context_window');

  // qodercli silently downgrades an unrecognised -m value, so the requested id is checked here instead.
  let known = [];
  try { known = await listModels(); } catch { /* validation skipped if the list is unavailable */ }
  if (known.length && !known.some((m) => m.toLowerCase() === model.toLowerCase())) {
    return fail(res, 400, `unknown model "${model}"; known: ${known.join(', ')}`, 'unknown_model');
  }

  const prompt = toPrompt(messages);
  const images = imageBlocks(messages);
  const created = Math.floor(Date.now() / 1000);
  const completionId = `chatcmpl-${randomUUID()}`;

  let release;
  const t0 = Date.now();
  try { release = await acquireSlot(); }
  catch (err) { return fail(res, err.status || 429, 'too many concurrent qodercli runs, retry later', 'queue_full'); }
  req.on('aborted', () => { /* slot frees when the child exits */ });

  const acc = makeAccumulator();
  let sse = null;
  if (wantStream) {
    res.writeHead(200, {
      'content-type': 'text/event-stream; charset=utf-8',
      'cache-control': 'no-cache, no-transform',
      connection: 'keep-alive',
      'x-accel-buffering': 'no',
    });
    sse = (obj) => res.write(`data: ${JSON.stringify(obj)}\n\n`);
    sse({ id: completionId, object: 'chat.completion.chunk', created, model, choices: [{ index: 0, delta: { role: 'assistant', content: '' }, finish_reason: null }] });
  }

  let finalized = false;
  const respond = () => {
    if (finalized) return;
    finalized = true;
    const text = acc.state.text || acc.state.result || '';
    if (acc.state.error && !text) {
      if (sse) { res.write(`data: ${JSON.stringify({ error: { message: acc.state.error, type: 'server_error', code: 'cli_error' } })}\n\n`); res.write('data: [DONE]\n\n'); res.end(); return; }
      fail(res, 502, acc.state.error, 'cli_error');
      return;
    }
    const usage = usageOf(acc.state.usage, prompt, text, acc.state);
    if (sse) {
      if (!text && acc.state.result) sse({ id: completionId, object: 'chat.completion.chunk', created, model, choices: [{ index: 0, delta: { content: acc.state.result }, finish_reason: null }] });
      sse({ id: completionId, object: 'chat.completion.chunk', created, model, choices: [{ index: 0, delta: {}, finish_reason: 'stop' }] });
      // как OpenAI при stream_options.include_usage: отдельный чанк с пустыми choices
      sse({ id: completionId, object: 'chat.completion.chunk', created, model, choices: [], usage });
      res.write('data: [DONE]\n\n');
      logLine(`chat 200 stream model=${model} effort=${effort || '-'} ctx=${contextWindow || '-'} ${Date.now() - t0}ms chars=${text.length} think=${acc.state.thinking.length} est_in=${usage.prompt_tokens} est_out=${usage.completion_tokens} credits=${acc.state.credits ?? '-'}`);
      res.end();
      return;
    }
    const message = { role: 'assistant', content: text };
    if (acc.state.thinking) message.reasoning_content = acc.state.thinking;
    const body = JSON.stringify({
      id: completionId,
      object: 'chat.completion',
      created,
      model: acc.state.model || model,
      choices: [{ index: 0, message, finish_reason: 'stop' }],
      usage,
    });
    res.writeHead(200, { 'content-type': 'application/json; charset=utf-8', 'content-length': Buffer.byteLength(body) });
    logLine(`chat 200 model=${model} effort=${effort || '-'} ctx=${contextWindow || '-'} ${Date.now() - t0}ms chars=${text.length} think=${acc.state.thinking.length} est_in=${usage.prompt_tokens} est_out=${usage.completion_tokens} credits=${acc.state.credits ?? '-'}`);
    res.end(body);
  };

  try {
    const content = images.length ? [{ type: 'text', text: prompt }, ...images] : prompt;
    const stdinText = `${JSON.stringify({ type: 'user', message: { role: 'user', content } })}\n`;
    await runQoder(cliArgs({ model, maxTokens: payload.max_tokens, effort, contextWindow }), stdinText, (evt) => {
      const delta = acc.push(evt);
      if (sse && !finalized) {
        if (delta.text) sse({ id: completionId, object: 'chat.completion.chunk', created, model, choices: [{ index: 0, delta: { content: delta.text }, finish_reason: null }] });
        if (delta.thinking) sse({ id: completionId, object: 'chat.completion.chunk', created, model, choices: [{ index: 0, delta: { reasoning_content: delta.thinking }, finish_reason: null }] });
      }
      // message_stop приходит сразу после текста, result — уже после teardown CLI (~1-2 с)
      const answerDone = evt.type === 'result' || (evt.type === 'stream_event' && evt.event?.type === 'message_stop');
      if (answerDone) respond();
    });
  } catch (err) {
    release();
    if (finalized) return;   // ответ уже ушёл, падение процесса после result клиенту не важно
    const body = { error: { message: err.message || 'qodercli failed', type: 'server_error', code: 'cli_error' } };
    if (sse) { res.write(`data: ${JSON.stringify(body)}\n\n`); res.write('data: [DONE]\n\n'); return res.end(); }
    return fail(res, err.status || 502, err.message || 'qodercli failed', 'cli_error');
  }
  release();
  respond();
}

async function handleModels(res) {
  try {
    const names = await listModels();
    const body = JSON.stringify({ object: 'list', data: names.map((id) => ({ id, object: 'model', owned_by: 'qoder-cli' })) });
    res.writeHead(200, { 'content-type': 'application/json; charset=utf-8', 'content-length': Buffer.byteLength(body) });
    res.end(body);
  } catch (err) {
    fail(res, 502, `model list unavailable: ${err.message}`, 'models_error');
  }
}

const IMAGES_DIR = process.env.GATEWAY_IMAGES_DIR || path.join(HERE, 'images');
fs.mkdirSync(path.join(IMAGES_DIR, 'vibe_images'), { recursive: true });

function newestImage(dir, sinceMs) {
  let best = null;
  for (const name of fs.readdirSync(dir)) {
    const full = path.join(dir, name);
    const st = fs.statSync(full);
    if (st.isFile() && st.mtimeMs >= sinceMs - 1000 && (!best || st.mtimeMs > best.mtime)) best = { full, mtime: st.mtimeMs };
  }
  return best;
}

async function handleImages(req, res) {
  let payload;
  try { payload = JSON.parse(await readBody(req) || '{}'); }
  catch { return fail(res, 400, 'invalid JSON body', 'invalid_json'); }
  const prompt = String(payload.prompt || '').trim();
  if (!prompt) return fail(res, 400, 'prompt is required', 'missing_prompt');
  const size = /^\d{2,5}x\d{2,5}$/.test(String(payload.size || '')) ? String(payload.size) : '1024x1024';
  const model = payload.model && payload.model !== 'default' ? payload.model : CFG.defaultModel;

  let release;
  const t0 = Date.now();
  try { release = await acquireSlot(); }
  catch { return fail(res, 429, 'gateway queue full', 'queue_full'); }

  // ImageGen — встроенный инструмент CLI; чат-эндпоинт его сознательно не включает
  const args = ['-p', '-m', model, '--input-format', 'stream-json', '--output-format', 'stream-json',
    '--no-session-persistence', '--tools', 'ImageGen', '--permission-mode', 'dont_ask', '--cwd', IMAGES_DIR];
  const stdinText = `${JSON.stringify({ type: 'user', message: { role: 'user', content: `Сгенерируй изображение и сохрани файл: ${prompt}. Размер: ${size}.` } })}\n`;
  const startedAt = Date.now();
  let savedPath = null;
  try {
    await runQoder(args, stdinText, (evt) => {
      if (evt.type !== 'assistant' && evt.type !== 'message') return;
      for (const block of evt.message?.content || []) {
        if (block.type === 'tool_result' && typeof block.content === 'string') {
          const marker = block.content.split('Saved to:').pop();
          if (marker && marker !== block.content) savedPath = marker.trim();
        }
      }
    });
  } catch (err) {
    release();
    return fail(res, err.status || 502, err.message || 'image generation failed', 'image_error');
  }
  release();

  const outDir = path.join(IMAGES_DIR, 'vibe_images');
  const file = (savedPath && fs.existsSync(savedPath)) ? savedPath : newestImage(outDir, startedAt)?.full;
  if (!file) return fail(res, 502, 'ImageGen produced no file', 'image_error');

  const name = path.basename(file);
  const stats = fs.statSync(file);
  const data = {
    url: `http://${CFG.host}:${CFG.port}/files/${encodeURIComponent(name)}`,
    revised_prompt: prompt,
  };
  if (payload.response_format !== 'url') data.b64_json = fs.readFileSync(file).toString('base64');
  const body = JSON.stringify({ created: Math.floor(Date.now() / 1000), model, seconds: (Date.now() - t0) / 1000, data: [data] });
  res.writeHead(200, { 'content-type': 'application/json; charset=utf-8', 'content-length': Buffer.byteLength(body) });
  logLine(`image 200 model=${model} ${Date.now() - t0}ms file=${name} bytes=${stats.size}`);
  res.end(body);
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url, `http://${CFG.host}`);
  if (req.method === 'OPTIONS') { res.writeHead(204, { allow: 'GET, POST, OPTIONS' }); return res.end(); }

  if (url.pathname === '/health') { res.writeHead(200, { 'content-type': 'text/plain' }); return res.end('ok'); }

  const header = req.headers.authorization || '';
  const token = header.startsWith('Bearer ') ? header.slice(7) : '';
  if (!CFG.allowAnonymous && token !== CFG.key) return fail(res, 401, 'missing or invalid api key', 'invalid_api_key');

  if (req.method === 'POST' && url.pathname === '/v1/chat/completions') {
    handleChat(req, res).catch((err) => fail(res, err.status || 500, err.message || 'internal error', 'internal_error'));
    return;
  }
  if (req.method === 'POST' && url.pathname === '/v1/images/generations') {
    handleImages(req, res).catch((err) => fail(res, err.status || 500, err.message || 'internal error', 'internal_error'));
    return;
  }
  if (req.method === 'GET' && url.pathname.startsWith('/files/')) {
    const name = path.basename(decodeURIComponent(url.pathname.slice('/files/'.length)));
    const file = path.join(IMAGES_DIR, 'vibe_images', name);
    if (!fs.existsSync(file)) return fail(res, 404, 'no such file', 'not_found');
    const ext = path.extname(file).toLowerCase();
    const type = ext === '.png' ? 'image/png' : ext === '.webp' ? 'image/webp' : 'image/jpeg';
    res.writeHead(200, { 'content-type': type, 'content-length': fs.statSync(file).size });
    fs.createReadStream(file).pipe(res);
    return;
  }
  if (req.method === 'GET' && url.pathname === '/v1/models') return void handleModels(res);
  if (url.pathname === '/v1/completions') return fail(res, 404, 'only /v1/chat/completions is implemented', 'unsupported_endpoint');
  return fail(res, 404, `no route: ${req.method} ${url.pathname}`, 'not_found');
});

if (!CFG.key && !CFG.allowAnonymous) {
  console.error('GATEWAY_KEY is not set. Refusing to start an unauthenticated gateway; set GATEWAY_KEY or GATEWAY_ALLOW_ANONYMOUS=1 (localhost use only).');
  process.exit(1);
}

server.listen(CFG.port, CFG.host, () => {
  logLine(`listen http://${CFG.host}:${CFG.port} cli=${CFG.cli} parallel=${CFG.maxParallel} model=${CFG.defaultModel}`);
  if (!fs.existsSync(CFG.cli)) logLine(`warn qodercli not found at ${CFG.cli} — set QODER_CLI_PATH`);
});
