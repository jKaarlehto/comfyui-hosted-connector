"use strict";

import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const source = fs.readFileSync(new URL("./site/connect.js", import.meta.url), "utf8");
const html = fs.readFileSync(new URL("./site/index.html", import.meta.url), "utf8");
const functions = { atob, Uint8Array, TextDecoder, URL };
vm.runInNewContext(source, functions);
const { invitationToken, localAddress, sessionStatus, modelStatus } = functions;
const invitation = {
  version: 1, endpoint: "endpoint123", key: "x".repeat(40),
  ssh_key: "-----BEGIN OPENSSH PRIVATE KEY-----\nfixture\n-----END OPENSSH PRIVATE KEY-----"
};
const canonical = Buffer.from(JSON.stringify(invitation)).toString("base64url");
assert.equal(invitationToken("#" + Buffer.from(JSON.stringify(invitation)).toString("base64")), canonical);
assert.equal(invitationToken("#" + canonical), canonical);
for (const value of ["", "#", "#javascript:alert(1)", "#" + "a".repeat(16385), "#e30", "#////", "#<script>alert(1)</script>", "#%61%61", "#AA=AA", "#bnVsbA"]) {
  assert.throws(() => invitationToken(value));
}
for (const invalid of [
  { version: 2 }, { version: "1" }, { endpoint: "example.com/path" }, { endpoint: 12345678 },
  { key: "bad" }, { ssh_key: 1 }, { ssh_key: "-----BEGIN OPENSSH PRIVATE KEY-----<script>" },
  { endpoint: "<img src=x onerror=alert(1)>" }, { url: "https://attacker.test/" }, { key: "x\n".repeat(30) }
]) {
  assert.throws(() => invitationToken("#" + Buffer.from(JSON.stringify({ ...invitation, ...invalid })).toString("base64url")));
}

