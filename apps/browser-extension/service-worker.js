"use strict";

importScripts("protocol.js");

const protocol = globalThis.WebHubProtocol;
const MAX_TABS = 100;
const MAX_RAW_URLS = MAX_TABS * 10;
const MAX_PENDING_OPERATIONS = 3;
const MAX_URL_LENGTH = 4096;
const MAX_OPERATION_ID_LENGTH = 256;
const MAX_SPACE_ID_LENGTH = 256;
const MAX_SPACE_NAME_LENGTH = 128;
const RECEIPT_PREFIX = "webhub.space-group.receipt.v1.";
const BROWSER_SESSION_KEY = "webhub.browser-session.v1";
const RECEIPT_TTL_MS = 7 * 24 * 60 * 60 * 1000;
const PENDING_RECEIPT_TTL_MS = 15 * 60 * 1000;
const PENDING_TAB_SOURCE = "webhub-space-pending-tab";
const HASH_PATTERN = /^[a-f0-9]{64}$/;
const REGISTRATION_TOKEN_PATTERN = /^[A-Za-z0-9-]{16,64}$/;
const CONTROL_CHARACTER_PATTERN = /[\u0000-\u001f\u007f]/;
const inFlightOperations = new Map();
const inFlightPayloads = new Map();
const receiptLocks = new Map();
let operationQueue = Promise.resolve();
let pendingOperationCount = 0;
let browserSessionPromise = null;

class BridgeError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "BridgeError";
    this.code = code;
  }
}

function success(result) {
  return { ok: true, result };
}

function failure(error) {
  if (error instanceof BridgeError) {
    return { ok: false, error: { code: error.code, message: error.message } };
  }
  console.error("Unexpected WebHub extension error", error);
  return {
    ok: false,
    error: {
      code: "INTERNAL_ERROR",
      message: "The browser extension could not complete the request.",
    },
  };
}

function requireSender(sender) {
  const windowId = sender && sender.tab ? sender.tab.windowId : undefined;
  if (
    !sender ||
    sender.id !== chrome.runtime.id ||
    !sender.tab ||
    !Number.isInteger(windowId) ||
    windowId < 0 ||
    sender.frameId !== 0 ||
    !protocol.isAllowedLocalUrl(sender.url)
  ) {
    throw new BridgeError("INVALID_SENDER", "Requests must come from a WebHub page in a browser tab.");
  }
  return windowId;
}

function requireText(value, field, maximumLength) {
  if (typeof value !== "string") {
    throw new BridgeError(`INVALID_${field}`, `${field} must be a string.`);
  }
  const normalized = value.trim();
  if (
    normalized.length === 0 ||
    normalized.length > maximumLength ||
    CONTROL_CHARACTER_PATTERN.test(normalized)
  ) {
    throw new BridgeError(`INVALID_${field}`, `${field} is empty, too long, or contains control characters.`);
  }
  return normalized;
}

function normalizeUrls(value) {
  if (!Array.isArray(value) || value.length === 0) {
    throw new BridgeError("INVALID_URLS", "urls must contain at least one HTTP or HTTPS URL.");
  }
  if (value.length > MAX_RAW_URLS) {
    throw new BridgeError("TOO_MANY_URLS", "The URL payload exceeds the browser helper limit.");
  }

  const uniqueUrls = [];
  const seen = new Set();
  for (const rawUrl of value) {
    if (typeof rawUrl !== "string" || rawUrl.length === 0 || rawUrl.length > MAX_URL_LENGTH) {
      throw new BridgeError("INVALID_URLS", "Every URL must be a valid HTTP or HTTPS URL.");
    }
    let parsed;
    try {
      parsed = new URL(rawUrl);
    } catch {
      throw new BridgeError("INVALID_URLS", "Every URL must be a valid HTTP or HTTPS URL.");
    }
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
      throw new BridgeError("INVALID_URLS", "Only HTTP and HTTPS URLs can be opened.");
    }
    const normalized = parsed.href;
    if (!seen.has(normalized)) {
      seen.add(normalized);
      uniqueUrls.push(normalized);
    }
  }
  if (uniqueUrls.length > MAX_TABS) {
    throw new BridgeError("TOO_MANY_URLS", `A Space group can open at most ${MAX_TABS} tabs at once.`);
  }
  return uniqueUrls;
}

