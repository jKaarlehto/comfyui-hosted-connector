"use strict";

import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const source = fs.readFileSync(new URL("./site/connect.js", import.meta.url), "utf8");
const functions = { atob, Uint8Array, TextDecoder };
vm.runInNewContext(source, functions);
const { invitationToken } = functions;
const invitation = {
  version: 1, endpoint: "endpoint123", key: "x".repeat(40),
  ssh_key: "-----BEGIN OPENSSH PRIVATE KEY-----\nfixture\n-----END OPENSSH PRIVATE KEY-----"
};
const encoded = Buffer.from(JSON.stringify(invitation)).toString("base64");
const canonical = Buffer.from(JSON.stringify(invitation)).toString("base64url");
assert.equal(invitationToken("#" + encoded), canonical);
assert.equal(invitationToken("#" + canonical), canonical);
for (const value of ["", "#", "#javascript:alert(1)", "#" + "a".repeat(16385), "#e30", "#////", "#<script>alert(1)</script>", "#%61%61", "#AA=AA", "#bnVsbA"]) {
  assert.throws(() => invitationToken(value));
}
for (const invalid of [
  { version: 2 }, { version: "1" }, { endpoint: "example.com/path" }, { endpoint: 12345678 },
  { key: "bad" }, { ssh_key: 1 }, { ssh_key: "-----BEGIN OPENSSH PRIVATE KEY-----<script>" },
  { endpoint: "<img src=x onerror=alert(1)>" }, { url: "https://attacker.test/" }, { key: "x\n".repeat(30) }
]) {
  const value = Buffer.from(JSON.stringify({ ...invitation, ...invalid })).toString("base64url");
  assert.throws(() => invitationToken(value));
}

let requestNumber = 0;
function page(fragment, storage = new Map(), script = source) {
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      disabled: true, textContent: "", events: {}, open: false,
      addEventListener(type, action) { this.events[type] = action; },
      focus() { }, removeAttribute() { }, setAttribute() { }
    });
    return elements.get(id);
  }
  const requests = [];
  const events = {};
  const channels = [];
  const timers = [];
  const writes = [];
  const navigations = [];
  const location = { hash: fragment };
  Object.defineProperty(location, "href", {
    get() { return navigations.at(-1) || "unchanged"; },
    set(value) { navigations.push(value); }
  });
  const sandbox = {
    document: { getElementById: element },
    window: { location, addEventListener(type, action) { events[type] = action; } },
    fetch(url) { requests.push(url); return Promise.resolve({ ok: false }); },
    crypto: { randomUUID() { return "00000000-0000-0000-0000-" + String(++requestNumber).padStart(12, "0"); } },
    localStorage: {
      getItem(key) { return storage.get(key) || null; },
      setItem(key, value) { writes.push([key, value]); storage.set(key, value); },
      removeItem(key) { storage.delete(key); }
    },
    BroadcastChannel: class {
      constructor(name) { this.name = name; this.messages = []; channels.push(this); }
      addEventListener(type, action) { this.receive = action; }
      postMessage(message) { this.messages.push(message); }
      close() { }
    },
    setTimeout(action) { timers.push(action); },
    atob, Uint8Array, TextDecoder
  };
  vm.runInNewContext(script, sandbox);
  return { elements, sandbox, requests, events, channels, timers, writes, navigations, storage };
}

for (const fragment of ["#<script>bad</script>", "#////", "#e30", ""]) {
  const test = page(fragment);
  assert.equal(test.elements.get("connect").disabled, true);
  test.elements.get("connect").events.click();
  assert.equal(test.sandbox.window.location.href, "unchanged");
  assert.deepEqual(test.requests, ["downloads/release.json"]);
}
const valid = page("#" + canonical);
assert.equal(valid.sandbox.window.location.href, "unchanged");
assert.equal(valid.elements.get("connect").disabled, false);
valid.elements.get("connect").events.click();
assert.equal(valid.sandbox.window.location.href, "hosted-comfyui://connect#" + canonical);
assert.deepEqual(valid.requests, ["downloads/release.json"]);
valid.elements.get("installed").events.click();
assert.equal(valid.navigations.length, 2);
assert.doesNotMatch(valid.elements.get("status").textContent, /connected|server ready/i);

const installing = page("#" + canonical);
installing.elements.get("download").events.click();
const request = JSON.parse(installing.storage.get("hosted-comfyui-install-request"));
assert.equal(installing.navigations.length, 0);
assert.ok(request.id);
assert.ok(request.time);
const signal = { type: "installed", requestId: request.id };
installing.channels[0].receive({ data: { ...signal, requestId: "wrong" } });
assert.equal(installing.navigations.length, 0);
installing.channels[0].receive({ data: signal });
assert.equal(installing.navigations.length, 1);
installing.events.storage({ key: "hosted-comfyui-install-event", newValue: JSON.stringify(signal) });
installing.channels[0].receive({ data: signal });
assert.equal(installing.navigations.length, 1, "Duplicate notifications must not relaunch the protocol");
assert.match(installing.elements.get("status").textContent, /click Connect/i);
assert.equal(installing.channels[0].messages[0].type, "continuing");
assert.equal(installing.storage.size, 0);
for (const [, value] of installing.writes) {
  assert.ok(!value.includes(canonical));
  assert.ok(!value.includes(invitation.key));
  assert.ok(!value.includes("ssh_key"));
}

const shared = new Map();
const earlier = page("#" + canonical, shared);
const latest = page("#" + canonical, shared);
earlier.elements.get("download").events.click();
const earlierId = JSON.parse(shared.get("hosted-comfyui-install-request")).id;
latest.elements.get("download").events.click();
const latestId = JSON.parse(shared.get("hosted-comfyui-install-request")).id;
earlier.channels[0].receive({ data: { type: "installed", requestId: earlierId } });
assert.equal(earlier.navigations.length, 0, "A newer install request must supersede another tab");
for (const tab of [earlier, latest]) tab.channels[0].receive({ data: { type: "installed", requestId: latestId } });
assert.equal(earlier.navigations.length, 0);
assert.equal(latest.navigations.length, 1);

const manual = page("#" + canonical);
manual.elements.get("download").events.click();
const manualId = JSON.parse(manual.storage.get("hosted-comfyui-install-request")).id;
manual.elements.get("connect").events.click();
manual.channels[0].receive({ data: { type: "installed", requestId: manualId } });
assert.equal(manual.navigations.length, 1, "Manual connection cancels the outstanding automatic attempt");

const completionSource = fs.readFileSync(new URL("./site/installed.js", import.meta.url), "utf8");
const completeStorage = new Map([["hosted-comfyui-install-request", JSON.stringify(request)]]);
const complete = page("", completeStorage, completionSource);
assert.equal(complete.channels[0].messages[0].requestId, request.id);
assert.equal(complete.requests.length, 0);
assert.equal(complete.navigations.length, 0);
complete.channels[0].receive({ data: { type: "continuing", requestId: request.id } });
complete.timers[0]();
assert.match(complete.elements.get("handoff-status").textContent, /requested the connector/);
assert.doesNotMatch(complete.elements.get("handoff-status").textContent, /connected|server ready/i);
const missing = page("", new Map(), completionSource);
assert.match(missing.elements.get("handoff-status").textContent, /invitation tab and click Connect/);
assert.equal(missing.navigations.length, 0);
console.log("Invitation validation, navigation and automatic handoff tests passed.");