function page(fragment = "#" + canonical, markup = html, query = "") {
  const elements = new Map();
  for (const match of markup.matchAll(/<[^>]*\bid="([^"]+)"[^>]*>/g)) {
    const tag = match[0];
    elements.set(match[1], {
      hidden: /\bhidden\b/.test(tag), disabled: /\bdisabled\b/.test(tag), textContent: "", events: {},
      addEventListener(type, action) { this.events[type] = action; },
      removeAttribute(name) { this[name] = undefined; }, setAttribute(name, value) { this[name] = value; }
    });
  }
  const requests = [];
  const events = {};
  const documentEvents = {};
  const timers = new Map();
  const navigations = [];
  const redirects = [];
  let timerId = 0;
  let nonceId = 0;
  let href = "https://example.test/connect/" + query + fragment;
  const location = { hash: fragment, replace(value) { redirects.push(value); } };
  Object.defineProperty(location, "href", { get() { return href; }, set(value) { href = value; navigations.push(value); } });
  const result = {
    elements, requests, events, documentEvents, timers, navigations, redirects,
    now: 100000, clipboard: [],
    response: async () => { throw new Error("No connector"); },
    release: { ok: true, json: async () => ({ installerAvailable: true }) }
  };
  const document = {
    documentElement: { dataset: { connectorPage: /data-connector-page="([^"]+)"/.exec(markup)?.[1] } },
    hidden: false, getElementById(id) { return elements.get(id) || null; },
    addEventListener(type, action) { documentEvents[type] = action; }
  };
  const sandbox = {
    document, window: { location, addEventListener(type, action) { events[type] = action; } },
    fetch(url, options) {
      requests.push({ url, options });
      return url === "downloads/release.json" ? Promise.resolve(result.release) : result.response(url, options);
    },
    crypto: { getRandomValues(bytes) { bytes.fill(++nonceId); return bytes; } },
    setTimeout(action, delay) { const id = ++timerId; timers.set(id, { action, delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
    Date: { now: () => result.now },
    navigator: { clipboard: { async writeText(value) { result.clipboard.push(value); } } },
    AbortController, URL, atob, Uint8Array, TextDecoder
  };
  Object.defineProperty(sandbox, "localStorage", { get() { throw new Error("Installation must not use browser storage"); } });
  vm.runInNewContext(source, sandbox);
  return Object.assign(result, { document });
}
const flush = () => new Promise(resolve => setImmediate(resolve));
async function poll(test) {
  const timer = [...test.timers].find(([, value]) => value.delay === 2000);
  assert.ok(timer, "Visible page schedules another live check");
  test.timers.delete(timer[0]);
  timer[1].action();
  await flush();
}
function answer(url, values = {}) {
  return { ok: true, json: async () => ({ app: "hosted-comfyui-connector", protocol: 1, version: "1.0.4", live_status: 1,
    nonce: new URL(url).searchParams.get("nonce"), session: new URL(url).searchParams.get("session"), ...values }) };
}
function state(test, ready) {
  assert.equal(test.elements.get("download").hidden, ready);
  assert.equal(test.elements.get("connect").hidden, !ready);
  assert.doesNotMatch(test.elements.get("status").textContent, /Checking invitation/);
}
assert.doesNotMatch(html, /I installed it|Already installed|First time|id="installed"|id="install-help"/);
assert.match(html, /connect-src 'self' http:\/\/127\.0\.0\.1:18187/);
const test = page();
await flush();
state(test, false);
test.elements.get("download").events.click();
assert.equal(test.elements.get("install-guide").hidden, false);
test.response = async url => answer(url);
await poll(test);
state(test, true);
assert.equal(test.elements.get("install-guide").hidden, true);
assert.equal(test.navigations.length, 0, "Detection must not launch or start a Pod");
test.response = async () => { throw new Error("Uninstalled"); };
await poll(test);
state(test, true);
await poll(test);
await poll(test);
await poll(test);
await poll(test);
state(test, false);
for (const request of test.requests.filter(request => request.url.startsWith("http:"))) {
  const uri = new URL(request.url);
  assert.equal(uri.origin, "http://127.0.0.1:18187");
  assert.equal(uri.pathname, "/status");
  assert.deepEqual([...uri.searchParams.keys()], ["nonce"]);
  assert.match(uri.searchParams.get("nonce"), /^[0-9a-f]{32}$/);
  assert.equal(request.options.credentials, "omit");
  assert.equal(request.options.cache, "no-store");
  assert.equal(request.options.redirect, "error");
  assert.equal(request.options.referrerPolicy, "no-referrer");
  assert.equal(request.options.targetAddressSpace, "loopback");
  assert.ok(!JSON.stringify(request).includes(invitation.key));
  assert.ok(!JSON.stringify(request).includes(canonical));
}
for (const values of [{ app: "unrelated" }, { protocol: 2 }, { nonce: "stale" }, { protocol: "1" }]) {
  test.response = async url => answer(url, values);
  await poll(test);
  state(test, false);
}
test.response = async () => ({ ok: true, json: async () => { throw new Error("Invalid JSON"); } });
await poll(test);
state(test, false);
for (const fragment of ["", "#invalid", "#e30"]) {
  const invalid = page(fragment);
  await flush();
  invalid.response = async url => answer(url);
  await poll(invalid);
  assert.equal(invalid.elements.get("connect").disabled, true);
  invalid.elements.get("connect").events.click();
  assert.equal(invalid.navigations.length, 0);
  assert.match(invalid.elements.get("status").textContent, /complete invitation/);
}

test.response = (url, options) => new Promise((resolve, reject) => {
  options.signal.addEventListener("abort", () => reject(new Error("Timed out")));
});
const next = [...test.timers].find(([, value]) => value.delay === 2000);
test.timers.delete(next[0]);
next[1].action();
const requestCount = test.requests.length;
test.events.focus();
test.documentEvents.visibilitychange();
assert.equal(test.requests.length, requestCount, "Checks must not overlap");
const deadline = [...test.timers].find(([, value]) => value.delay === 4500);
assert.ok(deadline);
test.timers.delete(deadline[0]);
deadline[1].action();
await flush();
state(test, false);

test.document.hidden = true;
await poll(test);
assert.equal(test.timers.size, 0, "Hidden page stops polling");
test.response = async url => answer(url);
test.document.hidden = false;
test.documentEvents.visibilitychange();
await flush();
state(test, true);
test.events.pagehide();
assert.equal(test.timers.size, 0);
test.events.focus();
assert.equal(test.timers.size, 0);
test.events.pageshow();
await flush();
state(test, true);

const oldMarkup = '<html><button id="connect" disabled></button><p id="status">Checking invitation...</p><details id="install-guide"><a id="download"></a></details></html>';
const cached = page("#" + canonical, oldMarkup);
assert.equal(cached.requests.length, 0);
assert.equal(cached.redirects.length, 1);
const refresh = new URL(cached.redirects[0]);
assert.equal(refresh.origin, "https://example.test");
assert.equal(refresh.search, "?page=3");
assert.equal(refresh.hash, "#" + canonical);
const stillCached = page("#" + canonical, oldMarkup, "?page=3");
assert.equal(stillCached.redirects.length, 0, "Never create a reload loop");
assert.match(stillCached.elements.get("status").textContent, /out of date/);

const oldConnector = page();
await flush();
oldConnector.response = async url => answer(url, { live_status: undefined, version: "1.0.3" });
await poll(oldConnector);
state(oldConnector, false);
assert.equal(oldConnector.elements.get("download").textContent, "Update connector");
assert.match(oldConnector.elements.get("status").textContent, /Update the connector/);
assert.equal(oldConnector.navigations.length, 0);

const active = page();
await flush();
let remote = null;
active.response = async url => new URL(url).pathname === "/status" ? answer(url)
  : remote ? answer(url, remote) : { ok: false };
await poll(active);
active.elements.get("connect").events.click();
const firstLaunch = new URL(active.navigations[0]);
assert.equal(firstLaunch.protocol, "hosted-comfyui:");
assert.equal(firstLaunch.host, "connect");
assert.match(firstLaunch.searchParams.get("session"), /^[0-9a-f]{32}$/);
assert.equal(firstLaunch.hash, "#" + canonical);
assert.equal(active.elements.get("connect").hidden, true);
assert.match(active.elements.get("status").textContent, /Opening the connector/);
await poll(active);
assert.equal(active.elements.get("connect").hidden, true);
active.now += 21000;
await poll(active);
assert.equal(active.elements.get("connect").hidden, false);
assert.match(active.elements.get("status").textContent, /hasn't opened/);
remote = { state: "starting", message: "Installing ComfyUI dependencies", address: "" };
await poll(active);
assert.equal(active.elements.get("connect").hidden, true);
assert.equal(active.elements.get("status").textContent, remote.message);
assert.equal(active.elements.get("open").hidden, true);
remote = { state: "connected", message: "Connected", address: "http://127.0.0.1:18188" };
await poll(active);
assert.equal(active.elements.get("open").hidden, false);
assert.equal(active.elements.get("open").href, remote.address);
assert.equal(active.elements.get("address").textContent, remote.address);
assert.equal(active.elements.get("connect").hidden, true);
await active.elements.get("copy").events.click();
assert.deepEqual(active.clipboard, [remote.address]);
assert.equal(active.elements.get("copy").textContent, "Copied");
remote.model = { phase: "downloading", filename: "<img src=x onerror=alert(1)>.safetensors", completed_bytes: 1024 ** 3, total_bytes: 2 * 1024 ** 3 };
await poll(active);
assert.equal(active.elements.get("model").hidden, false);
assert.equal(active.elements.get("model-progress").value, 50);
assert.match(active.elements.get("model-status").textContent, /50% \(1.00 GB \/ 2.00 GB\)/);
assert.equal(active.elements.get("model-file").textContent, remote.model.filename);
assert.equal(active.elements.get("open").hidden, false);
remote.model.phase = "verifying";
await poll(active);
assert.match(active.elements.get("model-status").textContent, /Verifying model/);
remote.model.phase = "error";
remote.model.error = "secret must not be displayed";
await poll(active);
assert.match(active.elements.get("model-status").textContent, /failed/);
assert.doesNotMatch(active.elements.get("model-status").textContent, /secret/);
assert.equal(active.elements.get("model-progress").hidden, true);
remote.model = { phase: "idle" };
await poll(active);
assert.equal(active.elements.get("model").hidden, true);

const connectedStatus = remote;
remote = null;
active.now += 9000;
await poll(active);
assert.equal(active.elements.get("open").hidden, false, "A transient missed status preserves the connection display");
active.now += 1000;
await poll(active);
assert.equal(active.elements.get("open").hidden, true);
assert.equal(active.elements.get("connect").hidden, false);
assert.match(active.elements.get("status").textContent, /unavailable/);
remote = connectedStatus;
await poll(active);
assert.equal(active.elements.get("open").hidden, false);
remote = { state: "error", message: "The server could not start. Try reconnecting.", address: "" };
await poll(active);
assert.equal(active.elements.get("status").textContent, remote.message);
assert.equal(active.elements.get("connect").hidden, false);
assert.equal(active.elements.get("endpoint").hidden, true);
remote = { state: "disconnected", message: "Connection closed.", address: "" };
await poll(active);
assert.equal(active.elements.get("status").textContent, "Connection closed.");
assert.equal(active.elements.get("connect").textContent, "Reconnect");
let finishOldStatus;
active.response = async url => new URL(url).pathname === "/status" ? answer(url)
  : new Promise(resolve => { finishOldStatus = () => resolve(answer(url, connectedStatus)); });
const oldPoll = [...active.timers].find(([, value]) => value.delay === 2000);
active.timers.delete(oldPoll[0]);
oldPoll[1].action();
await flush();
active.elements.get("connect").events.click();
assert.notEqual(new URL(active.navigations[1]).searchParams.get("session"), firstLaunch.searchParams.get("session"));
assert.match(active.elements.get("status").textContent, /Opening/);
finishOldStatus();
await flush();
assert.equal(active.elements.get("open").hidden, true, "Old poll response cannot attach to a new session");
assert.match(active.elements.get("status").textContent, /Opening/);
active.response = async url => answer(url, connectedStatus);
await poll(active);
assert.equal(active.elements.get("open").hidden, false);
active.response = async () => { throw new Error("Helper stopped"); };
active.now += 10000;
await poll(active);
await poll(active);
await poll(active);
assert.equal(active.elements.get("connect").hidden, true);
assert.equal(active.elements.get("download").hidden, false);
assert.match(active.elements.get("status").textContent, /not responding/);
assert.doesNotMatch(active.elements.get("status").textContent, /Click Reconnect/);
for (const request of active.requests.filter(request => request.url.startsWith("http:"))) {
  const url = new URL(request.url);
  assert.equal(url.origin, "http://127.0.0.1:18187");
  assert.ok(["/status", "/session"].includes(url.pathname));
  assert.ok(!JSON.stringify(request).includes(canonical));
  assert.ok(!JSON.stringify(request).includes(invitation.key));
  assert.equal(request.options.credentials, "omit");
  assert.equal(request.options.referrerPolicy, "no-referrer");
}
for (const unsafe of ["https://127.0.0.1:18188", "http://evil.test:18188", "http://127.0.0.1:18188@evil.test", "http://127.0.0.1:18188/evil", "http://127.0.0.1:18188?x", "http://127.0.0.1:18188#x", "javascript:alert(1)", "http://127.0.0.1:22", "http://127.0.0.1:99999"]) {
  assert.equal(localAddress(unsafe), "");
}
assert.equal(localAddress("http://127.0.0.1:18188/"), "http://127.0.0.1:18188");
const snapshot = { app: "hosted-comfyui-connector", protocol: 1, session: "session", nonce: "nonce", state: "connected", message: "Connected", address: "http://127.0.0.1:18188" };
for (const invalid of [{ app: "other" }, { session: "other" }, { nonce: "other" }, { state: "other" }, { address: "https://evil.test" }, { message: 1 }, { message: "a".repeat(501) }]) {
  assert.equal(sessionStatus({ ...snapshot, ...invalid }, "session", "nonce"), null);
}
for (const invalid of [null, {}, { phase: "other" }, { phase: "downloading", filename: "model", completed_bytes: -1, total_bytes: 100 }, { phase: "downloading", filename: "model", completed_bytes: 1, total_bytes: Infinity }]) {
  assert.equal(modelStatus(invalid), null);
}
assert.equal(modelStatus({ phase: "downloading", filename: "a\u202eb", completed_bytes: 0, total_bytes: 0 }).filename, "ab");
assert.equal(modelStatus({ phase: "downloading", filename: "m".repeat(1024), completed_bytes: 0, total_bytes: 0 }).filename.length, 1024);
assert.equal(modelStatus({ phase: "downloading", filename: "m".repeat(1025), completed_bytes: 0, total_bytes: 0 }), null);
console.log("Invitation parsing, live detection/status, progress, reconnect, security, timeout and cached-page tests passed.");
