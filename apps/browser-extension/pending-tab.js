(() => {
  "use strict";

  const SOURCE = "webhub-space-pending-tab";
  const match = window.location.hash.match(/^#([a-f0-9]{64}):(\d{1,2}):([A-Za-z0-9-]{16,64})$/);
  if (!match) {
    window.close();
    return;
  }

  const [, operationHash, slotText, registrationToken] = match;
  const slot = Number(slotText);
  let attempts = 0;

  const register = () => {
    attempts += 1;
    chrome.runtime.sendMessage(
      {
        source: SOURCE,
        version: 1,
        type: "REGISTER_PENDING_TAB",
        operationHash,
        registrationToken,
        slot,
      },
      (response) => {
        if (!chrome.runtime.lastError && response && response.ok === true) return;
        if (attempts < 20) window.setTimeout(register, 250);
      },
    );
  };

  register();
})();
