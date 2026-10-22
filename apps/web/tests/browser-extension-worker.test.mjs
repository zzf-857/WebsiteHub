// Q47：在假的 chrome.* 环境里加载真实的 service-worker.js（连同 importScripts 引入的
// protocol.js / save-core.js / save-worker.js），检查工具栏按钮、右键菜单、通知按钮的接线，
// 以及标签组桥接的监听器仍然照旧注册。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const extensionDir = new URL("../../browser-extension/", import.meta.url);
const ORIGIN = "https://box.tail1234.ts.net";
const TOKEN = "whk_abcdefghijklmnopqrstuvwxyz0123456789-_AB";
const RESOURCE_ID = "6f1d2c3b-4a5e-4f60-9b1a-2c3d4e5f6a7b";
const SETTINGS_KEY = "webhub.save.settings.v1";

function event() {
  const listeners = [];
  return {
    listeners,
    addListener: (listener) => listeners.push(listener),
    fire: (...args) => listeners.map((listener) => listener(...args)),
  };
}

function toolResponse(payload, isError = false) {
  const text = typeof payload === "string" ? payload : JSON.stringify(payload);
  const body = JSON.stringify({ jsonrpc: "2.0", id: 1, result: { content: [{ type: "text", text }], isError } });
  return { status: 200, type: "basic", text: async () => body };
}

function loadWorker({ settings, granted = true, respond }) {
  const log = { fetches: [], badges: [], titles: [], notifications: [], cleared: [], tabs: [],
    menus: [], optionsOpened: 0, permissionChecks: [] };
  const store = settings === undefined ? {} : { [SETTINGS_KEY]: settings };
  const chrome = {
    runtime: {
      id: "test-extension",
      lastError: undefined,
      getURL: (path) => `chrome-extension://test-extension/${path}`,
      openOptionsPage: (callback) => {
        log.optionsOpened += 1;
        callback?.();
      },
      onMessage: event(),
      onStartup: event(),
      onInstalled: event(),
    },
    storage: {
      local: {
        get: async (keys) => {
          if (typeof keys === "string") return keys in store ? { [keys]: store[keys] } : {};
          return { ...store };
        },
        set: async (values) => Object.assign(store, values),
        remove: async () => {},
      },
    },
    permissions: {
      contains: async ({ origins }) => {
        log.permissionChecks.push(...origins);
        return granted;
      },
    },
    action: {
      onClicked: event(),
      setBadgeBackgroundColor: async () => {},
      setBadgeTextColor: async () => {},
      setBadgeText: async ({ tabId, text }) => log.badges.push([tabId, text]),
      setTitle: async ({ tabId, title }) => log.titles.push([tabId, title]),
    },
    contextMenus: {
      onClicked: event(),
      removeAll: (callback) => {
        log.menus.length = 0;
        callback();
      },
      create: (item, callback) => {
        log.menus.push(item);
        callback?.();
      },
    },
    notifications: {
      onClicked: event(),
      onButtonClicked: event(),
      create: (id, options, callback) => {
        log.notifications.push({ id, options });
        callback?.();
      },
      clear: (id, callback) => {
        log.cleared.push(id);
        callback?.();
      },
    },
    tabs: {
      create: async (options) => log.tabs.push(options.url),
    },
  };
  const context = vm.createContext({
    chrome,
    console: { error: () => {}, warn: () => {}, log: () => {} },
    URL,
    AbortController,
    TextEncoder,
    setTimeout: () => 0,
    clearTimeout: () => {},
    fetch: async (input, init) => {
      log.fetches.push({ input, init: { ...init, body: JSON.parse(init.body) } });
      return respond(input, init);
    },
  });
  context.importScripts = (...names) => {
    for (const name of names) {
      const url = new URL(name, extensionDir);
      vm.runInContext(readFileSync(url, "utf8"), context, { filename: url.pathname });
    }
  };
  context.importScripts("service-worker.js");
  return { chrome, log, store };
}

const settled = () => new Promise((resolve) => setImmediate(resolve));
async function drain() {
  for (let index = 0; index < 20; index += 1) await settled();
}

test("the tab-group bridge listeners are still registered next to the save entry points", () => {
  const { chrome } = loadWorker({ settings: undefined, respond: () => toolResponse({}) });
  assert.equal(chrome.runtime.onMessage.listeners.length, 1);
  assert.equal(chrome.runtime.onStartup.listeners.length, 2);
  assert.equal(chrome.action.onClicked.listeners.length, 1);
  assert.equal(chrome.contextMenus.onClicked.listeners.length, 1);
  assert.equal(chrome.notifications.onClicked.listeners.length, 1);
  assert.equal(chrome.notifications.onButtonClicked.listeners.length, 1);
});

