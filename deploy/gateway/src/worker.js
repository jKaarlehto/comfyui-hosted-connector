import { DurableObject } from 'cloudflare:workers';

const ID = /^[a-f0-9]{32}$/;
const TOKEN = /^[a-f0-9]{64}$/;
const TERMINAL = new Set(['COMPLETED', 'FAILED', 'CANCELLED', 'TIMED_OUT']);
const STATUS = new Set(['IN_QUEUE', 'IN_PROGRESS', ...TERMINAL]);
const STARTUP_MESSAGES = new Set([
  'Checking for updates', 'Downloading Notch plugin', 'Starting connection services',
  'Updating ComfyUI', 'Installing ComfyUI dependencies', 'Preparing saved files', 'Starting ComfyUI',
  'Could not check for updates; the owner must check GitHub access',
  'Could not download the Notch plugin; the owner must check GitHub access and the deploy key',
  'Could not start connection services; contact the owner',
  'Could not update ComfyUI; the owner must check GitHub access',
  'Could not install ComfyUI dependencies; contact the owner',
  'Could not prepare persistent files; the owner must check storage',
  'ComfyUI failed to start; the owner must check the startup log',
  'Runpod is taking longer to respond; checking again shortly',
  'Waiting for GPU capacity; retrying once a minute',
  'Applying updated startup settings before starting ComfyUI', 'Starting hosted ComfyUI',
  'Waiting for GPU capacity within the configured price limit',
  'Another recovery request is being reconciled',
  'That GPU is unavailable; checking the next configured GPU',
  'Runpod rejected the replacement configuration; the owner must check the starter settings',
  'Checking whether Runpod accepted the replacement',
  'Preparing a replacement GPU and restoring persistent files',
  'Reconciling the replacement with Runpod before retrying',
  'Stopping a duplicate recovery allocation', 'Retiring a duplicate recovery allocation',
  'The replacement configuration or price differs; it is stopped for owner review',
  'Starting the replacement GPU', 'The replacement GPU became unavailable; trying another',
  'Stopping the original Pod before completing recovery', 'Waiting for SSH',
  'Checking ComfyUI readiness on the next connection request',
  "Waiting for the server's startup status",
]);

class RequestError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

