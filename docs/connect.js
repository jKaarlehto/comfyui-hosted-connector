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

function initializePage() {
  if (document.documentElement.dataset.connectorPage !== "2") {
    const address = new URL(window.location.href);
    if (address.searchParams.get("page") !== "2") {
      address.searchParams.set("page", "2");
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
  let token;
  try {
    token = invitationToken(window.location.hash);
  } catch { }
  let ready = false;
  let installing = false;
  let opening = false;
  let installerAvailable = true;
  let timer;
  let pending;
  let stopped = false;
  const render = () => {
    download.hidden = ready;
    connect.hidden = !ready;
    connect.disabled = !token || !ready;
    guide.hidden = ready || !installing;
    status.textContent = !token
      ? "Open the complete invitation link sent by your host."
      : ready
        ? (opening ? "Opening the connector. Allow your browser's prompt." : "Connector detected. You're ready to connect.")
        : installerAvailable
          ? "Install the connector to continue. Allow local access if your browser asks."
          : "The connector download is being prepared. Please return shortly.";
  };
  render();
  download.addEventListener("click", () => { installing = true; render(); });
  connect.addEventListener("click", () => {
    if (!token || !ready) return;
    opening = true;
    render();
    try { window.location.href = "hosted-comfyui://connect#" + token; }
    catch { status.textContent = "Click Connect again and allow your browser to open the connector."; }
  });
  const check = async () => {
    if (pending || stopped) return;
    clearTimeout(timer);
    if (document.hidden) return;
    const controller = new AbortController();
    pending = controller;
    const timeout = setTimeout(() => controller.abort(), 4500);
    let available = false;
    try {
      const nonce = Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, "0")).join("");
      const response = await fetch("http://127.0.0.1:18187/status?nonce=" + nonce, {
        mode: "cors", credentials: "omit", cache: "no-store", referrerPolicy: "no-referrer",
        redirect: "error", targetAddressSpace: "loopback", signal: controller.signal
      });
      if (response.ok) {
        const value = await response.json();
        available = value && value.app === "hosted-comfyui-connector" && value.protocol === 1 && value.nonce === nonce;
      }
    } catch { }
    finally {
      clearTimeout(timeout);
      pending = null;
    }
    if (stopped) return;
    if (!available) opening = false;
    ready = Boolean(available);
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
