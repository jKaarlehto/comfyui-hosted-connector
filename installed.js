"use strict";

if (typeof BroadcastChannel !== "undefined") {
  const channel = new BroadcastChannel("hosted-comfyui-install");
  channel.postMessage({ type: "installed" });
  channel.close();
}
try {
  localStorage.setItem("hosted-comfyui-installed", String(Date.now()));
  localStorage.removeItem("hosted-comfyui-installed");
} catch { }
