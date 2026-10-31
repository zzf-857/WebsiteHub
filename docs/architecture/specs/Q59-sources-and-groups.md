# Q59 · 来源管理：分组、每源设置、看板布局与 OPML

- 基线：Q65 合并后的 `claude/loving-maxwell-gfbcxo` HEAD（开工前向总负责人确认哈希）；分支 `q59-work`
- 迁移：`20261101_0029`，`down_revision = "20261101_0028"`（Q65）
- 区域：后端 `feeds/`；前端新页面 `/feeds/manage`，类名前缀 `feeds-`（管理页用 `feeds-manage-` 细分）
- 并行关系：无（Q60、Q61 都依赖本条目）。**不改** `/feeds` 看板主页面的布局（那是 Q61），只把现有“管理”页签链接到新页面
- 决策依据：`ITERATION_QUEUE.md` “2026-11 方向扩展” D9 / D10
- 新依赖：无（OPML 用标准库 `xml.etree.ElementTree` + 下文的安全限制）

## 1. 背景（必填）

现在订阅只有一个扁平列表：不能分组、不能改名、拉取间隔与保留期写死（`POLL_INTERVAL` 60 分钟、`RETENTION` 30 天）、
条目列表没有游标分页（`list_items` 一次最多 200 条）、不能按分组 / 星标 / 未读筛选，也没法从其他阅读器迁移（无 OPML）。
这些是 D10 分栏看板与 Q60 Agent 频道的地基。

## 2. 功能要求（必填）

### 来源与分组
- **R1 分组**：每账号至多 30 个分组；名称 1–40 字（NFKC + 空白折叠后按大小写无关去重）；有 `position`。
  接口：列出、新建、改名、删除（来源回到“未分组”，条目不受影响）、整体重排。
- **R2 来源设置**：`PATCH /api/feeds/{id}` 增加可选字段（`None` = 不改）：
  - `custom_title`（≤200，空串 = 清除，显示名 = `custom_title or title`）
  - `group_id`（`null` 显式移出分组；用字段“是否出现”区分“不改”与“移出”，Pydantic `model_fields_set`）
  - `fetch_interval_minutes` ∈ {15, 30, 60, 180, 360, 1440}（仅 `rss` / `podcast`）
  - `retention_days` ∈ {7, 30, 90, 365}
  - `enabled`（已有）
  - 必带 `expected_version`，冲突 409 `version_conflict`。
- **R3 排序**：`PUT /api/feeds/order` `{ "group_id": "…|null", "feed_ids": [...] }` 设定某分组内来源顺序；
  列表须正好是该分组当前全部来源，否则 409 `order_conflict`。
- **R4 拉取间隔与保留期生效**：调度用 `fetch_interval_minutes`（缺省 60），失败退避仍用现有 `backoff_delay`，
  但基数改为该来源的间隔；`cleanup_items` 按来源的 `retention_days`（缺省 30）判定过期；每源条目上限常量
  改为按种类的映射 `MAX_ITEMS_PER_SOURCE = {"rss": 200, "podcast": 200}`（Q60 加 `agent`）。
- **R5 健康状态**：来源响应包含 `last_fetched_at`、`next_fetch_at`、`consecutive_failures`、`last_error`（已是固定码）、
  `unread_count`、`item_count`；`health` 派生字段：`ok`（最近一次成功）/ `failing`（连续失败 1–4 次）/
  `broken`（≥5 次）/ `paused`（`enabled=false`）。派生规则写成纯函数 `feeds/health.py::feed_health`。

### 条目
- **R6 星标**：`feed_items.starred_at`；`PATCH /api/feeds/items/{id}` 增加 `starred: bool`。
  星标条目与已收藏条目一样**永不**被裁剪或成为墓碑（`_trim_feed`、`cleanup_items` 条件加 `starred_at IS NULL`）。
- **R7 列表查询**：`GET /api/feeds/items` 参数：`source_id`、`group_id`（`"none"` = 未分组）、`starred`、`unread`、
  `kind`、`cursor`、`limit`（1–100，缺省 30）。排序 `coalesce(published_at, fetched_at) DESC, id DESC`；
  游标编码排序键 + 全部筛选参数（筛选变了旧游标 422 `invalid_cursor`）。响应 `{ items, next_cursor }`。
  原有调用方（前端“全部”页签）改用新参数，保持行为。
