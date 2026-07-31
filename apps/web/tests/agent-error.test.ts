import assert from "node:assert/strict";
import test from "node:test";

import { agentErrorAction } from "../lib/agent-error.ts";

test("Provider 预检错误提供对应的设置操作", () => {
  assert.deepEqual(agentErrorAction("provider_not_configured"), {
    href: "/settings/providers",
    label: "去配置 Provider",
  });
  assert.deepEqual(agentErrorAction("provider_credentials_unavailable"), {
    href: "/settings/providers",
    label: "检查密钥",
  });
  assert.deepEqual(agentErrorAction("provider_fake_ip_detected"), {
    href: "/settings/providers",
    label: "重新测试连接",
  });
  assert.deepEqual(agentErrorAction("provider_target_unavailable"), {
    href: "/settings/providers",
    label: "测试 Provider",
  });
});

test("未知或缺失错误码不生成误导操作", () => {
  assert.equal(agentErrorAction(null), null);
  assert.equal(agentErrorAction("runner_unavailable"), null);
});