function normalizeOpenPayload(payload) {
  if (!protocol.isRecord(payload)) {
    throw new BridgeError("INVALID_PAYLOAD", "OPEN_SPACE_GROUP requires a payload object.");
  }
  if (typeof payload.recovery !== "boolean") {
    throw new BridgeError("INVALID_RECOVERY", "recovery must be a boolean.");
  }
  const now = Date.now();
  if (
    !Number.isSafeInteger(payload.operationStartedAt) ||
    payload.operationStartedAt <= 0 ||
    payload.operationStartedAt < now - RECEIPT_TTL_MS - 60_000 ||
    payload.operationStartedAt > now + 60_000
  ) {
    throw new BridgeError(
      "INVALID_OPERATION_STARTED_AT",
      "operationStartedAt must identify a recent browser operation.",
    );
  }
  return {
    operationId: requireText(payload.operationId, "OPERATION_ID", MAX_OPERATION_ID_LENGTH),
    operationStartedAt: payload.operationStartedAt,
    recovery: payload.recovery,
    spaceId: requireText(payload.spaceId, "SPACE_ID", MAX_SPACE_ID_LENGTH),
    spaceName: requireText(payload.spaceName, "SPACE_NAME", MAX_SPACE_NAME_LENGTH),
    urls: normalizeUrls(payload.urls),
  };
}

function apiCall(invoke) {
  return new Promise((resolve, reject) => {
    try {
      invoke((result) => {
        const lastError = chrome.runtime.lastError;
        if (lastError) {
          reject(new Error(lastError.message));
          return;
        }
        resolve(result);
      });
    } catch (error) {
      reject(error);
    }
  });
}

function storageGet(keys) {
  return apiCall((done) => chrome.storage.local.get(keys, done));
}

function storageSet(values) {
  return apiCall((done) => chrome.storage.local.set(values, done));
}

function storageRemove(keys) {
  if (Array.isArray(keys) && keys.length === 0) return Promise.resolve();
  return apiCall((done) => chrome.storage.local.remove(keys, done));
}

function getBrowserSessionId() {
  if (!browserSessionPromise) {
    const pending = (async () => {
      const values = await storageGet([BROWSER_SESSION_KEY]);
      const existing = values[BROWSER_SESSION_KEY];
      if (typeof existing === "string" && REGISTRATION_TOKEN_PATTERN.test(existing)) {
        return existing;
      }
      const created = crypto.randomUUID();
      await storageSet({ [BROWSER_SESSION_KEY]: created });
      return created;
    })();
    browserSessionPromise = pending.catch((error) => {
      browserSessionPromise = null;
      throw error;
    });
  }
  return browserSessionPromise;
}

function rotateBrowserSessionId() {
  const created = crypto.randomUUID();
  const pending = storageSet({ [BROWSER_SESSION_KEY]: created }).then(() => created);
  browserSessionPromise = pending.catch((error) => {
    browserSessionPromise = null;
    throw error;
  });
  return browserSessionPromise;
}

function withReceiptLock(operationHash, task) {
  const previous = receiptLocks.get(operationHash) || Promise.resolve();
  const running = previous.then(task, task);
  const tail = running.then(
    () => undefined,
    () => undefined,
  );
  receiptLocks.set(operationHash, tail);
  return running.finally(() => {
    if (receiptLocks.get(operationHash) === tail) receiptLocks.delete(operationHash);
  });
}

function createTab(windowId, url) {
  return apiCall((done) => chrome.tabs.create({ active: false, url, windowId }, done));
}

function navigateTab(tabId, url) {
  return apiCall((done) => chrome.tabs.update(tabId, { url }, done));
}

function groupTabs(windowId, tabIds) {
  return apiCall((done) =>
    chrome.tabs.group({ createProperties: { windowId }, tabIds }, done),
  );
}

function updateGroup(groupId, spaceName) {
  return apiCall((done) =>
    chrome.tabGroups.update(
      groupId,
      { collapsed: false, color: "green", title: spaceName },
      done,
    ),
  );
}

function activateTab(tabId) {
  return apiCall((done) => chrome.tabs.update(tabId, { active: true }, done));
}

function removeTab(tabId) {
  return apiCall((done) => chrome.tabs.remove(tabId, done));
}

function getTab(tabId) {
  return apiCall((done) => chrome.tabs.get(tabId, done));
}

function queryGroupTabs(groupId) {
  return apiCall((done) => chrome.tabs.query({ groupId }, done));
}

function queryGroups() {
  return apiCall((done) => chrome.tabGroups.query({}, done));
}

function isPendingPlaceholderUrl(value) {
  return typeof value === "string" &&
    value.split("#", 1)[0] === chrome.runtime.getURL("pending.html");
}

