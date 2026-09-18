"use strict";

function invitationToken(fragment) {
  const token = fragment.replace(/^#/, "");
  if (!token || token.length > 16384 || !/^[A-Za-z0-9_+/=-]+$/.test(token)) {
    throw new Error("Open the complete invitation link sent by your host.");
  }
  const normalized = token.replace(/-/g, "+").replace(/_/g, "/").replace(/=+$/, "");
  const bytes = Uint8Array.from(atob(normalized), (character) => character.charCodeAt(0));
  const invitation = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
  if (!invitation || Object.keys(invitation).sort().join(",") !== "endpoint,key,ssh_key,version" ||
      invitation.version !== 1 || typeof invitation.endpoint !== "string" ||
      !/^[a-z0-9]{8,40}$/.test(invitation.endpoint) || typeof invitation.key !== "string" ||
      !/^[A-Za-z0-9_-]{20,200}$/.test(invitation.key) || typeof invitation.ssh_key !== "string" ||
      !/^-----BEGIN OPENSSH PRIVATE KEY-----\r?\n[A-Za-z0-9+/=\r\n]+-----END OPENSSH PRIVATE KEY-----\r?\n?$/.test(invitation.ssh_key)) {
    throw new Error("This invitation is invalid. Ask your host for a new link.");
  }
  return normalized.replace(/\+/g, "-").replace(/\//g, "_");
}

function randomToken() {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, "0")).join("");
}

function localAddress(value) {
  if (typeof value !== "string" || value !== value.trim() || !/^http:\/\/127\.0\.0\.1:[1-9][0-9]{0,4}\/?$/.test(value)) return "";
  try {
    const address = new URL(value);
    return Number(address.port) >= 1024 && Number(address.port) <= 65535 ? address.origin : "";
  } catch { return ""; }
}

function sessionStatus(value, session, nonce) {
  if (!value || value.app !== "hosted-comfyui-connector" || value.protocol !== 1 ||
      value.nonce !== nonce || value.session !== session ||
      !["starting", "connected", "error", "disconnected"].includes(value.state) ||
      typeof value.message !== "string" || value.message.length > 500) return null;
  const address = localAddress(value.address);
  if (value.state === "connected" && !address) return null;
  return { state: value.state, message: value.message, address, model: value.model };
}

