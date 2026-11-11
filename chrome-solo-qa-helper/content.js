(() => {
  "use strict";

  const PAGE_SOURCE = "claude-eval-console";
  const HELPER_SOURCE = "solo-qa-helper";
  const VERSION = chrome.runtime.getManifest().version;
  const ALLOWED_TYPES = new Set([
    "SOLO_QA_SYNC", "SOLO_QA_SUBMIT", "SOLO_GSB_SYNC", "SOLO_GSB_SUBMIT",
  ]);

  function post(type, requestId, payload = {}) {
    window.postMessage(
      { source: HELPER_SOURCE, type, requestId: requestId || "", payload },
      window.location.origin,
    );
  }

  function announce() {
    post("SOLO_QA_BRIDGE_READY", "", { version: VERSION });
  }

  window.addEventListener("message", async (event) => {
    if (event.source !== window || event.origin !== window.location.origin) return;
    const message = event.data;
    if (!message || message.source !== PAGE_SOURCE) return;
    if (message.type === "SOLO_QA_BRIDGE_PING") {
      announce();
      return;
    }
    if (!ALLOWED_TYPES.has(message.type) || typeof message.requestId !== "string") return;
    try {
      const response = await chrome.runtime.sendMessage({
        type: message.type,
        payload: message.payload || {},
      });
      if (!response?.ok) throw new Error(response?.error || "提交助手没有返回结果");
      post("SOLO_QA_BRIDGE_RESULT", message.requestId, response.data || {});
    } catch (error) {
      post("SOLO_QA_BRIDGE_ERROR", message.requestId, {
        error: error instanceof Error ? error.message : String(error),
      });
    }
  });

  announce();
})();
