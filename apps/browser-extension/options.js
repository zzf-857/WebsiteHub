// 选项页：WebHub 地址 + 读写令牌。设置只存 chrome.storage.local（不随账号同步）；
// 访问该地址的主机权限在点击「保存 / 测试连接」时按需申请（optional_host_permissions）。
// 不与 Service Worker 通信：标签组桥接的消息通道保持原样。
(() => {
  "use strict";

  const save = globalThis.WebHubSave;
  const form = document.getElementById("settings-form");
  const originInput = document.getElementById("origin");
  const tokenInput = document.getElementById("token");
  const originWarning = document.getElementById("origin-warning");
  const agentsLink = document.getElementById("agents-link");
  const testButton = document.getElementById("test");
  const clearButton = document.getElementById("clear");
  const status = document.getElementById("status");
  const buttons = Array.from(form.querySelectorAll("button"));
  const TOKEN_PLACEHOLDER = tokenInput.placeholder;

  let stored = null;

  function setStatus(text, tone) {
    status.textContent = text;
    if (tone) status.dataset.tone = tone;
    else delete status.dataset.tone;
  }

  function setBusy(busy) {
    for (const button of buttons) button.disabled = busy;
    form.setAttribute("aria-busy", busy ? "true" : "false");
  }

  function refreshOriginHints() {
    const origin = save.normalizeOrigin(originInput.value);
    originWarning.hidden = !(origin && save.isInsecureRemoteOrigin(origin));
    if (origin) {
      agentsLink.href = save.agentsSettingsUrl(origin);
      agentsLink.hidden = false;
    } else {
      agentsLink.removeAttribute("href");
      agentsLink.hidden = true;
    }
  }

  function showStored() {
    tokenInput.value = "";
    tokenInput.placeholder = stored
      ? `已保存 ${save.tokenPreview(stored.token)}（留空则保持不变）`
      : TOKEN_PLACEHOLDER;
    if (stored) originInput.value = stored.origin;
    refreshOriginHints();
  }

  // 令牌框留空表示沿用已保存的令牌。
  function readForm() {
    const origin = save.normalizeOrigin(originInput.value);
    const rawToken = tokenInput.value.trim();
    if (!origin) {
      return { error: "请填写有效的 WebHub 地址，例如 https://<机器>.<tailnet>.ts.net。" };
    }
    if (rawToken) {
      const token = save.normalizeToken(rawToken);
      if (!token) return { error: "令牌格式不对：应以 whk_ 开头，请从 WebHub 设置页完整复制。" };
      return { origin, token };
    }
    if (stored) return { origin, token: stored.token };
    return { error: "请粘贴读写令牌。" };
  }

  // 必须在点击事件的同步阶段调用，浏览器才会弹出授权确认。
  function requestHostPermission(origin) {
    return chrome.permissions.request({ origins: [save.permissionPattern(origin)] });
  }

  const PERMISSION_DENIED = "没有获得访问这个地址的授权，扩展无法连接 WebHub。";

  // 换了主机就收回旧主机的授权；随扩展声明的本机地址收不回，忽略即可。
  function releaseHostPermission(origin) {
    chrome.permissions.remove({ origins: [save.permissionPattern(origin)] }).catch(() => {});
  }

  function runWithPermission(values, task) {
    const permission = requestHostPermission(values.origin);
    setBusy(true);
    void (async () => {
      try {
        if (!(await permission)) {
          setStatus(PERMISSION_DENIED, "error");
          return;
        }
        await task();
      } catch (error) {
        console.error("WebHub options action failed", error);
        setStatus("操作没有完成，请重试。", "error");
      } finally {
        setBusy(false);
      }
    })();
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const values = readForm();
    if (values.error) {
      setStatus(values.error, "error");
      return;
    }
    runWithPermission(values, async () => {
      const previous = stored;
      const next = { origin: values.origin, token: values.token };
      await chrome.storage.local.set({ [save.SETTINGS_KEY]: next });
      stored = next;
      showStored();
      if (previous &&
        save.permissionPattern(previous.origin) !== save.permissionPattern(next.origin)) {
        releaseHostPermission(previous.origin);
      }
      setStatus("已保存。可以点「测试连接」确认令牌可用。", "success");
    });
  });

  testButton.addEventListener("click", () => {
    const values = readForm();
    if (values.error) {
      setStatus(values.error, "error");
      return;
    }
    setStatus("正在连接…");
    runWithPermission(values, async () => {
      const outcome = await save.callTool({
        fetchImpl: (input, init) => fetch(input, init),
        origin: values.origin,
        token: values.token,
        name: "list_taxonomy",
        arguments: {},
        requestId: `options-${Date.now()}`,
      });
      const result = save.describeConnection(outcome);
      const unsaved = !stored || stored.origin !== values.origin || stored.token !== values.token;
      const suffix = result.ok && unsaved ? "（设置还没保存）" : "";
      setStatus(`${result.text}${suffix}`, result.ok ? "success" : "error");
    });
  });

  clearButton.addEventListener("click", () => {
    const previous = stored;
    setBusy(true);
    void (async () => {
      try {
        await chrome.storage.local.remove(save.SETTINGS_KEY);
        stored = null;
        originInput.value = "";
        showStored();
        if (previous) releaseHostPermission(previous.origin);
        setStatus("已清除地址和令牌。", "success");
      } catch (error) {
        console.error("WebHub options clear failed", error);
        setStatus("清除失败，请重试。", "error");
      } finally {
        setBusy(false);
      }
    })();
  });

  originInput.addEventListener("input", refreshOriginHints);
  originInput.addEventListener("blur", () => {
    const origin = save.normalizeOrigin(originInput.value);
    if (origin) originInput.value = origin;
    refreshOriginHints();
  });

  void (async () => {
    try {
      const values = await chrome.storage.local.get(save.SETTINGS_KEY);
      stored = save.normalizeSettings(values[save.SETTINGS_KEY]);
    } catch {
      stored = null;
    }
    showStored();
  })();
})();