function modelStatus(value) {
  if (!value || !["downloading", "verifying", "error"].includes(value.phase) ||
      typeof value.filename !== "string" || value.filename.length > 1024 ||
      !Number.isSafeInteger(value.completed_bytes) || value.completed_bytes < 0 ||
      !Number.isSafeInteger(value.total_bytes) || value.total_bytes < 0) return null;
  const percent = value.total_bytes > 0 ? Math.min(100, Math.floor(value.completed_bytes / value.total_bytes * 100)) : null;
  const size = bytes => (bytes / (1024 ** 3)).toFixed(2) + " GB";
  const message = value.phase === "error" ? "Model download failed. Retry generation in ComfyUI."
    : value.phase === "verifying" ? "Verifying model"
    : "Downloading model";
  return {
    filename: value.filename.replace(/[\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/g, ""),
    message: message + (value.phase !== "error" && percent !== null
      ? " · " + percent + "% (" + size(value.completed_bytes) + " / " + size(value.total_bytes) + ")" : ""),
    percent: value.phase === "error" ? null : percent, failed: value.phase === "error"
  };
}

function initializePage() {
  if (document.documentElement.dataset.connectorPage !== "3") {
    const address = new URL(window.location.href);
    if (address.searchParams.get("page") !== "3") {
      address.searchParams.set("page", "3");
      window.location.replace(address.href);
    } else {
      const status = document.getElementById("status");
      if (status) status.textContent = "This page is out of date. Reload it to continue.";
    }
    return;
  }
  const connect = document.getElementById("connect");
  const download = document.getElementById("download");
  const status = document.getElementById("status");
  const guide = document.getElementById("install-guide");
  const open = document.getElementById("open");
  const endpoint = document.getElementById("endpoint");
  const address = document.getElementById("address");
  const copy = document.getElementById("copy");
  const model = document.getElementById("model");
  const modelLabel = document.getElementById("model-status");
  const modelFile = document.getElementById("model-file");
  const progress = document.getElementById("model-progress");
  let token;
  try {
    token = invitationToken(window.location.hash);
  } catch { }
  let ready = false;
  let outdated = false;
  let installing = false;
  let session = null;
  let launchedAt = 0;
  let lastSeen = 0;
  let snapshot = null;
  let localError = "";
  let missedPresence = 0;
  let installerAvailable = true;
  let timer;
  let pending;
  let stopped = false;
  const render = () => {
    const waiting = session && !snapshot && Date.now() - launchedAt < 20000 && !localError;
    const connected = ready && snapshot?.state === "connected" && !localError;
    const busy = waiting || (ready && snapshot?.state === "starting" && !localError);
    download.hidden = ready;
    download.textContent = outdated ? "Update connector" : "Download and install";
    connect.hidden = !ready || connected || busy;
    connect.disabled = !token || !ready || busy;
    connect.textContent = session ? "Reconnect" : "Connect";
    open.hidden = !connected;
    endpoint.hidden = !connected;
    if (connected) {
      open.href = snapshot.address;
      if (address.textContent !== snapshot.address) copy.textContent = "Copy address";
      address.textContent = snapshot.address;
    } else {
      open.removeAttribute("href");
      address.textContent = "";
    }
    const transfer = connected && modelStatus(snapshot.model);
    model.hidden = !transfer;
    if (transfer) {
      if (modelLabel.textContent !== transfer.message) modelLabel.textContent = transfer.message;
      modelFile.textContent = transfer.filename;
      progress.hidden = transfer.failed;
      if (transfer.percent === null) progress.removeAttribute("value");
      else progress.value = transfer.percent;
    }
    guide.hidden = ready || !installing;
    const message = !token
      ? "Open the complete invitation link sent by your host."
      : outdated ? "Update the connector to show connection progress here. Your invitation will still work."
      : !ready && session ? "The local connector is not responding. Open it again or reinstall it."
      : localError || (ready && session
        ? snapshot
          ? (snapshot.state === "connected" ? "Connected. Keep the connector open while working." : snapshot.message)
          : waiting ? "Opening the connector. Allow your browser's prompt."
            : "The connector hasn't opened. Click Reconnect and allow your browser's prompt."
        : ready ? "Connector detected. You're ready to connect."
        : installerAvailable
          ? "Install the connector to continue. Allow local access if your browser asks."
          : "The connector download is being prepared. Please return shortly.");
    if (status.textContent !== message) status.textContent = message;
  };
  render();
  download.addEventListener("click", () => { installing = true; render(); });
  connect.addEventListener("click", () => {
    if (!token || !ready || connect.disabled) return;
    session = randomToken();
    launchedAt = Date.now();
    lastSeen = 0;
    snapshot = null;
    localError = "";
    copy.textContent = "Copy address";
    render();
    try { window.location.href = "hosted-comfyui://connect?session=" + session + "#" + token; }
    catch { localError = "Click Reconnect and allow your browser to open the connector."; render(); }
  });
  copy.addEventListener("click", async () => {
    if (!snapshot || snapshot.state !== "connected" || !ready || localError) return;
    try {
      await navigator.clipboard.writeText(snapshot.address);
      copy.textContent = "Copied";
    } catch { copy.textContent = "Select and copy the address above"; }
  });
  const read = async (path, signal) => {
    const response = await fetch("http://127.0.0.1:18187" + path, {
      mode: "cors", credentials: "omit", cache: "no-store", referrerPolicy: "no-referrer",
      redirect: "error", targetAddressSpace: "loopback", signal
    });
    return response.ok ? response.json() : null;
  };
  const check = async () => {
    if (pending || stopped) return;
    clearTimeout(timer);
    if (document.hidden) return;
    const controller = new AbortController();
    pending = controller;
    const timeout = setTimeout(() => controller.abort(), 4500);
    let detected = false;
    let capable = false;
    const requestedSession = session;
    let current = null;
    try {
      const nonce = randomToken();
      const value = await read("/status?nonce=" + nonce, controller.signal);
      detected = value && value.app === "hosted-comfyui-connector" && value.protocol === 1 && value.nonce === nonce;
      capable = detected && value.live_status === 1;
      if (capable && requestedSession) {
        const statusNonce = randomToken();
        current = sessionStatus(await read("/session?session=" + requestedSession + "&nonce=" + statusNonce, controller.signal), requestedSession, statusNonce);
      }
    } catch { }
    finally {
      clearTimeout(timeout);
      pending = null;
    }
    if (stopped) return;
    missedPresence = detected ? 0 : missedPresence + 1;
    if (detected || missedPresence >= 3 || !ready) {
      ready = Boolean(capable);
      outdated = Boolean(detected && !capable);
    }
    if (session && requestedSession === session) {
      if (current) {
        snapshot = current;
        lastSeen = Date.now();
        localError = "";
      } else if (snapshot && Date.now() - lastSeen >= 10000) {
        localError = "Connection status is unavailable. Click Reconnect to check the connection.";
      }
    }
    render();
    if (!document.hidden) timer = setTimeout(check, 2000);
  };
  document.addEventListener("visibilitychange", check);
  window.addEventListener("focus", check);
  window.addEventListener("pagehide", () => {
    stopped = true;
    clearTimeout(timer);
    if (pending) pending.abort();
  });
  window.addEventListener("pageshow", () => { stopped = false; check(); });
  check();
  fetch("downloads/release.json", { cache: "no-store", credentials: "omit", referrerPolicy: "no-referrer" })
    .then((response) => response.ok ? response.json() : null)
    .then((release) => {
      if (release && release.installerAvailable === false) {
        installerAvailable = false;
        download.removeAttribute("href");
        download.setAttribute("aria-disabled", "true");
        render();
      }
    })
    .catch(() => { });
}

if (typeof document !== "undefined") initializePage();
