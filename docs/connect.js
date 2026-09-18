"use strict";

function invitationToken(fragment) {
  const token = fragment.replace(/^#/, "");
  if (!token || token.length > 16384 || !/^[A-Za-z0-9_+/=-]+$/.test(token)) {
    throw new Error("Open the complete invitation link sent by your host.");
  }
  const normalized = token.replace(/-/g, "+").replace(/_/g, "/").replace(/=+$/, "");
  const bytes = Uint8Array.from(atob(normalized), (character) => character.charCodeAt(0));
  const invitation = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
  const legacy = invitation && Object.keys(invitation).sort().join(",") === "endpoint,key,ssh_key,version" &&
      invitation.version === 1 && typeof invitation.endpoint === "string" &&
      /^[a-z0-9]{8,40}$/.test(invitation.endpoint) && typeof invitation.key === "string" &&
      /^[A-Za-z0-9_-]{20,200}$/.test(invitation.key) && typeof invitation.ssh_key === "string" &&
      /^-----BEGIN OPENSSH PRIVATE KEY-----\r?\n[A-Za-z0-9+/=\r\n]+-----END OPENSSH PRIVATE KEY-----\r?\n?$/.test(invitation.ssh_key);
  const enrollment = invitation && Object.keys(invitation).sort().join(",") === "gateway,invite_id,token,version" &&
      invitation.version === 2 && typeof invitation.gateway === "string" &&
      /^https:\/\/[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.workers\.dev$/.test(invitation.gateway) &&
      invitation.gateway === invitation.gateway.trim() && typeof invitation.invite_id === "string" &&
      invitation.invite_id.length === 32 && /^[0-9a-f]{32}$/.test(invitation.invite_id) && typeof invitation.token === "string" &&
      (invitation.token.length === 0 || invitation.token.length === 64) && /^(?:[0-9a-f]{64})?$/.test(invitation.token);
  if (!legacy && !enrollment) {
    throw new Error("This invitation is invalid. Ask your host for a new link.");
  }
  return normalized.replace(/\+/g, "-").replace(/\//g, "_");
}

function randomToken() {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, "0")).join("");
}

function currentConnector(version) {
  if (typeof version !== "string" || !/^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$/.test(version) || version !== version.trim()) return false;
  const parts = version.split(".").map(Number);
  return parts[0] > 1 || (parts[0] === 1 && (parts[1] > 1 || (parts[1] === 1 && parts[2] >= 1)));
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
  return { state: value.state, message: value.message, address, model: value.model, enrolled: value.enrolled === true };
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
  if (document.documentElement.dataset.connectorPage !== "5") {
    const address = new URL(window.location.href);
    if (address.searchParams.get("page") !== "5") {
      address.searchParams.set("page", "5");
      window.location.replace(address.href);
    } else {
      const status = document.getElementById("status");
      if (status) status.textContent = "This page is out of date. Reload it to continue.";
    }
    return;
  }
  const connect = document.getElementById("connect");
  const retry = document.getElementById("retry");
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
  let invitation;
  try {
    token = invitationToken(window.location.hash);
    invitation = JSON.parse(atob(token.replace(/-/g, "+").replace(/_/g, "/")));
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
  let invitationState = !token ? "invalid" : invitation.version === 2 ? (invitation.token ? "checking" : "saved") : "legacy";
  let invitationPending;
  let invitationNextCheck = 0;
  const render = () => {
    const allowed = session || ["legacy", "unused", "redeemed", "saved"].includes(invitationState);
    const installAllowed = session || ["legacy", "unused"].includes(invitationState) || (outdated && ["redeemed", "saved"].includes(invitationState));
    const waiting = session && !snapshot && Date.now() - launchedAt < 20000 && !localError;
    const connected = ready && snapshot?.state === "connected" && !localError;
    const busy = waiting || (ready && snapshot?.state === "starting" && !localError);
    download.hidden = ready || !installAllowed;
    download.textContent = outdated ? "Update connector" : "Download and install";
    connect.hidden = !allowed || !ready || connected || busy;
    connect.disabled = !allowed || !token || !ready || busy;
    retry.hidden = session || invitationState !== "unavailable";
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
    guide.hidden = ready || !installing || !installAllowed;
    const invitationMessage = session ? "" : {
      checking: "Checking invitation…",
      expired: "This invitation has expired. Ask the owner for a new link.",
      revoked: "This invitation's access has been revoked. Ask the owner for a new link.",
      invalid: "This invitation is invalid. Ask the owner for a new link.",
      unavailable: "Could not check this invitation. Try again shortly.",
      redeemed: outdated ? "This invitation has already been accepted. Update your existing connector to reopen access saved on this computer."
        : ready ? "This invitation has already been accepted. Connect if you accepted it on this computer; otherwise ask the owner for a new link."
        : "This invitation has already been accepted. Use Saved workspaces in the connector on the original computer, or ask the owner for a new link.",
      saved: outdated ? "Update your existing connector to open this saved workspace."
        : ready ? "This is a saved workspace. Connect using this computer's saved access."
        : "This link opens a saved workspace on the original computer. Use its connector, or ask the owner for a new invitation."
    }[invitationState];
    const message = !token
      ? "Open the complete invitation link sent by your host."
      : invitationMessage || (outdated ? "Update the connector to open this workspace and show its progress here."
      : !ready && session ? "The local connector is not responding. Open it again or reinstall it."
      : localError || (ready && session
        ? snapshot
          ? (snapshot.state === "connected" ? "Connected. Keep the connector open while working." : snapshot.message)
          : waiting ? "Opening the connector. Allow your browser's prompt."
            : "The connector hasn't opened. Click Reconnect and allow your browser's prompt."
        : ready ? "Connector detected. You're ready to connect."
        : installerAvailable
          ? "Install the connector to continue. Allow local access if your browser asks."
          : "The connector download is being prepared. Please return shortly."));
    if (status.textContent !== message) status.textContent = message;
  };
  render();
  const checkInvitation = async () => {
    if (invitation?.version !== 2 || !invitation.token || invitationPending || session || stopped || document.hidden) return;
    const controller = new AbortController();
    invitationPending = controller;
    const timeout = setTimeout(() => controller.abort(), 5000);
    let state = "unavailable";
    try {
      const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(invitation.token));
      const tokenHash = Array.from(new Uint8Array(bytes), byte => byte.toString(16).padStart(2, "0")).join("");
      const response = await fetch(invitation.gateway + "/v1/invitations/status", {
        method: "POST", mode: "cors", credentials: "omit", cache: "no-store", referrerPolicy: "no-referrer", redirect: "error",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ invite_id: invitation.invite_id, token_hash: tokenHash }), signal: controller.signal
      });
      if (response.ok) {
        const value = await response.json();
        if (value && ["unused", "redeemed", "expired", "revoked", "invalid"].includes(value.state)) state = value.state;
      }
    } catch { }
    finally { clearTimeout(timeout); invitationPending = null; }
    if (stopped || session) return;
    invitationState = state;
    invitationNextCheck = Date.now() + 15000;
    render();
  };
  retry.addEventListener("click", () => {
    invitationState = "checking";
    render();
    checkInvitation();
  });
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
    if (Date.now() >= invitationNextCheck) checkInvitation();
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
      capable = detected && currentConnector(value.version) && value.live_status === 1 && (invitation?.version !== 2 || value.enrollment === 2);
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
        if (current.enrolled && invitation?.version === 2 && invitation.token) {
          invitation = { ...invitation, token: "" };
          token = btoa(JSON.stringify(invitation)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
          const bookmark = new URL(window.location.href);
          bookmark.hash = token;
          window.history.replaceState(null, "", bookmark.href);
        }
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
    if (invitationPending) invitationPending.abort();
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
