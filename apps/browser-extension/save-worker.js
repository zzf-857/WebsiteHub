// 「收藏到 WebHub」：工具栏按钮、快捷键（_execute_action，默认 Alt+Shift+S）与右键菜单。
// 与标签组桥接（service-worker.js 的其余部分）完全独立：不共用消息通道、存储键或状态，
// 也不接收任何网页或扩展页面发来的消息——选项页自己读写设置与申请授权。
(() => {
  "use strict";

  const save = globalThis.WebHubSave;
  const MENU_PAGE = "webhub-save-page";
  const MENU_LINK = "webhub-save-link";
  const WEB_PATTERNS = ["http://*/*", "https://*/*"];
  const ICON_PATH = "icons/icon-128.png";
  const DEFAULT_TITLE = "收藏到 WebHub";
  const BADGE_CLEAR_MS = Object.freeze({ saved: 4000, already: 4000, error: 10000 });
  const inFlightUrls = new Set();
  const badgeOwners = new Map();
  let requestCounter = 0;

  function ignoreLastError() {
    void chrome.runtime.lastError;
  }

  async function readSettings() {
    const values = await chrome.storage.local.get(save.SETTINGS_KEY);
    return values[save.SETTINGS_KEY];
  }

  function hasPermission(pattern) {
    return chrome.permissions.contains({ origins: [pattern] });
  }

  function openOptionsPage() {
    chrome.runtime.openOptionsPage(ignoreLastError);
  }

  function nextNonce() {
    requestCounter = (requestCounter + 1) % 1_000_000;
    return `${Date.now()}-${requestCounter}`;
  }

  function isTabId(tabId) {
    return Number.isInteger(tabId) && tabId >= 0;
  }

  // 徽标与提示文字只挂在发起收藏的标签页上；过几秒自动复原，除非期间又有新的收藏覆盖。
  async function showBadge(tabId, state, nonce, title) {
    if (!isTabId(tabId)) return;
    const badge = save.badgeFor(state);
    badgeOwners.set(tabId, nonce);
    try {
      await chrome.action.setBadgeBackgroundColor({ tabId, color: badge.color });
      if (typeof chrome.action.setBadgeTextColor === "function") {
        await chrome.action.setBadgeTextColor({ tabId, color: badge.textColor });
      }
      await chrome.action.setBadgeText({ tabId, text: badge.text });
      await chrome.action.setTitle({ tabId, title: title || DEFAULT_TITLE });
    } catch {
      // 标签页已关闭。
      return;
    }
    const delay = BADGE_CLEAR_MS[state];
    if (!delay) return;
    setTimeout(() => {
      if (badgeOwners.get(tabId) !== nonce) return;
      badgeOwners.delete(tabId);
      chrome.action.setBadgeText({ tabId, text: "" }).catch(() => {});
      chrome.action.setTitle({ tabId, title: DEFAULT_TITLE }).catch(() => {});
    }, delay);
  }

  function notify(feedback) {
    const options = {
      type: "basic",
      iconUrl: chrome.runtime.getURL(ICON_PATH),
      title: feedback.title,
      message: feedback.message,
      priority: 0,
    };
    if (feedback.contextMessage) options.contextMessage = feedback.contextMessage;
    if (feedback.buttonTitle) options.buttons = [{ title: feedback.buttonTitle }];
    chrome.notifications.create(feedback.notificationId, options, ignoreLastError);
  }

  async function saveTarget(target, tabId) {
    const key = target && typeof target.url === "string" ? target.url.trim() : "";
    // 连按快捷键时只发一次；服务端本来也会去重，这里省掉重复的通知。
    if (key && inFlightUrls.has(key)) return;
    if (key) inFlightUrls.add(key);
    const nonce = nextNonce();
    try {
      let settings;
      try {
        settings = await readSettings();
      } catch {
        settings = undefined;
      }
      let pendingBadge = Promise.resolve();
      const feedback = await save.performSave({
        target,
        settings,
        hasPermission,
        fetchImpl: (input, init) => fetch(input, init),
        nonce,
        requestId: nonce,
        onRequest: () => {
          pendingBadge = showBadge(tabId, "pending", nonce, "正在收藏到 WebHub…");
        },
      });
      // 等「…」写完再写结果，免得极快的失败被迟到的进行中徽标盖住。
      await pendingBadge;
      await showBadge(tabId, feedback.state, nonce, `${feedback.title}：${feedback.message}`);
      if (feedback.code === "NOT_CONFIGURED") {
        openOptionsPage();
        return;
      }
      notify(feedback);
    } catch (error) {
      console.error("WebHub save failed unexpectedly", error);
      await showBadge(tabId, "error", nonce, "收藏失败");
    } finally {
      if (key) inFlightUrls.delete(key);
    }
  }

  function targetFromTab(tab) {
    return { url: tab && tab.url, title: tab && tab.title };
  }

  async function openNotificationTarget(notificationId) {
    chrome.notifications.clear(notificationId, ignoreLastError);
    let settings;
    try {
      settings = await readSettings();
    } catch {
      return;
    }
    const destination = save.notificationDestination(notificationId, settings);
    if (!destination) return;
    if (destination.kind === "options") {
      openOptionsPage();
      return;
    }
    try {
      await chrome.tabs.create({ url: destination.url });
    } catch (error) {
      console.warn("Could not open the WebHub resource", error);
    }
  }

  function createMenus() {
    chrome.contextMenus.removeAll(() => {
      ignoreLastError();
      chrome.contextMenus.create(
        {
          id: MENU_PAGE,
          title: DEFAULT_TITLE,
          contexts: ["page"],
          documentUrlPatterns: WEB_PATTERNS,
        },
        ignoreLastError,
      );
      chrome.contextMenus.create(
        {
          id: MENU_LINK,
          title: "收藏这个链接到 WebHub",
          contexts: ["link"],
          targetUrlPatterns: WEB_PATTERNS,
        },
        ignoreLastError,
      );
    });
  }

  chrome.runtime.onInstalled.addListener((details) => {
    createMenus();
    if (details && details.reason === "install") {
      void readSettings()
        .then((settings) => {
          if (!save.normalizeSettings(settings)) openOptionsPage();
        })
        .catch(() => {});
    }
  });

  chrome.runtime.onStartup.addListener(createMenus);

  chrome.action.onClicked.addListener((tab) => {
    void saveTarget(targetFromTab(tab), tab && tab.id);
  });

  chrome.contextMenus.onClicked.addListener((info, tab) => {
    const tabId = tab && tab.id;
    if (info.menuItemId === MENU_LINK) {
      void saveTarget({ url: info.linkUrl }, tabId);
      return;
    }
    if (info.menuItemId !== MENU_PAGE) return;
    // 页面菜单用顶层页面；标签页信息与之一致时再带上标题。
    const url = info.pageUrl || (tab && tab.url);
    const title = tab && tab.url === url ? tab.title : undefined;
    void saveTarget({ url, title }, tabId);
  });

  chrome.notifications.onClicked.addListener((notificationId) => {
    void openNotificationTarget(notificationId);
  });

  chrome.notifications.onButtonClicked.addListener((notificationId) => {
    void openNotificationTarget(notificationId);
  });
})();
