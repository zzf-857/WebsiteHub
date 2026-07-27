import { ProviderSettingsWorkspace } from "@/components/settings/provider-settings-workspace";
import { SemanticIndexPanel } from "@/components/settings/semantic-index-panel";

export default function ProvidersSettingsPage() {
  return (
    <>
      <ProviderSettingsWorkspace />
      {/* 语义索引依赖上面配置的 embedding Provider，放在同一页才看得出因果 */}
      <SemanticIndexPanel />
    </>
  );
}
