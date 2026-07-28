(() => {
  "use strict";

  const PAGE_SOURCE = "webhub-web";
  const EXTENSION_SOURCE = "webhub-browser-extension";
  const VERSION = 1;
  const REQUEST_TYPES = Object.freeze(["PING", "OPEN_SPACE_GROUP"]);
  const REQUEST_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;

  function isRecord(value) {
    if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
    const prototype = Object.getPrototypeOf(value);
    return prototype === Object.prototype || prototype === null;
  }

  function isRequestId(value) {
    return typeof value === "string" && REQUEST_ID_PATTERN.test(value);
  }

  function isAllowedLocalUrl(value) {
    try {
      const url = value instanceof URL ? value : new URL(value);
      return (
        (url.protocol === "http:" || url.protocol === "https:") &&
        (url.hostname === "localhost" || url.hostname === "127.0.0.1") &&
        url.port === "3100"
      );
    } catch {
      return false;
    }
  }

  function isRequestEnvelope(value) {
    return (
      isRecord(value) &&
      value.source === PAGE_SOURCE &&
      value.target === EXTENSION_SOURCE &&
      value.version === VERSION &&
      REQUEST_TYPES.includes(value.type) &&
      isRequestId(value.requestId)
    );
  }

  const protocol = Object.freeze({
    PAGE_SOURCE,
    EXTENSION_SOURCE,
    VERSION,
    REQUEST_TYPES,
    isAllowedLocalUrl,
    isRecord,
    isRequestEnvelope,
    isRequestId,
  });

  Object.defineProperty(globalThis, "WebHubProtocol", {
    configurable: false,
    enumerable: false,
    value: protocol,
    writable: false,
  });
})();