- **R8 批量已读与撤销**：
  - `POST /api/feeds/items/mark-read` `{ "scope": { "kind": "all|source|group|starred", "id": "…|null" }, "before": "<ISO 时间>" }`
    把范围内 `fetched_at <= before` 且未读的条目置为已读，返回 `{ "item_ids": [...], "count": n }`（至多 2,000 条，超出返回 `truncated: true`）。
  - `POST /api/feeds/items/state` `{ "item_ids": [...≤2000], "read": false }` 用于撤销（只接受 `read`、`starred` 两个字段）。
  - 已读是使用信号：不改任何 `version`。
- **R9 新内容计数**：`GET /api/feeds/items/counts?since=<ISO>&scopes=source:<id>,group:<id>,all,picks,starred`
  返回每个范围 `fetched_at > since` 的未读条目数（最多 20 个范围）。供 Q61 的“N 条新内容”提示。

### 看板布局
- **R10** 新表 `feed_boards`（每账号一行），`columns_json` 存栏列表；接口：
  - `GET /api/feeds/board` → `{ "version": n, "columns": [...] }`；无记录时返回**默认布局**（不写库）：
    `[精选, 全部]` + 前 4 个分组各一栏。默认布局由纯函数 `feeds/board.py::default_columns` 生成。
  - `PUT /api/feeds/board` `{ "expected_version": n|0, "columns": [...] }` 整体替换（0 表示首次创建）。
  - 栏：`{ "id": "<客户端生成的 uuid>", "kind": "all|picks|starred|source|group", "target_id": "…|null", "unread_only": bool }`，
    至多 12 栏；`source` / `group` 必须是本账号的有效 id（否则 422 `invalid_column_target`）；`id` 在列表内唯一。
  - 读取时自动丢弃目标已被删除的栏（不写库，下一次保存时自然清除）。

### OPML
- **R11 导出** `GET /api/feeds/opml` → `text/x-opml` 附件；分组 → 一级 `<outline text="组名">`，来源为其子项
  （`type="rss" text title xmlUrl htmlUrl`）；未分组来源放顶层。不导出 `agent` 来源。
- **R12 导入** `POST /api/feeds/opml/import?dry_run=true|false`，请求体为 OPML 文本（≤2 MiB，`Content-Type`
  `text/x-opml` / `application/xml` / `text/xml`）：
  - 安全：拒绝含 `<!DOCTYPE` 或 `<!ENTITY` 的文档（422 `invalid_opml`），只读取 `outline` 的 `xmlUrl`、`text`、`title`；
    URL 必须是 http(s)，经 `normalize_feed_url`；**导入时不抓取**（交给调度器，`next_fetch_at = now`）。
  - 分组：第一层带子项的 outline 当作分组（按名称匹配已有分组，否则新建，受 30 上限）；更深层级并入其第一层分组。
  - 计数：`{ "groups": {"create", "reuse"}, "sources": {"create", "skip_existing", "skip_invalid", "skip_limit"} }`
    与警告列表；`dry_run=true` 只返回计数不写库；`false` 单事务写入，受 `MAX_FEEDS_PER_ACCOUNT`（100）约束，超出计入 `skip_limit`。
  - 重复执行幂等（已存在的来源计入 `skip_existing`）。

### 前端：来源管理页 `/feeds/manage`
- **R13** 页面结构（管理模式密度）：
  - 顶部工具条：“添加来源”（沿用现有添加流程）、“新建分组”、“导入 OPML”、“导出 OPML”。
  - 主体按分组分段（“未分组”在最后）；每段标题可就地改名、可删除（二次确认，说明来源会回到未分组）；
    分段可拖动排序；来源行可拖到其他分段（移入分组）或段内拖动排序。拖动同时提供键盘方式：
    聚焦行后 `Alt+↑/↓` 段内移动、`Alt+Shift+↑/↓` 移到上 / 下一个分组。
  - 来源行：来源标识（Q51 的 `feedMarkSource`）、显示名、种类、健康徽标（`ok` 不显示，`failing` 黄色、`broken` 红色、
    `paused` 灰色，颜色只用令牌）、未读数 / 条目数、最近拉取时间；行操作：改名、间隔、保留期、暂停 / 恢复、立即刷新、删除（二次确认）。
  - 多选（复选框 + Shift 连选）后批量：移到分组、暂停、恢复、删除（二次确认）。
  - OPML 导入：选择文件 → 试算结果（计数 + 警告）→ “确认导入”主按钮 → 结果。