test("install creates the page and link menus and opens the options page when unconfigured", async () => {
  const { chrome, log } = loadWorker({ settings: undefined, respond: () => toolResponse({}) });
  chrome.runtime.onInstalled.fire({ reason: "install" });
  await drain();
  assert.deepEqual(
    log.menus.map((item) => [item.id, item.title, item.contexts.join()]),
    [
      ["webhub-save-page", "收藏到 WebHub", "page"],
      ["webhub-save-link", "收藏这个链接到 WebHub", "link"],
    ],
  );
  assert.equal(log.optionsOpened, 1);
});

test("toolbar click saves the tab, shows ✓ and a notification that opens the detail page", async () => {
  const { chrome, log } = loadWorker({
    settings: { origin: ORIGIN, token: TOKEN },
    respond: () => toolResponse({
      already_saved: false,
      resource: { id: RESOURCE_ID, title: "Hermes Agent", url: "https://github.com/nous/hermes" },
    }),
  });
  chrome.action.onClicked.fire({ id: 7, url: "https://github.com/nous/hermes", title: "nous/hermes" });
  await drain();

  assert.deepEqual(log.permissionChecks, ["https://box.tail1234.ts.net/*"]);
  assert.equal(log.fetches.length, 1);
  assert.equal(log.fetches[0].input, `${ORIGIN}/api/backend/mcp`);
  assert.equal(log.fetches[0].init.headers.Authorization, `Bearer ${TOKEN}`);
  assert.equal(log.fetches[0].init.body.params.name, "save_resource");
  assert.equal(log.fetches[0].init.body.params.arguments.url, "https://github.com/nous/hermes");
  assert.deepEqual(log.badges.map(([, text]) => text), ["…", "✓"]);
  assert.ok(log.badges.every(([tabId]) => tabId === 7));

  assert.equal(log.notifications.length, 1);
  const [notification] = log.notifications;
  assert.equal(notification.options.title, "已收藏到 WebHub");
  assert.equal(notification.options.iconUrl, "chrome-extension://test-extension/icons/icon-128.png");
  assert.equal(notification.options.buttons[0].title, "打开详情");

  chrome.notifications.onButtonClicked.fire(notification.id, 0);
  await drain();
  assert.deepEqual(log.cleared, [notification.id]);
  assert.deepEqual(log.tabs, [`${ORIGIN}/library/${RESOURCE_ID}`]);
});

test("link menu saves the link URL; read-only tokens surface ! and an options button", async () => {
  const { chrome, log } = loadWorker({
    settings: { origin: ORIGIN, token: TOKEN },
    respond: () => toolResponse("this token is read-only: nope", true),
  });
  chrome.contextMenus.onClicked.fire(
    { menuItemId: "webhub-save-link", linkUrl: "https://example.com/post", pageUrl: "https://news.example/" },
    { id: 3, url: "https://news.example/", title: "News" },
  );
  await drain();
  assert.deepEqual(log.fetches[0].init.body.params.arguments, { url: "https://example.com/post" });
  assert.deepEqual(log.badges.map(([, text]) => text), ["…", "!"]);
  const [notification] = log.notifications;
  assert.equal(notification.options.title, "收藏失败");
  assert.equal(notification.options.buttons[0].title, "打开设置");

  chrome.notifications.onClicked.fire(notification.id);
  await drain();
  assert.equal(log.optionsOpened, 1);
  assert.deepEqual(log.tabs, []);
});

test("without a host grant nothing is sent and the user is told to save the options once", async () => {
  const { chrome, log } = loadWorker({
    settings: { origin: ORIGIN, token: TOKEN },
    granted: false,
    respond: () => toolResponse({}),
  });
  chrome.contextMenus.onClicked.fire(
    { menuItemId: "webhub-save-page", pageUrl: "https://example.com/" },
    { id: 4, url: "https://example.com/", title: "Example" },
  );
  await drain();
  assert.equal(log.fetches.length, 0);
  assert.deepEqual(log.badges, [[4, "!"]]);
  assert.match(log.notifications[0].options.message, /授权/);
});

test("an unconfigured extension opens its options page instead of failing silently", async () => {
  const { chrome, log } = loadWorker({ settings: undefined, respond: () => toolResponse({}) });
  chrome.action.onClicked.fire({ id: 5, url: "https://example.com/", title: "Example" });
  await drain();
  assert.equal(log.fetches.length, 0);
  assert.equal(log.optionsOpened, 1);
  assert.equal(log.notifications.length, 0);
  assert.deepEqual(log.badges, [[5, "!"]]);
});