async function removePendingPlaceholderTab(tabId) {
  let tab;
  try {
    tab = await getTab(tabId);
  } catch {
    return;
  }
  if (tab.status !== "complete") return;
  const stillPending = typeof tab.pendingUrl === "string"
    ? isPendingPlaceholderUrl(tab.pendingUrl)
    : isPendingPlaceholderUrl(tab.url);
  if (stillPending) await removeTabsIndividually([tabId]);
}

async function removeTabsIndividually(tabIds) {
  const failures = [];
  await Promise.all(
    [...new Set(tabIds)].map(async (tabId) => {
      try {
        await removeTab(tabId);
      } catch (removeError) {
        try {
          await getTab(tabId);
          failures.push(removeError);
        } catch {
          // The tab no longer exists, so cleanup for this id is complete.
        }
      }
    }),
  );
  if (failures.length > 0) {
    throw new Error(`Could not remove ${failures.length} Space tab(s).`);
  }
}

async function sha256(value) {
  const bytes = new TextEncoder().encode(value);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function canonicalPayload(payload) {
  return JSON.stringify({
    spaceId: payload.spaceId,
    spaceName: payload.spaceName,
    urls: payload.urls,
  });
}

async function completedGroupState(receipt) {
  let groups;
  let browserSessionId;
  try {
    [groups, browserSessionId] = await Promise.all([
      queryGroups(),
      getBrowserSessionId(),
    ]);
  } catch {
    return "unknown";
  }
  const sameBrowserSession = receipt.browserSessionId === browserSessionId;
  const candidates = sameBrowserSession
    ? groups.filter((group) => group.id === receipt.result.groupId)
    : groups.filter((group) => group.color === "green" && group.title === receipt.groupTitle);
  if (candidates.length === 0) return "absent";

  let queryFailed = false;
  for (const group of candidates) {
    if (group.color !== "green" || group.title !== receipt.groupTitle) continue;
    try {
      const tabs = await queryGroupTabs(group.id);
      if (tabs.length === receipt.result.openedCount) return "present";
    } catch {
      queryFailed = true;
    }
  }
  return queryFailed ? "unknown" : "absent";
}

function isCompletedReceipt(value) {
  return (
    protocol.isRecord(value) &&
    value.version === 1 &&
    value.status === "completed" &&
    typeof value.payloadHash === "string" &&
    typeof value.browserSessionId === "string" &&
    REGISTRATION_TOKEN_PATTERN.test(value.browserSessionId) &&
    typeof value.groupTitle === "string" &&
    value.groupTitle.length > 0 &&
    value.groupTitle.length <= MAX_SPACE_NAME_LENGTH &&
    !CONTROL_CHARACTER_PATTERN.test(value.groupTitle) &&
    Number.isFinite(value.createdAt) &&
    Number.isFinite(value.expiresAt) &&
    protocol.isRecord(value.result) &&
    Number.isInteger(value.result.openedCount) &&
    value.result.openedCount > 0 &&
    Number.isInteger(value.result.groupId) &&
    value.result.groupId >= 0
  );
}

async function persistCompletedReceipt(
  receiptKey,
  operationHash,
  payloadHash,
  result,
  groupTitle,
  preservePendingTabs = false,
) {
  return withReceiptLock(operationHash, async () => {
    const browserSessionId = await getBrowserSessionId();
    const values = await storageGet([receiptKey]);
    const existing = values[receiptKey];
    if (
      (isCompletedReceipt(existing) || isPendingReceipt(existing)) &&
      existing.payloadHash !== payloadHash
    ) {
      throw new BridgeError(
        "IDEMPOTENCY_CONFLICT",
        "This operationId already belongs to a different Space payload.",
      );
    }
    if (isCompletedReceipt(existing)) {
      const groupState = await completedGroupState(existing);
      if (groupState === "present") return existing.result;
      if (groupState === "unknown") {
        throw new BridgeError(
          "RECOVERY_FAILED",
          "The extension could not confirm its existing Space group.",
        );
      }
    }
    if (
      isPendingReceipt(existing) &&
      existing.browserSessionId === browserSessionId &&
      !preservePendingTabs
    ) {
      await removeTabsIndividually(existing.tabIds.filter((tabId) => tabId !== null));
    }
    const storedResult = {
      openedCount: result.openedCount,
      groupId: result.groupId,
    };
    await storageSet({
      [receiptKey]: {
        version: 1,
        status: "completed",
        payloadHash,
        browserSessionId,
        groupTitle,
        result: storedResult,
        createdAt: Date.now(),
        expiresAt: Date.now() + RECEIPT_TTL_MS,
      },
    });
    return storedResult;
  });
}

function isPendingReceipt(value) {
  if (
    !protocol.isRecord(value) ||
    value.version !== 1 ||
    value.status !== "pending" ||
    typeof value.payloadHash !== "string" ||
    !HASH_PATTERN.test(value.operationHash) ||
    !REGISTRATION_TOKEN_PATTERN.test(value.registrationToken) ||
    typeof value.browserSessionId !== "string" ||
    !REGISTRATION_TOKEN_PATTERN.test(value.browserSessionId) ||
    !Number.isInteger(value.windowId) ||
    value.windowId < 0 ||
    !Number.isInteger(value.expectedTabCount) ||
    value.expectedTabCount < 1 ||
    value.expectedTabCount > MAX_TABS ||
    !Array.isArray(value.tabIds) ||
    value.tabIds.length !== value.expectedTabCount ||
    !Number.isFinite(value.createdAt) ||
    !Number.isFinite(value.expiresAt)
  ) {
    return false;
  }
  if (
    value.groupId !== null &&
    (!Number.isInteger(value.groupId) || value.groupId < 0)
  ) {
    return false;
  }
  const registeredTabIds = value.tabIds.filter((tabId) => tabId !== null);
  const uniqueTabIds = new Set(registeredTabIds);
  return (
    uniqueTabIds.size === registeredTabIds.length &&
    registeredTabIds.every((tabId) => Number.isInteger(tabId) && tabId >= 0)
  );
}

async function readReceipt(receiptKey) {
  const operationHash = receiptOperationHash(receiptKey);
  return withReceiptLock(operationHash, async () => {
    const values = await storageGet([receiptKey]);
    const receipt = values[receiptKey];
    if (receipt === undefined) return null;
    if (isPendingReceipt(receipt)) return receipt;
    if (
      !isCompletedReceipt(receipt) ||
      receipt.expiresAt <= Date.now()
    ) {
      await storageRemove([receiptKey]);
      return null;
    }
    const groupState = await completedGroupState(receipt);
    if (groupState === "unknown") {
      throw new BridgeError(
        "RECOVERY_FAILED",
        "The extension could not confirm its completed Space group.",
      );
    }
    if (groupState === "absent") {
      await storageRemove([receiptKey]);
      return null;
    }
    return receipt;
  });
}

function receiptOperationHash(receiptKey) {
  const operationHash = receiptKey.slice(RECEIPT_PREFIX.length);
  if (!receiptKey.startsWith(RECEIPT_PREFIX) || !HASH_PATTERN.test(operationHash)) {
    throw new Error("Invalid WebHub receipt key");
  }
  return operationHash;
}

async function recoverCompletedReceiptAlias(
  receiptKey,
  operationHash,
  payloadHash,
  operationStartedAt,
) {
  let currentReceipt;
  try {
    currentReceipt = await readReceipt(receiptKey);
  } catch {
    throw new BridgeError("RECOVERY_FAILED", "The extension could not read its recovery receipt.");
  }
  if (currentReceipt && currentReceipt.payloadHash !== payloadHash) {
    throw new BridgeError(
      "IDEMPOTENCY_CONFLICT",
      "This operationId already belongs to a different Space payload.",
    );
  }
  if (isCompletedReceipt(currentReceipt)) {
    return { ...currentReceipt.result, replayed: true };
  }
  if (isPendingReceipt(currentReceipt)) return null;

  let values;
  try {
    values = await storageGet(null);
  } catch {
    throw new BridgeError(
      "RECOVERY_FAILED",
      "The extension could not inspect recent Space operation receipts.",
    );
  }

  const now = Date.now();
  const matches = [];
  for (const [key, value] of Object.entries(values)) {
    if (
      key === receiptKey ||
      !key.startsWith(RECEIPT_PREFIX) ||
      !isCompletedReceipt(value) ||
      value.payloadHash !== payloadHash ||
      value.expiresAt <= now ||
      value.createdAt <= operationStartedAt ||
      value.createdAt > now
    ) {
      continue;
    }
    matches.push(value);
  }
  matches.sort((left, right) => right.createdAt - left.createdAt);
  let match = null;
  let queryFailed = false;
  for (const candidate of matches) {
    const groupState = await completedGroupState(candidate);
    if (groupState === "present") {
      match = candidate;
      break;
    }
    if (groupState === "unknown") queryFailed = true;
  }
  if (!match && queryFailed) {
    throw new BridgeError(
      "RECOVERY_FAILED",
      "The extension could not confirm a matching completed Space group.",
    );
  }
  if (!match) return null;

  try {
    const storedResult = await persistCompletedReceipt(
      receiptKey,
      operationHash,
      payloadHash,
      match.result,
      match.groupTitle,
    );
    return { ...storedResult, replayed: true };
  } catch (error) {
    if (error instanceof BridgeError && error.code === "IDEMPOTENCY_CONFLICT") {
      throw error;
    }
    throw new BridgeError(
      "ALIAS_STORAGE_FAILED",
      "The extension found the completed group but could not save its recovery receipt.",
    );
  }
}

async function recoverPendingReceipt(receiptKey, constraints = {}) {
  const operationHash = receiptOperationHash(receiptKey);
  return withReceiptLock(operationHash, async () => {
    const browserSessionId = await getBrowserSessionId();
    const values = await storageGet([receiptKey]);
    const receipt = values[receiptKey];
    if (!isPendingReceipt(receipt)) return false;
    if (
      (constraints.payloadHash && receipt.payloadHash !== constraints.payloadHash) ||
      (constraints.registrationToken &&
        receipt.registrationToken !== constraints.registrationToken) ||
      (constraints.expiredOnly && receipt.expiresAt > Date.now())
    ) {
      return false;
    }
    const registeredTabIds = receipt.tabIds.filter((tabId) => tabId !== null);
    if (receipt.browserSessionId !== browserSessionId) {
      if (registeredTabIds.length > 0 || receipt.groupId !== null) {
        throw new BridgeError(
          "CROSS_SESSION_PENDING",
          "The browser restarted while this Space group was still being created.",
        );
      }
    } else {
      await removeTabsIndividually(registeredTabIds);
    }
    await storageRemove([receiptKey]);
    return true;
  });
}

async function rollbackPendingOperation(
  receiptKey,
  operationHash,
  registrationToken,
  localTabIds,
) {
  return withReceiptLock(operationHash, async () => {
    const browserSessionId = await getBrowserSessionId();
    const values = await storageGet([receiptKey]);
    const receipt = values[receiptKey];
    if (isCompletedReceipt(receipt)) return false;
    const receiptBelongsToSession = isPendingReceipt(receipt) &&
      receipt.browserSessionId === browserSessionId;
    if (receiptBelongsToSession && receipt.registrationToken !== registrationToken) {
      throw new Error("A newer pending Space operation owns this receipt.");
    }
    const receiptTabIds = receiptBelongsToSession
      ? receipt.tabIds.filter((tabId) => tabId !== null)
      : [];
    await removeTabsIndividually([...localTabIds, ...receiptTabIds]);
    await storageRemove([receiptKey]);
    return true;
  });
}

async function recoverMatchingPendingReceipts(payloadHash, excludedReceiptKey) {
  const values = await storageGet(null);
  for (const [key, value] of Object.entries(values)) {
    if (
      key === excludedReceiptKey ||
      !key.startsWith(RECEIPT_PREFIX) ||
      !isPendingReceipt(value) ||
      value.payloadHash !== payloadHash
    ) {
      continue;
    }
    await recoverPendingReceipt(key, { payloadHash });
  }
}

function pendingTabUrl(operationHash, slot, registrationToken) {
  return `${chrome.runtime.getURL("pending.html")}#${operationHash}:${slot}:${registrationToken}`;
}

function pendingRegistrationParts(message, sender) {
  if (
    !protocol.isRecord(message) ||
    message.source !== PENDING_TAB_SOURCE ||
    message.version !== 1 ||
    message.type !== "REGISTER_PENDING_TAB" ||
    !HASH_PATTERN.test(message.operationHash) ||
    !REGISTRATION_TOKEN_PATTERN.test(message.registrationToken) ||
    !Number.isInteger(message.slot) ||
    message.slot < 0 ||
    message.slot >= MAX_TABS ||
    !sender ||
    sender.id !== chrome.runtime.id ||
    !sender.tab ||
    !Number.isInteger(sender.tab.id) ||
    sender.frameId !== 0 ||
    typeof sender.url !== "string" ||
    sender.url.split("#", 1)[0] !== chrome.runtime.getURL("pending.html")
  ) {
    throw new BridgeError("INVALID_PENDING_TAB", "The pending tab registration is invalid.");
  }
  return {
    operationHash: message.operationHash,
    registrationToken: message.registrationToken,
    slot: message.slot,
    tabId: sender.tab.id,
  };
}

async function registerPendingTab(receiptKey, parts) {
  return withReceiptLock(parts.operationHash, async () => {
    const browserSessionId = await getBrowserSessionId();
    const values = await storageGet([receiptKey]);
    const receipt = values[receiptKey];
    if (
      !isPendingReceipt(receipt) ||
      receipt.browserSessionId !== browserSessionId ||
      receipt.operationHash !== parts.operationHash ||
      receipt.registrationToken !== parts.registrationToken ||
      parts.slot >= receipt.expectedTabCount
    ) {
      throw new BridgeError(
        "PENDING_TAB_EXPIRED",
        "The pending Space operation no longer exists.",
      );
    }

    const existingTabId = receipt.tabIds[parts.slot];
    if (existingTabId !== null && existingTabId !== parts.tabId) {
      throw new BridgeError(
        "PENDING_TAB_CONFLICT",
        "The pending Space tab slot is already occupied.",
      );
    }
    if (receipt.tabIds.some((tabId, index) => index !== parts.slot && tabId === parts.tabId)) {
      throw new BridgeError(
        "PENDING_TAB_CONFLICT",
        "The pending Space tab is already registered.",
      );
    }

    const tabIds = [...receipt.tabIds];
    tabIds[parts.slot] = parts.tabId;
    const updatedReceipt = {
      ...receipt,
      tabIds,
      expiresAt: Date.now() + PENDING_RECEIPT_TTL_MS,
    };
    await storageSet({ [receiptKey]: updatedReceipt });
    return updatedReceipt;
  });
}

async function handlePendingTabRegistration(message, sender) {
  let parts;
  try {
    parts = pendingRegistrationParts(message, sender);
    const receiptKey = `${RECEIPT_PREFIX}${parts.operationHash}`;
    await registerPendingTab(receiptKey, parts);
    if (!inFlightOperations.has(parts.operationHash)) {
      await recoverPendingReceipt(receiptKey, {
        registrationToken: parts.registrationToken,
      });
      return { registered: true, recovered: true };
    }
    return { registered: true, recovered: false };
  } catch (error) {
    if (
      sender &&
      sender.tab &&
      Number.isInteger(sender.tab.id) &&
      typeof sender.url === "string" &&
      sender.url.split("#", 1)[0] === chrome.runtime.getURL("pending.html")
    ) {
      try {
        await removePendingPlaceholderTab(sender.tab.id);
      } catch (cleanupError) {
        console.warn("Could not remove an invalid WebHub pending tab", cleanupError);
      }
    }
    throw error;
  }
}

async function cleanupExpiredReceipts() {
  const values = await storageGet(null);
  const now = Date.now();
  for (const [key, value] of Object.entries(values)) {
    if (!key.startsWith(RECEIPT_PREFIX)) continue;
    const operationHash = key.slice(RECEIPT_PREFIX.length);
    if (inFlightOperations.has(operationHash)) continue;

    if (isPendingReceipt(value)) {
      if (value.expiresAt > now) continue;
      try {
        await recoverPendingReceipt(key, { expiredOnly: true });
      } catch (error) {
        console.warn("Could not clean an expired WebHub Space operation", error);
      }
      continue;
    }
    if (isCompletedReceipt(value) && value.expiresAt > now) continue;
    await withReceiptLock(operationHash, async () => {
      const currentValues = await storageGet([key]);
      const current = currentValues[key];
      const staleCompleted = isCompletedReceipt(current) && current.expiresAt <= Date.now();
      if (current !== undefined && !isPendingReceipt(current) &&
        (!isCompletedReceipt(current) || staleCompleted)) {
        await storageRemove([key]);
      }
    });
  }
}

async function executeOpenOperation(payload, windowId, receiptKey, payloadHash, operationHash) {
  let browserSessionId;
  try {
    browserSessionId = await getBrowserSessionId();
  } catch {
    throw new BridgeError(
      payload.recovery ? "RECOVERY_FAILED" : "STORAGE_FAILED",
      "The extension could not prepare its browser session.",
    );
  }

  let receipt;
  try {
    receipt = await readReceipt(receiptKey);
  } catch {
    throw new BridgeError(
      payload.recovery ? "RECOVERY_FAILED" : "STORAGE_FAILED",
      "The extension could not read its operation receipt.",
    );
  }

  if (receipt && receipt.payloadHash !== payloadHash) {
    throw new BridgeError(
      "IDEMPOTENCY_CONFLICT",
      "This operationId was already used with a different Space payload.",
    );
  }
  if (isCompletedReceipt(receipt)) {
    return { ...receipt.result, replayed: true };
  }
  if (isPendingReceipt(receipt)) {
    try {
      await recoverPendingReceipt(receiptKey, { payloadHash });
    } catch (error) {
      if (error instanceof BridgeError && error.code === "CROSS_SESSION_PENDING") {
        throw error;
      }
      throw new BridgeError(
        "RECOVERY_FAILED",
        "The extension could not clean up the interrupted Space operation.",
      );
    }
  }
  try {
    await recoverMatchingPendingReceipts(payloadHash, receiptKey);
  } catch (error) {
    if (error instanceof BridgeError && error.code === "CROSS_SESSION_PENDING") {
      throw error;
    }
    throw new BridgeError(
      "RECOVERY_FAILED",
      "The extension could not clean up an earlier matching Space operation.",
    );
  }

  const registrationToken = crypto.randomUUID();
  const tabIds = Array(payload.urls.length).fill(null);
  const createdAt = Date.now();
  let groupId = null;
  let stage = "prepare";
  const persistPending = () => withReceiptLock(operationHash, () => storageSet({
    [receiptKey]: {
      version: 1,
      status: "pending",
      payloadHash,
      operationHash,
      registrationToken,
      browserSessionId,
      windowId,
      expectedTabCount: payload.urls.length,
      tabIds: [...tabIds],
      groupId,
      createdAt,
      expiresAt: Date.now() + PENDING_RECEIPT_TTL_MS,
    },
  }));

  try {
    // Persist intent before the first browser side effect. Each new tab first
    // loads an extension page which independently registers its id, closing
    // the tabs.create -> receipt checkpoint gap if this worker is restarted.
    await persistPending();
    for (const [slot, url] of payload.urls.entries()) {
      stage = "create";
      const tab = await createTab(
        windowId,
        pendingTabUrl(operationHash, slot, registrationToken),
      );
      if (!tab || !Number.isInteger(tab.id)) throw new Error("Created tab has no id");
      tabIds[slot] = tab.id;
      stage = "register";
      await registerPendingTab(receiptKey, {
        operationHash,
        registrationToken,
        slot,
        tabId: tab.id,
      });
      stage = "navigate";
      const navigatedTab = await navigateTab(tab.id, url);
      if (!navigatedTab || navigatedTab.id !== tab.id) {
        throw new Error("Created tab could not navigate to its target");
      }
    }

    stage = "group";
    groupId = await groupTabs(windowId, tabIds);
    if (!Number.isInteger(groupId) || groupId < 0) throw new Error("Created group has no id");
    stage = "checkpoint";
    await persistPending();

    stage = "update";
    const updatedGroup = await updateGroup(groupId, payload.spaceName);
    if (!updatedGroup || updatedGroup.id !== groupId) {
      throw new Error("Created group could not be updated");
    }

    stage = "activate";
    const activatedTab = await activateTab(tabIds[0]);
    if (!activatedTab || activatedTab.id !== tabIds[0]) {
      throw new Error("Created tab could not be activated");
    }

    const result = {
      openedCount: tabIds.length,
      groupId,
    };
    stage = "persist";
    await persistCompletedReceipt(
      receiptKey,
      operationHash,
      payloadHash,
      result,
      payload.spaceName,
      true,
    );
    return { ...result, replayed: false };
  } catch (error) {
    try {
      await rollbackPendingOperation(
        receiptKey,
        operationHash,
        registrationToken,
        tabIds.filter((tabId) => tabId !== null),
      );
    } catch (cleanupError) {
      console.warn("Could not clean up tabs after a failed Space operation", cleanupError);
      throw new BridgeError(
        "RECOVERY_FAILED",
        "The extension could not completely roll back the failed Space operation.",
      );
    }

    const stageErrors = {
      prepare: ["STORAGE_FAILED", "The extension could not prepare its operation receipt."],
      create: ["TAB_CREATE_FAILED", "The browser could not create every Space tab."],
      register: ["STORAGE_FAILED", "The extension could not register a pending Space tab."],
      navigate: ["TAB_NAVIGATE_FAILED", "The browser could not navigate every Space tab."],
      checkpoint: ["STORAGE_FAILED", "The extension could not update its operation receipt."],
      group: ["TAB_GROUP_FAILED", "The browser could not group the newly created tabs."],
      update: ["TAB_GROUP_UPDATE_FAILED", "The browser could not name the new tab group."],
      activate: ["TAB_ACTIVATE_FAILED", "The browser could not activate the first Space tab."],
      persist: ["STORAGE_FAILED", "The extension could not save its operation receipt."],
    };
    const [code, message] = stageErrors[stage] || ["INTERNAL_ERROR", "The Space group could not be opened."];
    throw new BridgeError(code, message);
  }
}

function enqueueOperation(task) {
  if (pendingOperationCount >= MAX_PENDING_OPERATIONS) {
    return Promise.reject(new BridgeError(
      "EXTENSION_BUSY",
      "The browser helper already has too many pending Space operations.",
    ));
  }
  pendingOperationCount += 1;
  const queued = operationQueue.then(task, task);
  operationQueue = queued.then(
    () => undefined,
    () => undefined,
  );
  return queued.finally(() => {
    pendingOperationCount -= 1;
  });
}

async function openSpaceGroup(payloadValue, windowId) {
  const payload = normalizeOpenPayload(payloadValue);
  const [operationHash, payloadHash] = await Promise.all([
    sha256(payload.operationId),
    sha256(canonicalPayload(payload)),
  ]);
  const receiptKey = `${RECEIPT_PREFIX}${operationHash}`;

  const inFlight = inFlightOperations.get(operationHash);
  if (inFlight) {
    if (inFlight.payloadHash !== payloadHash) {
      throw new BridgeError(
        "IDEMPOTENCY_CONFLICT",
        "This operationId is already running with a different Space payload.",
      );
    }
    const result = await inFlight.promise;
    return { ...result, replayed: true };
  }

  const matchingPayload = inFlightPayloads.get(payloadHash);
  if (matchingPayload) {
    const aliasPromise = (async () => {
      const result = await matchingPayload.promise;
      try {
        await persistCompletedReceipt(
          receiptKey,
          operationHash,
          payloadHash,
          result,
          payload.spaceName,
        );
      } catch (error) {
        if (error instanceof BridgeError) throw error;
        throw new BridgeError(
          "ALIAS_STORAGE_FAILED",
          "The extension could not save the merged operation receipt.",
        );
      }
      return { ...result, replayed: true };
    })();
    inFlightOperations.set(operationHash, { payloadHash, promise: aliasPromise });
    try {
      return await aliasPromise;
    } finally {
      const current = inFlightOperations.get(operationHash);
      if (current && current.promise === aliasPromise) inFlightOperations.delete(operationHash);
    }
  }

  if (payload.recovery) {
    const recovered = await recoverCompletedReceiptAlias(
      receiptKey,
      operationHash,
      payloadHash,
      payload.operationStartedAt,
    );
    if (recovered) return recovered;
  }

  const operationPromise = enqueueOperation(() => executeOpenOperation(
    payload,
    windowId,
    receiptKey,
    payloadHash,
    operationHash,
  ));
  inFlightOperations.set(operationHash, { payloadHash, promise: operationPromise });
  inFlightPayloads.set(payloadHash, { operationHash, promise: operationPromise });
  try {
    return await operationPromise;
  } finally {
    const current = inFlightOperations.get(operationHash);
    if (current && current.promise === operationPromise) inFlightOperations.delete(operationHash);
    const currentPayload = inFlightPayloads.get(payloadHash);
    if (currentPayload && currentPayload.promise === operationPromise) {
      inFlightPayloads.delete(payloadHash);
    }
  }
}

async function handleMessage(message, sender) {
  const windowId = requireSender(sender);
  if (!protocol.isRequestEnvelope(message)) {
    throw new BridgeError("INVALID_MESSAGE", "The extension received an invalid request envelope.");
  }

  if (message.type === "PING") {
    return { capabilities: ["tabGroups"], maxTabs: MAX_TABS };
  }
  if (message.type === "OPEN_SPACE_GROUP") {
    return openSpaceGroup(message.payload, windowId);
  }
  throw new BridgeError("UNSUPPORTED_TYPE", "The extension does not support this request type.");
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  const isPendingRegistration = protocol.isRecord(message) && message.source === PENDING_TAB_SOURCE;
  const task = isPendingRegistration
    ? handlePendingTabRegistration(message, sender)
    : handleMessage(message, sender);
  void task
    .then((result) => sendResponse(success(result)))
    .catch((error) => sendResponse(failure(error)));
  if (!isPendingRegistration) {
    void cleanupExpiredReceipts().catch((error) => {
      console.warn("Could not clean expired WebHub operation receipts", error);
    });
  }
  return true;
});

chrome.runtime.onStartup.addListener(() => {
  void rotateBrowserSessionId()
    .then(() => cleanupExpiredReceipts())
    .catch((error) => {
      console.warn("Could not initialize the WebHub browser session", error);
    });
});