- **R14** 交互：移动 / 改名 / 暂停为乐观更新 + 5 秒撤销；删除来源与删除分组二次确认；加载 / 空 / 错误三态；
  空状态给出“添加第一个来源”和“导入 OPML”两个入口；390px 下分段与行纵向堆叠，行操作收进“更多”菜单。
- **R15** `/feeds` 现有“管理”页签改为跳转 `/feeds/manage` 的链接；账号菜单不加新入口。

## 3. 数据与迁移（必填）

`20261101_0029_feed_groups_and_board.py`：

- 新表 `feed_groups`：`id` PK、`user_id` FK users CASCADE、`name` String(40)、`name_key` String(40)（规范化后的去重键）、
  `position` Integer NOT NULL `server_default '0'`、`version` Integer NOT NULL `server_default '1'`、`created_at`、`updated_at`；
  `UniqueConstraint(user_id, name_key)`、`UniqueConstraint(user_id, id)`（供复合外键）。
- `feeds` 加列：`group_id` String(36) NULL、`custom_title` String(200) NULL、`position` Integer NOT NULL `server_default '0'`、
  `fetch_interval_minutes` Integer NULL、`retention_days` Integer NULL、`version` Integer NOT NULL `server_default '1'`。
  `group_id` 外键：SQLite 对 `ADD COLUMN` 只能加单列外键 `REFERENCES feed_groups(id) ON DELETE SET NULL`；
  账号一致性由服务层 `_owned_group` 保证（在代码注释说明为何不用复合外键）。CHECK 约束放在应用层校验
  （`ADD COLUMN` 不能加表级 CHECK），在报告中说明。
- `feed_items` 加列：`starred_at` DateTime NULL；索引 `ix_feed_items_user_starred (user_id, starred_at)`。
- 新表 `feed_boards`：`user_id` PK + FK users CASCADE、`version` Integer NOT NULL、`columns_json` Text NOT NULL、`updated_at`。
- 回填：现有来源 `position` 按 `created_at` 顺序写 0..n-1（迁移内分块）。
- 降级：删两张新表、删新增列与索引（`feeds` / `feed_items` 无 FTS 触发器，可直接 `drop_column`）。
- `DATABASE_SCHEMA_HEADS = {"20261101_0029"}`。

## 4. 接口契约（必填）

```jsonc
// GET /api/feeds  → items[*]（新增字段）
{ "id": "…", "kind": "rss", "title": "少数派", "custom_title": null, "display_title": "少数派",
  "group_id": "g1", "position": 2, "fetch_interval_minutes": 60, "retention_days": 30, "version": 3,
  "health": "ok", "consecutive_failures": 0, "last_error": null,
  "last_fetched_at": "…", "next_fetch_at": "…", "unread_count": 12, "item_count": 140 }
// GET /api/feeds/groups
{ "items": [ { "id": "g1", "name": "科技", "position": 0, "version": 1, "source_count": 8, "unread_count": 31 } ] }
// GET /api/feeds/items?group_id=g1&unread=true&limit=30
{ "items": [ { "…": "…", "starred": false, "read": false } ], "next_cursor": "…|null" }
// GET /api/feeds/board
{ "version": 0, "columns": [ { "id": "…", "kind": "picks", "target_id": null, "unread_only": false } ] }
```

错误码：`version_conflict`、`order_conflict`（409）；`invalid_cursor`、`invalid_column_target`、`invalid_opml`、
`group_limit_reached`、`interval_not_applicable`（422）；`not_found`（404）。全部中文固定文案。

前端契约：`lib/feeds-contract.ts` 增加 `FeedGroup`、`FeedBoard`、`FeedBoardColumn`（判别联合 `kind`）、`FeedHealth`、
`FeedItemPage`；旧响应缺新字段时取缺省（`customTitle: null`、`groupId: null`、`health: "ok"` 等）。
客户端 `lib/feeds-client.ts` 每个接口一个函数，错误抛 `FeedsApiError`。

