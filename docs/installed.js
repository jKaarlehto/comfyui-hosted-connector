"use strict";

const status = document.getElementById("handoff-status");
let request;
let channel;
let acknowledged = false;
try { request = JSON.parse(localStorage.getItem("hosted-comfyui-install-request")); } catch { }
const fallback = () => {
  if (!acknowledged) status.textContent = "Return to your invitation tab and click Connect. If it is closed or in another browser, reopen your invitation link.";
};
if (request && typeof request.id === "string" && /^[a-f0-9-]{36}$/.test(request.id) && Number.isFinite(request.time) &&
    Date.now() >= request.time && Date.now() - request.time < 3600000) {
  const receive = (message) => {
    if (!message || message.type !== "continuing" || message.requestId !== request.id) return;
    acknowledged = true;
    status.textContent = "Your invitation tab requested the connector. Allow your browser's prompt. If nothing opens, click Connect in that tab.";
  };
  const message = { type: "installed", requestId: request.id };
  if (typeof BroadcastChannel !== "undefined") {
    try {
      channel = new BroadcastChannel("hosted-comfyui-install");
      channel.addEventListener("message", (event) => receive(event.data));
      channel.postMessage(message);
    } catch { }
  }
  window.addEventListener("storage", (event) => {
    if (event.key === "hosted-comfyui-install-response" && event.newValue) {
      try { receive(JSON.parse(event.newValue)); } catch { }
    }
  });
  try {
    localStorage.setItem("hosted-comfyui-install-event", JSON.stringify(message));
    localStorage.removeItem("hosted-comfyui-install-event");
  } catch { }
  setTimeout(fallback, 1800);
} else {
  fallback();
}
