# Q61 · 信息流分栏看板与单来源页

- 基线：Q59 合并后的 HEAD；分支 `q61-work`
- 迁移：无（全部使用 Q59 的接口）
- 区域：前端 `/feeds`（看板）与新页面 `/feeds/sources/[id]`；类名前缀 `feeds-board-`、`feeds-source-`
- 并行关系：可与 Q60 并行。**不得**改后端（发现接口缺口时停下上报）、`/settings/*`、`components/feeds/feeds-manage-*`
- 决策依据：D10、D5（管理与发现两种密度——看板属于“阅读密度”，介于两者之间，见 §2.1）、D6（乐观更新 + 撤销）
- 新依赖：无（拖动用原生 Pointer 事件，不引入拖放库）

## 1. 背景（必填）

用户要求“同一个订阅源的信息能单独集合分栏”。现在 `/feeds` 只有“今日精选 / 全部 / 管理”三个页签，所有来源混在一条时间线里。
Q59 已提供分组、筛选、游标、批量已读、新内容计数与看板布局接口；本条目把 `/feeds` 改成分栏看板，并为每个来源提供独立页面。
Q60（Agent 频道）的条目会自动出现在看板里——本条目按 Q60 的契约渲染 `importance` 与 `tags`（缺省值时不显示）。

## 2. 功能要求（必填）

### 2.1 视觉与布局（总负责人设计约束）
- 页面结构：页头一行（标题“信息流”、全局未读数、“添加栏”次按钮、“管理来源”文字链接到 `/feeds/manage`）+ 看板区。
- 看板区横向排列栏，**滚动发生在看板容器内**（`overflow-x: auto`，`scroll-snap-type: x proximity`），页面本身不得横向滚动。
- 栏：宽 360px（≥1600px 视口时 400px），高度占满视口剩余空间，栏内纵向滚动；栏间距 `12px`；背景 `--surface`，
  边框 `--line`，圆角 8px；栏头固定在栏顶（`position: sticky`），底边 `--line-soft`。
- 栏头：来源标识（来源 → `feedMarkSource`；分组 → lucide `Folder`；全部 → `Inbox`；精选 → `Sparkles`；星标 → `Star`）、
  标题（单行省略）、未读数（`--text-muted`，0 时隐藏）、“更多”菜单按钮（`MoreHorizontal`）。
- 条目行（阅读密度，复用并扩展 Q51 的 `feed-item-row.tsx`，新增 `variant="compact"`）：
  第一行来源名（仅在多来源栏中显示）+ 相对时间，`--data-font-small`、`--text-muted`；
  第二行标题，最多 2 行，未读 `--text` + 600 字重，已读 `--text-secondary` + 400 字重；
  第三行摘要 1 行（`--text-muted`，只在栏宽 ≥360 时显示）；右侧 64×48 缩略图（有图才占位，规则同 Q51 R8）；
  `importance=3` 的条目左侧 3px `--accent` 竖条，`importance=2` 用 `--accent-mid`；标签最多显示 2 个小标签（`--surface-subtle` 底）。
  行悬停 / 聚焦时右上角浮出操作：星标、标为已读 / 未读、收进收藏库、打开原文。
- 新内容提示：栏顶居中的胶囊按钮“N 条新内容”（`--accent-soft` 底、`--accent-strong` 字），点击把新条目插到顶部并滚到顶。
  **不得**自动把新条目插入正在阅读的列表（避免内容跳动）。
- 空栏：图标 + 一句话（“这个来源暂时没有新内容”/“没有未读条目”）+ 次要操作（“查看全部”或“管理来源”）。
- 加载：栏内骨架行 6 条，形状与条目行一致；错误：栏内错误态 + “重试”。

### 2.2 看板交互
- **R1 布局读写**：进入页面读 `GET /api/feeds/board`；任何布局改动乐观更新并 `PUT` 整体保存（带 `expected_version`）；
  409 时重新读取并提示“布局已在其他窗口修改，已刷新”。
- **R2 添加栏**：“添加栏”打开选择面板（原生 `<dialog>`）：分组区（每个分组一项）、来源区（可搜索，按分组归类）、
  特殊区（全部 / 精选 / 星标）；已在看板上的目标标记“已添加”但仍可再加（允许同一来源一栏“全部”一栏“只看未读”）。12 栏上限时按钮禁用并提示。
