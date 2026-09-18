import assert from 'node:assert/strict';
import { createHash, randomBytes } from 'node:crypto';
import { mkdtemp, mkdir, readFile, rm } from 'node:fs/promises';
import { dirname, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import { createFetchMock, Log, LogLevel, Miniflare } from 'miniflare';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const source = await readFile(resolve(root, 'src/worker.js'), 'utf8');
const adminKey = 'a'.repeat(64), serverKey = 'b'.repeat(64), starterKey = 'c'.repeat(64);
const hash = value => createHash('sha256').update(value).digest('hex');
const publicKey = () => 'ssh-ed25519 ' + Buffer.concat([
  Buffer.from([0, 0, 0, 11]), Buffer.from('ssh-ed25519'), Buffer.from([0, 0, 0, 32]), randomBytes(32),
]).toString('base64');

function runtime(persist) {
  const mock = createFetchMock();
  mock.disableNetConnect();
  const mf = new Miniflare({
    modules: true,
    script: source + `
      export class TestWorkspace extends Workspace {
        expire(id) { this.sql.exec('UPDATE invites SET expires_at = 0 WHERE id = ?', id); }
        rows() { return { invites: this.sql.exec('SELECT * FROM invites').toArray(), devices: this.sql.exec('SELECT * FROM devices').toArray() }; }
      }
    `,
    compatibilityDate: '2026-06-11',
    durableObjects: { WORKSPACE: { className: 'TestWorkspace', useSQLite: true } },
    durableObjectsPersist: persist ?? false,
    bindings: { GATEWAY_ADMIN_KEY: adminKey, GATEWAY_SERVER_KEY: serverKey,
      RUNPOD_STARTER_KEY: starterKey, RUNPOD_STARTER_ID: 'test-endpoint', WORKSPACE_NAME: 'Test workspace' },
    fetchMock: mock,
    log: new Log(LogLevel.NONE),
  });
  let serial = 0;
  async function request(path, { method = 'GET', value, key, device, headers = {}, ip } = {}) {
    const response = await mf.dispatchFetch('https://gateway.example' + path, {
      method, headers: { 'CF-Connecting-IP': ip || '192.0.2.' + (++serial % 250 + 1),
        ...(value === undefined ? {} : { 'Content-Type': 'application/json' }),
        ...(key ? { Authorization: 'Bearer ' + key } : {}), ...(device ? { 'X-Device-ID': device } : {}), ...headers },
      body: value === undefined ? undefined : JSON.stringify(value),
    });
    return { status: response.status, headers: response.headers, data: await response.json() };
  }
  const admin = (path, method = 'GET', value) => request('/v1/admin/' + path, { method, value, key: adminKey });
  async function invitation() {
    const result = await admin('invites', 'POST', { label: 'Tester' });
    assert.equal(result.status, 201);
    return result.data;
  }
  async function enroll(invite, credential = randomBytes(32).toString('hex'), public_key = publicKey()) {
    const payload = { invite_id: invite.invite_id, token: invite.token, public_key,
      credential_hash: hash(credential), device_name: 'Device' };
    const result = await request('/v1/enroll', { method: 'POST', value: payload });
    return { ...result, credential, payload, id: result.data.device_id };
  }
  const auth = device => ({ key: device.credential, device: device.id });
  async function object() {
    const ns = await mf.getDurableObjectNamespace('WORKSPACE');
    return ns.get(ns.idFromName('workspace'));
  }
  const provider = () => mock.get('https://api.runpod.ai');
  return { mf, mock, request, admin, invitation, enroll, auth, object, provider };
}

test('concurrent redemption admits exactly one device and retry returns the same receipt', async t => {
  const r = runtime(); t.after(() => r.mf.dispose());
  const invite = await r.invitation();
  const attempts = await Promise.all(Array.from({ length: 16 }, () => r.enroll(invite)));
  const winners = attempts.filter(x => x.status === 200);
  assert.equal(winners.length, 1);
  assert.equal(attempts.filter(x => x.status === 409).length, 15);
  const winner = winners[0];
  const retry = await r.request('/v1/enroll', { method: 'POST', value: winner.payload });
  assert.deepEqual(retry.data, winner.data);
  const rows = await (await r.object()).rows();
  assert.equal(rows.devices.length, 1);
  assert.equal(rows.invites[0].device_id, winner.id);
  const stored = JSON.stringify(rows);
  assert(!stored.includes(invite.token));
  assert(!stored.includes(winner.credential));
  assert(!stored.includes('PRIVATE KEY'));
  const keys = await r.request('/v1/server/keys', { key: serverKey });
  assert.deepEqual(keys.data, { revision: 1, lease_seconds: 300,
    keys: [{ device_id: winner.id, public_key: winner.payload.public_key }] });
});

test('same-binding concurrent retries remain idempotent', async t => {
  const r = runtime(); t.after(() => r.mf.dispose());
  const invite = await r.invitation(), credential = randomBytes(32).toString('hex'), key = publicKey();
  const results = await Promise.all(Array.from({ length: 12 }, () => r.enroll(invite, credential, key)));
  assert(results.every(x => x.status === 200));
  assert.equal(new Set(results.map(x => x.id)).size, 1);
  assert.equal((await (await r.object()).rows()).devices.length, 1);
});

test('expiration, invitation revocation and device revocation have separate effects', async t => {
  const r = runtime(); t.after(() => r.mf.dispose());
  const expired = await r.invitation();
  await (await r.object()).expire(expired.invite_id);
  assert.equal((await r.enroll(expired)).status, 410);
  const revoked = await r.invitation();
  await r.admin('invites/' + revoked.invite_id + '/revoke', 'POST', {});
  assert.equal((await r.enroll(revoked)).status, 403);
  const used = await r.invitation(), device = await r.enroll(used);
  await (await r.object()).expire(used.invite_id);
  assert.equal((await r.request('/v1/enroll', { method: 'POST', value: device.payload })).status, 200);
  await r.admin('invites/' + used.invite_id + '/revoke', 'POST', {});
  assert.equal((await r.request('/v1/server/keys', { key: serverKey })).data.keys.length, 1);
  await r.admin('devices/' + device.id + '/revoke', 'POST', {});
  assert.equal((await r.request('/v1/connect', { method: 'POST', value: {}, ...r.auth(device) })).status, 403);
  assert.equal((await r.request('/v1/server/keys', { key: serverKey })).data.keys.length, 0);
  assert.equal((await r.request('/v1/server/keys', { key: serverKey })).data.revision, 2);
  await r.admin('devices/' + device.id + '/revoke', 'POST', {});
  assert.equal((await r.request('/v1/server/keys', { key: serverKey })).data.revision, 2);
});

test('authentication roles cannot be substituted and bad payloads do not consume invitations', async t => {
  const r = runtime(); t.after(() => r.mf.dispose());
  assert.equal((await r.request('/v1/admin/invites')).status, 401);
  assert.equal((await r.request('/v1/admin/invites', { key: serverKey })).status, 401);
  assert.equal((await r.request('/v1/server/keys', { key: adminKey })).status, 401);
  const invite = await r.invitation();
  for (const public_key of ['command="sh" ' + publicKey(), publicKey() + '\nssh-ed25519 bad', publicKey() + '\n', 'ssh-rsa AAAA', 'ssh-ed25519 AAAA']) {
    assert.equal((await r.enroll(invite, randomBytes(32).toString('hex'), public_key)).status, 400);
  }
  const device = await r.enroll(invite);
  assert.equal(device.status, 200);
  assert.equal((await r.request('/v1/connect', { method: 'POST', value: {}, key: hash(device.credential), device: device.id })).status, 401);
  const inventories = JSON.stringify([(await r.admin('invites')).data, (await r.admin('devices')).data]);
  assert(!inventories.includes(invite.token));
  assert(!inventories.includes(hash(device.credential)));
  assert(!inventories.includes(device.payload.public_key));
});

test('JSON identifiers reject coercion and trailing newlines without consuming the link', async t => {
  const r = runtime(); t.after(() => r.mf.dispose());
  const invite = await r.invitation();
  const payload = { invite_id: invite.invite_id, token: invite.token, public_key: publicKey(),
    credential_hash: hash(randomBytes(32).toString('hex')), device_name: 'Device' };
  for (const field of ['invite_id', 'token', 'credential_hash']) {
    for (const value of [[payload[field]], payload[field] + '\n', null, 123]) {
      const result = await r.request('/v1/enroll', { method: 'POST', value: { ...payload, [field]: value } });
      assert.equal(result.status, 400);
    }
  }
  assert.equal((await r.request('/v1/enroll', { method: 'POST', value: payload })).status, 200);
});

test('concurrent connect requests submit only one provider job', async t => {
  const r = runtime(); t.after(() => r.mf.dispose());
  const device = await r.enroll(await r.invitation());
  r.provider().intercept({ path: '/v2/test-endpoint/run', method: 'POST' })
    .reply(200, { id: 'only-job', status: 'IN_QUEUE' }).delay(200);
  const results = await Promise.all(Array.from({ length: 8 }, () =>
    r.request('/v1/connect', { method: 'POST', value: {}, ...r.auth(device) })));
  assert.equal(results.filter(x => x.status === 200).length, 1);
  assert.equal(results.filter(x => x.status === 409).length, 7);
  assert.deepEqual((await r.request('/v1/connect', { method: 'POST', value: {}, ...r.auth(device) })).data,
    { id: 'only-job', status: 'IN_QUEUE' });
  r.mock.assertNoPendingInterceptors();
});

test('job submission is scoped, deduplicated, owned, and filtered', async t => {
  const r = runtime(); t.after(() => r.mf.dispose());
  const first = await r.enroll(await r.invitation()), second = await r.enroll(await r.invitation());
  let submitted;
  r.provider().intercept({ path: '/v2/test-endpoint/run', method: 'POST',
    headers: { authorization: 'Bearer ' + starterKey } })
    .reply(options => {
      submitted = new Response(options.body).text();
      return { statusCode: 200, data: { id: 'job-1', status: 'COMPLETED', credentials: starterKey } };
    });
  const start = await r.request('/v1/connect', { method: 'POST', value: {}, ...r.auth(first) });
  assert.deepEqual(start.data, { id: 'job-1', status: 'IN_QUEUE' });
  assert.deepEqual(JSON.parse(await submitted), { input: { action: 'connect' } });
  const duplicate = await r.request('/v1/connect', { method: 'POST', value: {}, ...r.auth(first) });
  assert.deepEqual(duplicate.data, start.data);
  assert.equal((await r.request('/v1/jobs/job-1', r.auth(second))).status, 404);
  assert.equal((await r.request('/v1/jobs/job-1/cancel', { method: 'POST', value: {}, ...r.auth(second) })).status, 404);
  const output = { state: 'ready', host: '203.0.113.1', ssh_port: 12345, comfy_port: 8188, host_key: publicKey() };
  r.provider().intercept({ path: '/v2/test-endpoint/status/job-1', method: 'GET' })
    .reply(200, { id: 'job-1', status: 'COMPLETED', output: { ...output, control_key: starterKey }, secret: starterKey });
  const ready = await r.request('/v1/jobs/job-1', r.auth(first));
  assert.deepEqual(ready.data, { id: 'job-1', status: 'COMPLETED', output });
  assert(!JSON.stringify(ready.data).includes(starterKey));
  r.mock.assertNoPendingInterceptors();
});

test('revocation during a pending provider response blocks its result and future jobs', async t => {
  const r = runtime(); t.after(() => r.mf.dispose());
  const device = await r.enroll(await r.invitation());
  r.provider().intercept({ path: '/v2/test-endpoint/run', method: 'POST' })
    .reply(200, { id: 'slow-job', status: 'IN_QUEUE' }).delay(300);
  const pending = r.request('/v1/connect', { method: 'POST', value: {}, ...r.auth(device) });
  await new Promise(resolve => setTimeout(resolve, 100));
  assert.equal((await r.admin('devices/' + device.id + '/revoke', 'POST', {})).status, 200);
  assert.equal((await pending).status, 403);
  assert.equal((await r.request('/v1/jobs/slow-job', r.auth(device))).status, 403);
  assert.equal((await r.request('/v1/jobs/slow-job/cancel', { method: 'POST', value: {}, ...r.auth(device) })).status, 403);
});

test('provider errors and redirects never leak credentials or redirect the caller', async t => {
  const r = runtime(); t.after(() => r.mf.dispose());
  for (const code of [500, 302]) {
    const device = await r.enroll(await r.invitation());
    r.provider().intercept({ path: '/v2/test-endpoint/run', method: 'POST' })
      .reply(code, { error: starterKey }, { headers: { location: 'https://attacker.invalid/' + starterKey } });
    const result = await r.request('/v1/connect', { method: 'POST', value: {}, ...r.auth(device) });
    assert.equal(result.status, 502);
    assert(!JSON.stringify(result.data).includes(starterKey));
    assert.equal(result.headers.get('location'), null);
    assert.equal((await r.request('/v1/connect', { method: 'POST', value: {}, ...r.auth(device) })).status, 409);
  }
});

test('failed and cancelled jobs stay owner-bound and do not expose raw provider messages', async t => {
  const r = runtime(); t.after(() => r.mf.dispose());
  const device = await r.enroll(await r.invitation());
  r.provider().intercept({ path: '/v2/test-endpoint/run', method: 'POST' }).reply(200, { id: 'failed-job', status: 'IN_QUEUE' });
  await r.request('/v1/connect', { method: 'POST', value: {}, ...r.auth(device) });
  r.provider().intercept({ path: '/v2/test-endpoint/status/failed-job', method: 'GET' })
    .reply(200, { id: 'failed-job', status: 'COMPLETED', output: { state: 'unavailable', message: starterKey } });
  const result = await r.request('/v1/jobs/failed-job', r.auth(device));
  assert.equal(result.data.output.state, 'unavailable');
  assert(!JSON.stringify(result.data).includes(starterKey));
  r.provider().intercept({ path: '/v2/test-endpoint/cancel/failed-job', method: 'POST' })
    .reply(200, { id: 'failed-job', status: 'CANCELLED', error: starterKey });
  assert.deepEqual((await r.request('/v1/jobs/failed-job/cancel', { method: 'POST', value: {}, ...r.auth(device) })).data,
    { id: 'failed-job', status: 'CANCELLED' });
});

test('requests are bounded and browser origins, extra fields and unrecognized routes are rejected', async t => {
  const r = runtime(); t.after(() => r.mf.dispose());
  assert.equal((await r.request('/v1/admin/invites', { key: adminKey, headers: { Origin: 'https://attacker.invalid' } })).status, 403);
  assert.equal((await r.request('/v1/admin/invites?token=secret', { key: adminKey })).status, 403);
  assert.equal((await r.request('/v1/admin/invites', { method: 'POST', value: { label: 'x'.repeat(9000) }, key: adminKey })).status, 413);
  assert.equal((await r.admin('invites', 'POST', { label: 'bad\nname' })).status, 400);
  assert.equal((await r.admin('invites', 'POST', { expires_in_seconds: 0 })).status, 400);
  assert.equal((await r.admin('invites', 'POST', { expires_in_seconds: 2592001 })).status, 400);
  const device = await r.enroll(await r.invitation());
  assert.equal((await r.request('/v1/connect', { method: 'POST', value: { pod_id: 'another' }, ...r.auth(device) })).status, 400);
  assert.equal((await r.request('/v1/jobs/another/stop', r.auth(device))).status, 404);
  const result = await r.request('/v1/server/keys', { key: serverKey });
  assert.equal(result.headers.get('cache-control'), 'no-store');
  assert.equal(result.headers.get('access-control-allow-origin'), null);
});

test('enrollment rate limits persist in the Durable Object', async t => {
  const r = runtime(); t.after(() => r.mf.dispose());
  for (let i = 0; i < 40; ++i) {
    const result = await r.request('/v1/enroll', { method: 'POST', value: {}, ip: '192.0.2.254' });
    assert.equal(result.status, 400);
  }
  assert.equal((await r.request('/v1/enroll', { method: 'POST', value: {}, ip: '192.0.2.254' })).status, 429);
});

test('claimed invitations and revoked devices survive runtime restart', async t => {
  const artifacts = resolve(root, '../../.local-work');
  await mkdir(artifacts, { recursive: true });
  const directory = await mkdtemp(resolve(artifacts, 'gateway-test-'));
  assert(directory.startsWith(artifacts + sep));
  let r = runtime(directory);
  t.after(async () => { await r.mf.dispose(); await rm(directory, { recursive: true, force: true }); });
  const invite = await r.invitation(), device = await r.enroll(invite);
  await r.admin('devices/' + device.id + '/revoke', 'POST', {});
  await r.mf.dispose();
  r = runtime(directory);
  assert.equal((await r.enroll(invite)).status, 409);
  assert.equal((await r.request('/v1/enroll', { method: 'POST', value: device.payload })).status, 403);
  assert.equal((await r.request('/v1/connect', { method: 'POST', value: {}, ...r.auth(device) })).status, 403);
  assert.equal((await r.admin('devices')).data.devices.length, 1);
  assert.equal((await r.request('/v1/server/keys', { key: serverKey })).data.revision, 2);
});
