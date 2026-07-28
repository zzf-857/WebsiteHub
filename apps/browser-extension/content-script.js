(() => {
  "use strict";

  const protocol = globalThis.WebHubProtocol;
  const pendingRequestIds = new Set();
  const RESPONSE_TIMEOUT_MS = 90_000;

  if (!protocol || !protocol.isAllowedLocalUrl(window.location.href)) return;

  function responseEnvelope(requestId, response) {
    const envelope = {
      source: protocol.EXTENSION_SOURCE,
      target: protocol.PAGE_SOURCE,
      version: protocol.VERSION,
      type: "RESPONSE",
      requestId,
      ok: response.ok === true,
    };

    if (response.ok === true) {
      envelope.result = protocol.isRecord(response.result) ? response.result : {};
    } else {
      const error = protocol.isRecord(response.error) ? response.error : {};
      envelope.error = {
        code: typeof error.code === "string" ? error.code : "INTERNAL_ERROR",
        message:
          typeof error.message === "string"
            ? error.message
            : "The browser extension could not complete the request.",
      };
    }
    return envelope;
  }

  function postResponse(origin, requestId, response) {
    window.postMessage(responseEnvelope(requestId, response), origin);
  }

  function extensionUnavailable(origin, requestId) {
    postResponse(origin, requestId, {
      ok: false,
      error: {
        code: "EXTENSION_UNAVAILABLE",
        message: "The WebHub browser extension is unavailable or was reloaded.",
      },
    });
  }

  window.addEventListener("message", (event) => {
    if (event.source !== window) return;
    if (event.origin !== window.location.origin) return;
    if (!protocol.isAllowedLocalUrl(event.origin)) return;
    if (!protocol.isRequestEnvelope(event.data)) return;

    const request = event.data;
    if (pendingRequestIds.has(request.requestId)) {
      postResponse(event.origin, request.requestId, {
        ok: false,
        error: {
          code: "DUPLICATE_REQUEST",
          message: "A request with this requestId is already in progress.",
        },
      });
      return;
    }

    pendingRequestIds.add(request.requestId);
    let settled = false;
    const timeoutId = setTimeout(() => {
      if (settled) return;
      settled = true;
      pendingRequestIds.delete(request.requestId);
      postResponse(event.origin, request.requestId, {
        ok: false,
        error: {
          code: "EXTENSION_TIMEOUT",
          message: "The browser extension did not respond in time. Retrying is safe.",
        },
      });
    }, RESPONSE_TIMEOUT_MS);

    try {
      chrome.runtime.sendMessage(request, (response) => {
        if (settled) return;
        settled = true;
        clearTimeout(timeoutId);
        pendingRequestIds.delete(request.requestId);

        if (chrome.runtime.lastError || !protocol.isRecord(response)) {
          extensionUnavailable(event.origin, request.requestId);
          return;
        }
        postResponse(event.origin, request.requestId, response);
      });
    } catch {
      if (settled) return;
      settled = true;
      clearTimeout(timeoutId);
      pendingRequestIds.delete(request.requestId);
      extensionUnavailable(event.origin, request.requestId);
    }
  });
})();