- **R3 栏菜单**：只看未读（开关）、全部标为已读（调用 Q59 R8，5 秒撤销，撤销调用 `POST /api/feeds/items/state`）、
  打开来源页（仅来源栏）、左移 / 右移、移除栏（乐观移除 + 5 秒撤销，不弹确认）。
- **R4 拖动排序**：拖栏头排序（Pointer 事件，拖动时栏半透明 + 插入位置指示线 `--accent`）；键盘：栏头聚焦时 `Alt+←/→`。
  减少动态效果时取消过渡动画。
- **R5 栏内数据**：每栏独立请求 `GET /api/feeds/items`（栏的范围 + `unread_only`），每页 30 条，滚动到底自动加载下一页（`IntersectionObserver`）；
  每栏自己的请求序号，丢弃过期响应。精选栏用现有 `/api/feeds/picks`（不分页）。
- **R6 新内容轮询**：页面可见时每 120 秒调用一次 Q59 R9 计数接口（一次请求带全部栏的范围，`since` = 该栏最近一次加载时间）；
  `document.visibilityState === "hidden"` 时暂停，恢复可见时立即补一次。
- **R7 条目操作**：点击条目 → 新标签打开原文（`url`；无 `url` 时打开 Q62 之前的临时详情：栏内展开显示完整摘要与链接列表）并标为已读；
  星标 / 已读 / 未读乐观更新，失败回滚并提示；“收进收藏库”沿用现有保存接口，成功后行内显示“已收藏”。
  同一条目出现在多栏时，状态变化同步到所有栏（栏之间共享一个按条目 id 的状态表，放 `lib/feed-board-state.ts` 的 reducer）。
- **R8 键盘**：`g f` 前往信息流（加入全局快捷键表与帮助）；看板内 `h` / `l` 切换栏、`j` / `k` 栏内上下、`o` 打开原文、`s` 星标、
  `e` 标为已读 / 未读、`a` 收进收藏库、`Shift+E` 当前栏全部标为已读；输入框内不响应（复用 `lib/keyboard-shortcuts.ts`）。
  焦点移动时把条目滚入可见区域（`block: "nearest"`）。
- **R9 手机（<760px）**：看板变为单栏：顶部横向可滚动的栏切换条（胶囊按钮，当前栏 `--accent-soft`），下面只显示当前栏；
  记住上次选中的栏（`localStorage`，读写包 try/catch）。“添加栏”与栏菜单仍可用；拖动排序在手机上隐藏，只保留“左移 / 右移”。

### 2.3 单来源页 `/feeds/sources/[id]`
- **R10** 页头：大号来源标识（64px）、显示名、种类徽标（RSS / 播客 / Agent）、分组、站点链接（`site_url`，Agent 频道显示 slug）、
  健康状态（Q59 `health`，`failing` / `broken` 显示 `last_error` 对应的中文说明与“立即刷新”）、统计（条目数、未读数、最近更新）。
  操作：“加到看板”（已在看板则显示“已在看板”）、“全部标为已读”（撤销）、“设置”（链接到 `/feeds/manage#<id>`）。
- **R11** 时间线：单列，最大宽度 760px 居中，条目行使用 `variant="comfortable"`（摘要 3 行、缩略图 120×80）；按天分段
  （“今天 / 昨天 / 10月30日”，日期分段纯函数放 `lib/feed-day-groups.ts`）；筛选切换：全部 / 未读 / 星标；无限滚动。
- **R12** 来源不存在或不属于本账号：显示“找不到这个来源”与返回链接（后端已是 404）。

### 2.4 迁移现有页面
- **R13** 原“今日精选”页签内容成为看板的“精选”栏；原“全部”页签成为“全部”栏；“管理”页签已由 Q59 改为链接，现移到页头。
  原 `feeds-workspace.tsx` 拆分：看板容器 `components/feeds/feed-board.tsx`、栏 `components/feeds/feed-board-column.tsx`、
  添加栏面板 `components/feeds/feed-board-add-dialog.tsx`、数据 hook `components/feeds/use-feed-board.tsx`；
  每个文件 ≤700 行。旧组件中不再使用的代码删除，不保留注释掉的版本。
- **R14** 顶栏导航“订阅”改称“信息流”（`components/site-header.tsx` 只改这一处文案，本条目拥有该文件的这一行修改权）；
  页面 `<title>` 同步。