## 5. 文件清单（必填）

后端：`feeds/service.py`（若超 800 行，把分组拆到 `feeds/groups.py`、条目查询拆到 `feeds/items.py`）、新 `feeds/groups.py`、
新 `feeds/board.py`、新 `feeds/health.py`、新 `feeds/opml.py`、`feeds/schemas.py`、`feeds/routes.py`、`feeds/scheduler.py`（如需）、
`db/models/feeds.py`、`db/models/__init__.py`、`db/database.py`、迁移；测试 `tests/feeds/test_groups.py`、`test_source_settings.py`、
`test_items_query.py`、`test_board.py`、`test_opml.py`、迁移测试。
前端：新 `app/(workspace)/feeds/manage/page.tsx`、新 `components/feeds/feeds-manage-workspace.tsx`、
新 `components/feeds/feeds-manage-group.tsx`、新 `components/feeds/feeds-manage-row.tsx`、新 `components/feeds/use-feeds-manage.tsx`、
新 `lib/feeds-manage.ts`（排序 / 移动 / 撤销的纯逻辑）、`lib/feeds-contract.ts`、`lib/feeds-client.ts`、
`components/feeds/feeds-workspace.tsx`（只改“管理”页签为链接、改用新列表参数）、`app/styles/feeds.css`
（超 1200 行则新建 `app/styles/feeds-manage.css` 并在 `globals.css` 引入）；测试 `tests/feeds-manage.test.ts`、
`tests/feeds-contract.test.ts`、`tests/feeds-client.test.ts`。

## 6. 测试清单（必填）

- T1 分组增删改排：上限 30、规范化去重（“AI”与“ａｉ ”冲突）、删除后来源 `group_id` 为空、跨账号 404。
- T2 来源设置：每个字段的合法 / 非法值；`group_id` 未出现 = 不改、显式 `null` = 移出；`agent` 以外才允许间隔（Q60 前用 `podcast`/`rss` 测）；
  版本冲突 409；移入他人分组 404。
- T3 排序：正好一致才成功；缺项 / 多项 / 他人来源 409。
- T4 调度用每源间隔：注入 `now`，间隔 15 分钟的来源在 15 分钟后到期，失败退避以该间隔为基数。
- T5 每源保留期：`retention_days=7` 的来源 8 天前条目成墓碑、6 天前保留；星标与已收藏条目不受影响；`_trim_feed` 同理。
- T6 列表筛选与游标：各筛选单独与组合；翻页不重不漏（同一时间戳多条）；改筛选后旧游标 422；墓碑与已忽略不出现。
- T7 批量已读：范围与 `before` 截止正确；返回 id 列表可用于撤销；不改 `version`；跨账号 id 被忽略且不报存在性。
- T8 新内容计数：各范围计数正确；超过 20 个范围 422。
- T9 看板：无记录返回默认布局且不写库；首次保存 `expected_version=0`；版本冲突 409；无效目标 422；12 栏上限；
  删除来源后读取自动去掉该栏。
- T10 OPML 导出：结构、分组嵌套、特殊字符转义、无 `agent` 来源。
- T11 OPML 导入：试算不写库；执行后计数一致；重复执行幂等；DOCTYPE / ENTITY 被拒；非 http(s) 跳过；
  超过 100 上限计入 `skip_limit`；深层嵌套并入第一层分组；超 2 MiB 413。
- T12 健康派生纯函数：四种状态边界。
- T13 迁移升级（含 `position` 回填）→ 降级 → 再升级。
- T14 前端：契约三类输入；客户端 URL / 方法 / 请求体 / 错误映射；`lib/feeds-manage.ts` 的移动、重排、撤销逻辑。

## 7. 验收要点

- 所有新查询带 `user_id`；`group_id` 写入前校验属于本账号。
- 已读 / 星标是使用信号，不改版本；改名 / 移组 / 设置改版本。
- 截图：`/feeds/manage` 有 3 个分组 + 未分组、含 `failing` / `broken` / `paused` 各一的来源；浅 / 深 1440、手机 390；
  OPML 试算结果。
