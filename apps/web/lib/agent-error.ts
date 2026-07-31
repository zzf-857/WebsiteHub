export type AgentErrorAction = {
  href: "/settings/providers";
  label: string;
};

const PROVIDER_ERROR_ACTION_LABELS: Readonly<Record<string, string>> = {
  provider_not_configured: "去配置 Provider",
  provider_configuration_invalid: "检查 Provider 配置",
  provider_credentials_unavailable: "检查密钥",
  provider_fake_ip_detected: "重新测试连接",
  provider_target_blocked: "检查网络设置",
  provider_target_unavailable: "测试 Provider",
};

export function agentErrorAction(code: string | null | undefined): AgentErrorAction | null {
  if (!code) return null;
  const label = PROVIDER_ERROR_ACTION_LABELS[code];
  return label ? { href: "/settings/providers", label } : null;
}