function fail(status, message) { throw new RequestError(status, message); }
function now() { return Math.floor(Date.now() / 1000); }
function validHex(value, length) {
  return typeof value === 'string' && value.length === length && (length === 32 ? ID : TOKEN).test(value);
}
function validJob(value) {
  return typeof value === 'string' && value.length > 0 && value.length <= 128 && !/[^a-zA-Z0-9_-]/.test(value);
}
function random(bytes) {
  return Array.from(crypto.getRandomValues(new Uint8Array(bytes)), x => x.toString(16).padStart(2, '0')).join('');
}
async function digest(value) {
  const bytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value));
  return Array.from(new Uint8Array(bytes), x => x.toString(16).padStart(2, '0')).join('');
}
function equal(a, b) {
  if (a.length !== b.length) return false;
  let difference = 0;
  for (let i = 0; i < a.length; ++i) difference |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return difference === 0;
}
function reply(value, status = 200) {
  return Response.json(value, { status, headers: {
    'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'no-referrer', 'Content-Security-Policy': "default-src 'none'",
  } });
}
async function readJson(stream, limit) {
  if (!stream) fail(400, 'A JSON object is required');
  const reader = stream.getReader();
  const chunks = [];
  let length = 0;
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new RequestError(408, 'Request timed out')), 10000);
  });
  try {
    for (;;) {
      const { value, done } = await Promise.race([reader.read(), timeout]);
      if (done) break;
      length += value.length;
      if (length > limit) fail(413, 'Request is too large');
      chunks.push(value);
    }
  } finally {
    clearTimeout(timer);
    await reader.cancel().catch(() => {});
  }
  const bytes = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
  let result;
  try { result = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes)); }
  catch { fail(400, 'Invalid JSON'); }
  if (!result || typeof result !== 'object' || Array.isArray(result)) fail(400, 'A JSON object is required');
  return result;
}
async function body(request, allowed) {
  if (!/^application\/json(?:\s*;.*)?$/i.test(request.headers.get('Content-Type') || '')) fail(415, 'JSON is required');
  const value = await readJson(request.body, 8192);
  if (Object.keys(value).some(key => !allowed.includes(key))) fail(400, 'Unexpected request field');
  return value;
}
function name(value, fallback = '') {
  if (value === undefined) return fallback;
  if (typeof value !== 'string' || value.length > 80 || /[\x00-\x1f\x7f-\x9f]/.test(value)) fail(400, 'Invalid name');
  return value.trim();
}
function publicKey(value) {
  if (typeof value !== 'string' || value.length > 256) fail(400, 'Invalid SSH public key');
  const match = /^ssh-ed25519 ([A-Za-z0-9+/]+={0,2})(?: [^\r\n\x00-\x1f\x7f]{0,128})?$/.exec(value);
  if (!match || match[0] !== value) fail(400, 'An Ed25519 SSH public key is required');
  let bytes;
  try { bytes = Uint8Array.from(atob(match[1]), c => c.charCodeAt(0)); }
  catch { fail(400, 'Invalid SSH public key'); }
  const prefix = [0, 0, 0, 11, ...new TextEncoder().encode('ssh-ed25519'), 0, 0, 0, 32];
  if (bytes.length !== 51 || prefix.some((x, i) => bytes[i] !== x)) fail(400, 'Invalid SSH public key');
  return 'ssh-ed25519 ' + btoa(String.fromCharCode(...bytes));
}
function bearer(request) {
  const match = /^Bearer ([A-Za-z0-9_-]{32,512})$/.exec(request.headers.get('Authorization') || '');
  if (!match) fail(401, 'Authentication required');
  return match[1];
}
function cleanOutput(output) {
  if (!output || typeof output !== 'object') return { state: 'unavailable', message: 'The hosted server could not complete this request' };
  if (output.state === 'ready') {
    const host = output.host;
    const ipv4 = typeof host === 'string' && host.split('.').length === 4 && host.split('.').every(x => /^\d{1,3}$/.test(x) && Number(x) <= 255);
    const ipv6 = typeof host === 'string' && host.includes(':') && /^[a-fA-F0-9:]{2,45}$/.test(host);
    if (!(ipv4 || ipv6) || !Number.isInteger(output.ssh_port) || output.ssh_port < 1 || output.ssh_port > 65535 ||
        !Number.isInteger(output.comfy_port) || output.comfy_port < 1 || output.comfy_port > 65535) fail(502, 'Invalid hosted server response');
    let hostKey;
    try { hostKey = publicKey(output.host_key); } catch { fail(502, 'Invalid hosted server response'); }
    return { state: 'ready', host, ssh_port: output.ssh_port, comfy_port: output.comfy_port, host_key: hostKey };
  }
  const state = output.state === 'starting' ? 'starting' : 'unavailable';
  return { state, message: STARTUP_MESSAGES.has(output.message) ? output.message :
    state === 'starting' ? 'Starting hosted ComfyUI' : 'The hosted server is unavailable; contact the owner' };
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.protocol !== 'https:' || url.search) return reply({ error: 'Request is not allowed' }, 403);
    const origin = request.headers.get('Origin');
    if (origin !== null) {
      if (origin !== env.INVITATION_ORIGIN || url.pathname !== '/v1/invitations/status' ||
          !['POST', 'OPTIONS'].includes(request.method)) return reply({ error: 'Request is not allowed' }, 403);
      const headers = { 'Access-Control-Allow-Origin': origin, Vary: 'Origin', 'Cache-Control': 'no-store' };
      if (request.method === 'OPTIONS') {
        if (request.headers.get('Access-Control-Request-Method') !== 'POST' ||
            (request.headers.get('Access-Control-Request-Headers') || '').toLowerCase() !== 'content-type') return reply({ error: 'Request is not allowed' }, 403);
        return new Response(null, { status: 204, headers: { ...headers,
          'Access-Control-Allow-Methods': 'POST', 'Access-Control-Allow-Headers': 'Content-Type', 'Access-Control-Max-Age': '600' } });
      }
      const response = await env.WORKSPACE.get(env.WORKSPACE.idFromName('workspace')).fetch(request);
      const result = new Response(response.body, response);
      for (const [key, value] of Object.entries(headers)) result.headers.set(key, value);
      return result;
    }
    if (url.pathname.length > 200 || !url.pathname.startsWith('/v1/')) return reply({ error: 'Not found' }, 404);
    return env.WORKSPACE.get(env.WORKSPACE.idFromName('workspace')).fetch(request);
  },
};

