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
  const connect = document.getElementById("connect");
  const status = document.getElementById("status");
  let token;
  let installRequest = "";
  let channel;
  let confirmed = false;
  let confirmationSaved = false;
  try { confirmed = confirmationSaved = localStorage.getItem("hosted-comfyui-install-confirmed") === "1"; } catch { }
  const renderInstallState = () => {
    document.getElementById("download").hidden = confirmed;
    connect.className = confirmed ? "button primary" : "button secondary";
    connect.textContent = confirmed ? "Connect" : "Already installed? Connect";
    document.getElementById("install-help").textContent = confirmed ? "Help or reinstall" : "Installation help";
    document.getElementById("install-status").textContent = confirmed
      ? (confirmationSaved ? "Installation was confirmed in this browser." : "Installation confirmed for this visit.")
      : "Install the Windows connector to open this workspace.";
  };
  const confirmInstallation = () => {
    confirmed = true;
    try {
      localStorage.setItem("hosted-comfyui-install-confirmed", "1");
      confirmationSaved = true;
    } catch { }
    renderInstallState();
  };
  renderInstallState();
  try {
    token = invitationToken(window.location.hash);
    connect.disabled = false;
    status.textContent = "Allow your browser to open the connector when prompted.";
  } catch {
    status.textContent = "Open the complete invitation link sent by your host. If it still fails, ask for a new link.";
  }
  const openConnector = (automatic = false) => {
    if (!token) return;
    status.textContent = automatic
      ? "Opening the connector. If your browser blocks this, click Connect."
      : "Check your browser's prompt. If nothing opens, use the installation help below.";
    try { window.location.href = "hosted-comfyui://connect#" + token; }
    catch { status.textContent = "Your browser needs another click. Click Connect to continue."; }
  };
  const cancelInstallRequest = () => {
    try {
      const pending = JSON.parse(localStorage.getItem("hosted-comfyui-install-request"));
      if (pending && pending.id === installRequest) localStorage.removeItem("hosted-comfyui-install-request");
    } catch { }
    installRequest = "";
  };
  connect.addEventListener("click", () => { cancelInstallRequest(); openConnector(); });
  const beginInstall = () => {
    document.getElementById("install-guide").open = true;
    if (!token) return;
    installRequest = crypto.randomUUID();
    try {
      localStorage.setItem("hosted-comfyui-install-request", JSON.stringify({ id: installRequest, time: Date.now() }));
    } catch { installRequest = ""; }
  };
  document.getElementById("download").addEventListener("click", beginInstall);
  document.getElementById("reinstall").addEventListener("click", beginInstall);
  document.getElementById("appinstaller-download").addEventListener("click", beginInstall);
  const acknowledgeInstall = () => {
    confirmInstallation();
    if (!token) return;
    document.getElementById("install-guide").open = false;
    status.textContent = "Installation confirmed. Click Connect to open your workspace.";
    connect.focus();
  };
  document.getElementById("installed").addEventListener("click", () => {
    cancelInstallRequest();
    acknowledgeInstall();
    openConnector();
  });
  const receiveInstall = (message) => {
    if (message && message.type === "installation-confirmed") { confirmInstallation(); return; }
    if (!message || message.type !== "installed" || !installRequest || message.requestId !== installRequest) return;
    try {
      const pending = JSON.parse(localStorage.getItem("hosted-comfyui-install-request"));
      if (!pending || pending.id !== installRequest || !Number.isFinite(pending.time) ||
          Date.now() < pending.time || Date.now() - pending.time >= 3600000) return;
    } catch { return; }
    const requestId = installRequest;
    installRequest = "";
    acknowledgeInstall();
    openConnector(true);
    const response = { type: "continuing", requestId };
    if (channel) channel.postMessage(response);
    try {
      localStorage.setItem("hosted-comfyui-install-response", JSON.stringify(response));
      localStorage.removeItem("hosted-comfyui-install-response");
      const pending = JSON.parse(localStorage.getItem("hosted-comfyui-install-request"));
      if (pending && pending.id === requestId) localStorage.removeItem("hosted-comfyui-install-request");
    } catch { }
  };
  if (typeof BroadcastChannel !== "undefined") {
    try {
      channel = new BroadcastChannel("hosted-comfyui-install");
      channel.addEventListener("message", (event) => receiveInstall(event.data));
    } catch { }
  }
  window.addEventListener("storage", (event) => {
    if (event.key === "hosted-comfyui-install-confirmed" && event.newValue === "1") {
      confirmed = confirmationSaved = true;
      renderInstallState();
    }
    if (event.key === "hosted-comfyui-install-event" && event.newValue) {
      try { receiveInstall(JSON.parse(event.newValue)); } catch { }
    }
  });
  fetch("downloads/release.json", { cache: "no-store", credentials: "omit", referrerPolicy: "no-referrer" })
    .then((response) => response.ok ? response.json() : null)
    .then((release) => {
      if (release && release.appInstallerAvailable === true) {
        document.getElementById("appinstaller-option").hidden = false;
      }
      if (release && release.installerAvailable === false) {
        for (const id of ["download", "reinstall"]) {
          const download = document.getElementById(id);
          download.removeAttribute("href");
          download.setAttribute("aria-disabled", "true");
        }
        document.getElementById("download-status").textContent = "The connector download is being prepared. Please return shortly.";
      }
    })
    .catch(() => { });
}

if (typeof document !== "undefined") initializePage();
