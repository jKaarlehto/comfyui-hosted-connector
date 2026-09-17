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
const confirmationKey = "hosted-comfyui-install-confirmed";
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
  assert.equal(test.elements.get("download").hidden, false);
  test.elements.get("connect").events.click();
  test.elements.get("download").events.click();
  assert.equal(test.sandbox.window.location.href, "unchanged");
  assert.equal(test.storage.has("hosted-comfyui-install-request"), false);
  assert.deepEqual(test.requests, ["downloads/release.json"]);
}
const valid = page("#" + canonical);
assert.equal(valid.sandbox.window.location.href, "unchanged");
assert.equal(valid.elements.get("connect").disabled, false);
assert.equal(valid.elements.get("download").hidden, false);
assert.equal(valid.elements.get("connect").className, "button secondary");
assert.equal(valid.elements.get("connect").textContent, "Already installed? Connect");
valid.elements.get("connect").events.click();
assert.equal(valid.sandbox.window.location.href, "hosted-comfyui://connect#" + canonical);
assert.deepEqual(valid.requests, ["downloads/release.json"]);
assert.equal(valid.storage.has(confirmationKey), false, "A launch attempt cannot prove installation");
valid.elements.get("installed").events.click();
assert.equal(valid.navigations.length, 2);
assert.equal(valid.storage.get(confirmationKey), "1");
assert.equal(valid.elements.get("download").hidden, true);
assert.doesNotMatch(valid.elements.get("status").textContent, /connected|server ready/i);

const returning = page("#" + canonical, valid.storage);
assert.equal(returning.elements.get("download").hidden, true);
assert.equal(returning.elements.get("connect").className, "button primary");
assert.equal(returning.elements.get("connect").textContent, "Connect");
assert.equal(returning.elements.get("install-help").textContent, "Help or reinstall");
assert.match(returning.elements.get("install-status").textContent, /confirmed in this browser/);
assert.equal(returning.navigations.length, 0);
assert.equal(page("#" + canonical, new Map([[confirmationKey, "unexpected"]])).elements.get("download").hidden, false);

const notified = page("#" + canonical);
notified.events.storage({ key: confirmationKey, newValue: "1" });
assert.equal(notified.elements.get("download").hidden, true);
assert.equal(notified.navigations.length, 0);
notified.channels[0].receive({ data: { type: "installation-confirmed" } });
assert.equal(notified.navigations.length, 0, "Confirmation alone must never launch a tab's invitation");
const invalidConfirmed = page("#invalid", new Map([[confirmationKey, "1"]]));
assert.equal(invalidConfirmed.elements.get("connect").disabled, true);
invalidConfirmed.elements.get("installed").events.click();
assert.equal(invalidConfirmed.navigations.length, 0);

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
assert.equal(installing.storage.size, 1);
assert.equal(installing.storage.get(confirmationKey), "1");
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
assert.equal(complete.channels[0].messages[0].type, "installation-confirmed");
assert.equal(complete.channels[0].messages[1].requestId, request.id);
assert.equal(complete.requests.length, 0);
assert.equal(complete.navigations.length, 0);
complete.channels[0].receive({ data: { type: "continuing", requestId: request.id } });
complete.timers[0]();
assert.match(complete.elements.get("handoff-status").textContent, /requested the connector/);
assert.doesNotMatch(complete.elements.get("handoff-status").textContent, /connected|server ready/i);
const missing = page("", new Map(), completionSource);
assert.match(missing.elements.get("handoff-status").textContent, /invitation tab and click Connect/);
assert.equal(missing.navigations.length, 0);
assert.equal(missing.storage.get(confirmationKey), "1", "Callback must remember installation even with no invitation tab");
const reopened = page("#" + canonical, missing.storage);
assert.equal(reopened.elements.get("download").hidden, true);
assert.equal(reopened.navigations.length, 0);

class UnavailableStorage extends Map {
  get() { throw new Error("Storage blocked"); }
  set() { throw new Error("Storage blocked"); }
  delete() { throw new Error("Storage blocked"); }
}
const privateBrowser = page("#" + canonical, new UnavailableStorage());
assert.equal(privateBrowser.elements.get("download").hidden, false);
privateBrowser.elements.get("download").events.click();
assert.equal(privateBrowser.navigations.length, 0);
privateBrowser.elements.get("installed").events.click();
assert.equal(privateBrowser.elements.get("download").hidden, true);
assert.match(privateBrowser.elements.get("install-status").textContent, /for this visit/);
assert.equal(privateBrowser.navigations.length, 1);
const blockedCallback = page("", new UnavailableStorage(), completionSource);
assert.equal(blockedCallback.channels[0].messages[0].type, "installation-confirmed");
assert.match(blockedCallback.elements.get("handoff-status").textContent, /invitation tab/);
console.log("Invitation validation, remembered installation and automatic handoff tests passed.");