export class Workspace extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    this.sql = ctx.storage.sql;
    this.sql.exec(`
      CREATE TABLE IF NOT EXISTS invites (
        id TEXT PRIMARY KEY, token_hash TEXT NOT NULL, label TEXT NOT NULL,
        created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, revoked_at INTEGER, device_id TEXT
      );
      CREATE TABLE IF NOT EXISTS devices (
        id TEXT PRIMARY KEY, credential_hash TEXT NOT NULL UNIQUE, public_key TEXT NOT NULL UNIQUE,
        device_name TEXT NOT NULL, created_at INTEGER NOT NULL, revoked_at INTEGER, invite_id TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, device_id TEXT NOT NULL, created_at INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS connections (device_id TEXT PRIMARY KEY, job_id TEXT, created_at INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS limits (id TEXT PRIMARY KEY, window INTEGER NOT NULL, count INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS meta (id TEXT PRIMARY KEY, value INTEGER NOT NULL);
      INSERT OR IGNORE INTO meta VALUES ('revision', 0);
    `);
  }

  one(query, ...args) { return this.sql.exec(query, ...args).toArray()[0]; }
  workspaceName() {
    const value = this.env.WORKSPACE_NAME || 'ComfyUI Notch';
    if (typeof value !== 'string' || !value.trim() || value.length > 80 || /[\x00-\x1f\x7f-\x9f]/.test(value)) fail(503, 'The workspace name is not configured');
    return value.trim();
  }
  limit(id, maximum) {
    const window = Math.floor(now() / 60);
    this.sql.exec('DELETE FROM limits WHERE window < ?', window);
    const row = this.one('SELECT count FROM limits WHERE id = ?', id);
    if (row && row.count >= maximum) fail(429, 'Too many requests; try again shortly');
    this.sql.exec('INSERT INTO limits VALUES (?, ?, 1) ON CONFLICT(id) DO UPDATE SET count = count + 1', id, window);
  }
  revision() { this.sql.exec("UPDATE meta SET value = value + 1 WHERE id = 'revision'"); }
  async secret(request, key) {
    const expected = this.env[key];
    if (!expected || expected.length < 32) fail(503, 'The gateway is not configured');
    if (!equal(await digest(bearer(request)), await digest(expected))) fail(401, 'Authentication required');
  }
  activeDevice(id, hash) {
    const device = this.one('SELECT * FROM devices WHERE id = ?', id);
    if (!device || !equal(device.credential_hash, hash)) fail(401, 'Device authentication required');
    if (device.revoked_at !== null) fail(403, 'This device has been revoked');
    return device;
  }
  async device(request) {
    const id = request.headers.get('X-Device-ID') || '';
    const token = bearer(request);
    if (!validHex(id, 32) || !validHex(token, 64)) fail(401, 'Device authentication required');
    const hash = await digest(token);
    this.activeDevice(id, hash);
    return { id, hash };
  }

  async fetch(request) {
    try {
      this.limit('all', 1800);
      const ip = request.headers.get('CF-Connecting-IP') || 'unknown';
      const ipHash = await digest(ip.slice(0, 64));
      this.limit('ip:' + ipHash, 300);
      const path = new URL(request.url).pathname;
      if (path === '/v1/invitations/status' && request.method === 'POST') {
        this.limit('invite-status:' + ipHash, 40);
        return await this.invitationStatus(request);
      }
      if (path.startsWith('/v1/admin/')) {
        await this.secret(request, 'GATEWAY_ADMIN_KEY');
        return await this.admin(request, path);
      }
      if (path === '/v1/server/keys' && request.method === 'GET') {
        await this.secret(request, 'GATEWAY_SERVER_KEY');
        const keys = this.sql.exec('SELECT id AS device_id, public_key FROM devices WHERE revoked_at IS NULL ORDER BY id').toArray();
        return reply({ revision: this.one("SELECT value FROM meta WHERE id = 'revision'").value, lease_seconds: 300, keys });
      }
      if (path === '/v1/enroll' && request.method === 'POST') {
        this.limit('enroll:' + ipHash, 40);
        return await this.enroll(request);
      }
      if (path === '/v1/connect' && request.method === 'POST') {
        const device = await this.device(request);
        await body(request, []);
        this.activeDevice(device.id, device.hash);
        this.limit('connect:' + device.id, 30);
        return await this.connect(device);
      }
      const match = /^\/v1\/jobs\/([A-Za-z0-9_-]{1,128})(\/cancel)?$/.exec(path);
      if (match && request.method === (match[2] ? 'POST' : 'GET')) {
        const device = await this.device(request);
        if (match[2]) await body(request, []);
        this.activeDevice(device.id, device.hash);
        this.limit('poll:' + device.id, 120);
        return await this.job(device, match[1], !!match[2]);
      }
      fail(404, 'Not found');
    } catch (error) {
      return reply({ error: error instanceof RequestError ? error.message : 'The gateway could not complete this request' },
        error instanceof RequestError ? error.status : 500);
    }
  }

  async admin(request, path) {
    if (path === '/v1/admin/invites' && request.method === 'POST') {
      const data = await body(request, ['label', 'expires_in_seconds']);
      const workspace = this.workspaceName();
      const label = name(data.label);
      const lifetime = data.expires_in_seconds ?? 604800;
      if (!Number.isInteger(lifetime) || lifetime < 60 || lifetime > 2592000) fail(400, 'Invalid invitation lifetime');
      const id = random(16), token = random(32), hash = await digest(token);
      if (this.one('SELECT count(*) AS count FROM invites').count >= 4096) fail(409, 'Invitation limit reached');
      const created = now(), expires = created + lifetime;
      this.sql.exec('INSERT INTO invites VALUES (?, ?, ?, ?, ?, NULL, NULL)', id, hash, label, created, expires);
      return reply({ invite_id: id, token, expires_at: expires, workspace_name: workspace }, 201);
    }
    if (path === '/v1/admin/invites' && request.method === 'GET') {
      const invites = this.sql.exec('SELECT id AS invite_id, label, created_at, expires_at, revoked_at, device_id FROM invites ORDER BY created_at DESC, id').toArray();
      return reply({ invites: invites.map(({ revoked_at, ...item }) => ({ ...item, state: revoked_at !== null ? 'revoked' :
        item.device_id ? 'used' : item.expires_at <= now() ? 'expired' : 'unused' })) });
    }
    if (path === '/v1/admin/devices' && request.method === 'GET') {
      return reply({ devices: this.sql.exec('SELECT id AS device_id, device_name, created_at, revoked_at, invite_id FROM devices ORDER BY created_at DESC, id').toArray() });
    }
    const match = /^\/v1\/admin\/(invites|devices)\/([a-f0-9]{32})\/revoke$/.exec(path);
    if (match && request.method === 'POST') {
      await body(request, []);
      const table = match[1];
      const row = this.one(`SELECT revoked_at FROM ${table} WHERE id = ?`, match[2]);
      if (!row) fail(404, 'Not found');
      if (row.revoked_at === null) {
        this.ctx.storage.transactionSync(() => {
          this.sql.exec(`UPDATE ${table} SET revoked_at = ? WHERE id = ?`, now(), match[2]);
          if (table === 'devices') this.revision();
        });
      }
      return reply({ revoked: true });
    }
    fail(404, 'Not found');
  }

  async invitationStatus(request) {
    const data = await body(request, ['invite_id', 'token_hash']);
    if (!validHex(data.invite_id, 32) || !validHex(data.token_hash, 64)) fail(400, 'Invalid invitation');
    const invite = this.one('SELECT * FROM invites WHERE id = ?', data.invite_id);
    if (!invite || !equal(invite.token_hash, data.token_hash)) return reply({ state: 'invalid' });
    if (invite.device_id) {
      const device = this.one('SELECT revoked_at FROM devices WHERE id = ?', invite.device_id);
      return reply({ state: device && device.revoked_at === null ? 'redeemed' : 'revoked' });
    }
    return reply({ state: invite.revoked_at !== null ? 'revoked' : invite.expires_at <= now() ? 'expired' : 'unused' });
  }

  async enroll(request) {
    const data = await body(request, ['invite_id', 'token', 'public_key', 'credential_hash', 'device_name']);
    const workspace = this.workspaceName();
    if (!validHex(data.invite_id, 32) || !validHex(data.token, 64) || !validHex(data.credential_hash, 64)) fail(400, 'Invalid enrollment');
    const key = publicKey(data.public_key), deviceName = name(data.device_name, 'Tester');
    const tokenHash = await digest(data.token);
    const deviceId = this.ctx.storage.transactionSync(() => {
      const invite = this.one('SELECT * FROM invites WHERE id = ?', data.invite_id);
      if (!invite || !equal(invite.token_hash, tokenHash)) fail(401, 'Invalid invitation');
      if (invite.revoked_at !== null) fail(403, 'This invitation has been revoked');
      if (invite.device_id) {
        const existing = this.one('SELECT * FROM devices WHERE id = ?', invite.device_id);
        if (!existing || existing.public_key !== key || !equal(existing.credential_hash, data.credential_hash)) fail(409, 'This invitation has already been used');
        if (existing.revoked_at !== null) fail(403, 'This device has been revoked');
        return existing.id;
      }
      if (invite.expires_at <= now()) fail(410, 'This invitation has expired');
      if (this.one('SELECT count(*) AS count FROM devices WHERE revoked_at IS NULL').count >= 128 ||
          this.one('SELECT count(*) AS count FROM devices').count >= 4096) fail(409, 'Device limit reached');
      if (this.one('SELECT id FROM devices WHERE public_key = ? OR credential_hash = ?', key, data.credential_hash)) fail(409, 'This device credential is already registered');
      const id = random(16);
      this.sql.exec('INSERT INTO devices VALUES (?, ?, ?, ?, ?, NULL, ?)', id, data.credential_hash, key, deviceName, now(), invite.id);
      this.sql.exec('UPDATE invites SET device_id = ? WHERE id = ?', id, invite.id);
      this.revision();
      return id;
    });
    return reply({ device_id: deviceId, workspace_name: workspace });
  }

  async provider(path, method = 'GET', value) {
    const endpoint = this.env.RUNPOD_STARTER_ID;
    if (!/^[a-zA-Z0-9-]{1,64}$/.test(endpoint || '') || !this.env.RUNPOD_STARTER_KEY) fail(503, 'The starter is not configured');
    try {
      const response = await fetch('https://api.runpod.ai/v2/' + endpoint + path, {
        method, redirect: 'manual', signal: AbortSignal.timeout(10000),
        headers: { Authorization: 'Bearer ' + this.env.RUNPOD_STARTER_KEY, 'Content-Type': 'application/json' },
        body: value === undefined ? undefined : JSON.stringify(value),
      });
      if (!response.ok) fail(502, 'The hosted starter is unavailable; try again shortly');
      return await readJson(response.body, 65536);
    } catch { fail(502, 'The hosted starter is unavailable; try again shortly'); }
  }

  async connect(device) {
    const time = now();
    this.sql.exec('DELETE FROM jobs WHERE created_at < ?', time - 86400);
    this.sql.exec('DELETE FROM connections WHERE created_at < ?', time - 600);
    const existing = this.one('SELECT * FROM connections WHERE device_id = ?', device.id);
    if (existing?.job_id) return reply({ id: existing.job_id, status: 'IN_QUEUE' });
    if (existing && time - existing.created_at < 120) fail(409, 'A connection request is already being submitted; try again shortly');
    if (this.one('SELECT count(*) AS count FROM jobs').count >= 4096) fail(429, 'Connection limit reached; try again later');
    this.sql.exec('INSERT INTO connections VALUES (?, NULL, ?) ON CONFLICT(device_id) DO UPDATE SET job_id = NULL, created_at = excluded.created_at', device.id, time);
    const result = await this.provider('/run', 'POST', { input: { action: 'connect' } });
    if (!validJob(result.id)) fail(502, 'Invalid hosted starter response');
    this.ctx.storage.transactionSync(() => {
      const previous = this.one('SELECT device_id FROM jobs WHERE id = ?', result.id);
      if (previous && previous.device_id !== device.id) fail(502, 'Invalid hosted starter response');
      this.sql.exec('INSERT OR IGNORE INTO jobs VALUES (?, ?, ?)', result.id, device.id, time);
      this.sql.exec('UPDATE connections SET job_id = ? WHERE device_id = ?', result.id, device.id);
    });
    this.activeDevice(device.id, device.hash);
    return reply({ id: result.id, status: 'IN_QUEUE' });
  }

  async job(device, id, cancel) {
    const job = this.one('SELECT * FROM jobs WHERE id = ? AND device_id = ?', id, device.id);
    if (!job || job.created_at < now() - 86400) fail(404, 'Job not found');
    const result = await this.provider((cancel ? '/cancel/' : '/status/') + id, cancel ? 'POST' : 'GET');
    this.activeDevice(device.id, device.hash);
    if (result.id !== id || !STATUS.has(result.status)) fail(502, 'Invalid hosted starter response');
    if (TERMINAL.has(result.status)) this.sql.exec('DELETE FROM connections WHERE device_id = ? AND job_id = ?', device.id, id);
    const safe = { id, status: result.status };
    if (result.status === 'COMPLETED') safe.output = cleanOutput(result.output);
    else if (TERMINAL.has(result.status) && result.status !== 'CANCELLED') safe.error = 'The hosted starter could not complete this request';
    return reply(safe);
  }
}