## 3. 数据与迁移（必填）

无。只用 Q59 接口：`/api/feeds/board`、`/api/feeds/items`（筛选 + 游标）、`/api/feeds/items/mark-read`、
`/api/feeds/items/state`、`/api/feeds/items/counts`、`/api/feeds/picks`、`/api/feeds/{id}`。

## 4. 接口契约（必填）

不新增后端接口。前端新增纯逻辑模块（全部可单测）：
- `lib/feed-board-state.ts`：`boardReducer(state, action)`——动作 `columns/add|remove|move|toggleUnread|restore`、
  `items/loaded|prepend|patch`（按 id 同步到所有栏）、`items/markScopeRead|undoMarkScopeRead`。
- `lib/feed-board-layout.ts`：`moveColumn`、`insertIndexFromPointer(xPositions, pointerX)`、`columnScopeKey(column)`
  （计数接口的 `scopes` 参数与栏一一对应）。
- `lib/feed-day-groups.ts`：`groupByDay(items, now, timeZone)`。

## 5. 文件清单（必填）

前端：`app/(workspace)/feeds/page.tsx`、新 `app/(workspace)/feeds/sources/[id]/page.tsx`、新 `components/feeds/feed-board.tsx`、
`feed-board-column.tsx`、`feed-board-add-dialog.tsx`、`use-feed-board.tsx`、新 `components/feeds/feed-source-page.tsx`、
`components/feeds/feed-item-row.tsx`（加 variant）、`components/feeds/feeds-workspace.tsx`（拆分后删除或只剩薄壳）、
新 `lib/feed-board-state.ts`、`lib/feed-board-layout.ts`、`lib/feed-day-groups.ts`、`lib/keyboard-shortcuts.ts`（加 `g f` 与看板命令）、`components/site-header.tsx`（R14 一处文案）、
`app/styles/feeds.css`（看板样式超出则新建 `app/styles/feeds-board.css` 并在 `globals.css` 引入）、
测试 `tests/feed-board-state.test.ts`、`tests/feed-board-layout.test.ts`、`tests/feed-day-groups.test.ts`、
`tests/keyboard-shortcuts.test.ts`（追加）、`tests/feeds-rendering.test.mjs`（更新）。

## 6. 测试清单（必填）

- T1 reducer：添加 / 移除 / 移动 / 撤销移除恢复原位置；12 栏上限；同一条目在两栏中，星标后两栏都更新。
- T2 批量已读撤销：`markScopeRead` 后撤销恢复的正好是返回的 id 集合，不影响其间新加载的条目。
- T3 `insertIndexFromPointer` 边界（最左、最右、正中、栏间缝）；`moveColumn` 越界保护。
- T4 `columnScopeKey` 与 Q59 计数接口格式一致（all / picks / starred / source:<id> / group:<id>）。
- T5 `groupByDay`：跨午夜、时区（Asia/Shanghai 与 UTC 不同日）、无 `published_at` 时用 `fetched_at`。
- T6 快捷键：`g f` 导航；看板命令在输入框内不触发；`Shift+E` 与 `e` 区分。
- T7 过期响应：同一栏快速切换“只看未读”两次，只采纳最后一次（用可控的假 fetch 测 hook 依赖的纯函数或请求序号逻辑）。
- T8 渲染断言：看板容器有 `overflow-x: auto` 而 `body` 无横向滚动；条目缩略图规则沿用 Q51；手机断点下渲染栏切换条。

## 7. 验收要点

- 视觉：栏、条目行、新内容胶囊、空 / 加载 / 错误三态与 §2.1 一致；未读 / 已读字重对比清楚；头条竖条不喧宾夺主。
- 截图（种子：3 个分组、8 个 RSS 来源、1 个 Agent 频道含 importance 1/2/3 与标签的条目）：
  看板 5 栏（浅 / 深 1440）、看板手机 390 单栏、单来源页（RSS 与 Agent 各一，浅 / 深）、添加栏面板、空栏。
- 键盘全流程走一遍：`g f` → `l` → `j` ×3 → `s` → `e` → `Shift+E` → 撤销。
- 1440 宽、5 栏、每栏 90 条时滚动无明显卡顿（报告中给出 Performance 面板的一次录制结论）。
