"use client";

import { useCallback, useRef, useState } from "react";

import { AgentPanel } from "@/components/agent/agent-panel";
import {
  CategorySections,
  type CategorySectionsHandle,
} from "@/components/home/category-sections";
import { HomeMetadataBackfillToolbar } from "@/components/home/home-metadata-backfill-toolbar";
import { LibrarySidebar } from "@/components/home/library-sidebar";
import { PinnedSites } from "@/components/home/pinned-sites";
import { RecentSites } from "@/components/home/recent-sites";
import { SpaceShortcuts } from "@/components/home/space-shortcuts";

// 首页装配层（设计稿 1a 行 50–162）：左侧分类/Space 侧栏 + 右侧内容分区。
// 各数据分区各自负责取数与三态 UI，这里只保管「当前分类 activeId」一个联动状态：
// 侧栏点击 → 分区列表滚动到位；分区滚动经过 → 高亮回写到侧栏与吸顶 Tabs。
// SiteHeader / AuthGate 由 (workspace)/layout.tsx 提供，这里绝不重复渲染；
// 顶栏的吸顶工作态靠 AgentPanel 根元素的 id="agent-panel" 自行观察，无需传参。
export function HomePage() {
  const [activeId, setActiveId] = useState<string | null>(null);
  const [metadataRefreshRevision, setMetadataRefreshRevision] = useState(0);
  const sectionsRef = useRef<CategorySectionsHandle | null>(null);

  // 侧栏选择分类：先同步高亮，再驱动右侧分区滚动
  const handleSelect = useCallback((id: string | null) => {
    setActiveId(id);
    sectionsRef.current?.scrollToCategory(id);
  }, []);

  const refreshMetadataViews = useCallback(() => {
    setMetadataRefreshRevision((revision) => revision + 1);
  }, []);

  return (
    <div className="home-layout">
      <LibrarySidebar
        key={metadataRefreshRevision}
        activeCategoryId={activeId}
        onSelectCategory={handleSelect}
      />
      <div className="home-main">
        <AgentPanel />
        <HomeMetadataBackfillToolbar onCompleted={refreshMetadataViews} />
        <PinnedSites key={`pinned-${metadataRefreshRevision}`} />
        <SpaceShortcuts key={`spaces-${metadataRefreshRevision}`} />
        <RecentSites key={`recent-${metadataRefreshRevision}`} />
        <CategorySections
          key={`categories-${metadataRefreshRevision}`}
          ref={sectionsRef}
          activeId={activeId}
          onActiveChange={setActiveId}
        />
      </div>
    </div>
  );
}
