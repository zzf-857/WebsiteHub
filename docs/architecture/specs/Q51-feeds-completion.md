# Q51 · 订阅流补齐：条目不复活、缩略图

- 基线：`claude/loving-maxwell-gfbcxo` 当前 HEAD（含 Q50、Q56）；分支 `q51-work`
- 迁移：`20261023_0025`，`down_revision = "20261022_0024"`
- 类名前缀：`feeds-`；可改文件仅限本 spec“文件清单”
- 并行关系：与 Q53、Q54 并行；**不得**改 `site-header.tsx`、`media/`、`library/`

## 1. 背景与根因（必填）

`feeds/service.py` 的 `_trim_feed`（每源保留上限）与 `cleanup_items`（30 天清理）都直接 `DELETE` 条目。
`_store_items` 用 `feed_item_guid_per_feed` 已知 guid 去重，行被删后 guid 不再“已知”，源里仍在的条目
（尤其无 `published_at` 的条目，不会被“早于保留期”过滤）下次拉取会重新插入——“复活”。
另外条目没有图片字段，`/feeds` 只能显示文字。

## 2. 功能要求（必填）

- **R1 墓碑代替删除**：`feed_items` 新增 `purged_at`（可空时间）。`_trim_feed` 与 `cleanup_items` 改为把目标行
  更新为墓碑：`purged_at = now`，清空 `title=""`、`summary=""`、`author=NULL`、`url=NULL`、`audio_url=NULL`、
  `image_url=NULL`、`duration_seconds=NULL`；保留 `guid`、`feed_id`、`user_id`、`fetched_at`、`published_at`。
  已收进收藏库（`saved_site_id` 非空）的条目永不成为墓碑。
- **R2 墓碑不可见、不计数**：所有列表、精选、计数、MCP `get_feed_digest` 查询加 `purged_at IS NULL`；
  `_trim_feed` 的保留集合只统计非墓碑行。
- **R3 墓碑最终回收**：`cleanup_items` 额外硬删除 `purged_at < now - 365 天` 的墓碑（一年后源里早已没有该条目）。
- **R4 拉取去重仍覆盖墓碑**：`_store_items` 的已知 guid 查询包含墓碑行（即不加 `purged_at` 过滤），保证不复活。
- **R5 条目图片**：`ParsedItem` 新增 `image_url: str | None`。解析优先级（取第一个通过 `safe_link` 且为 http(s) 的）：
  1. `media_thumbnail[0].url`
  2. `media_content` 中 `medium == "image"` 或 `type` 以 `image/` 开头的第一项 `url`
  3. 条目级 `itunes_image.href`（feedparser 字段 `image.href` 或 `itunes_image`）
  4. `enclosures` 中 `type` 以 `image/` 开头的第一项 `href`
  5. 条目 `content[0].value` 或 `summary` HTML 中第一个 `<img src>`（用标准库 `html.parser` 提取，不执行、不加载）
  长度 ≤ `MAX_URL_LENGTH`。
- **R6 订阅源图片**：`ParsedFeed` 新增 `image_url`（频道 `image.href` / `itunes_image`），`feeds` 表新增 `image_url`；
  每次成功拉取更新。
- **R7 API**：`FeedItemResponse` 增加 `image_url: str | None`；`FeedResponse` 增加 `image_url: str | None`。
- **R8 前端缩略图**：条目右侧显示缩略图（桌面 96×64，<600px 时 72×48，`object-fit: cover`，圆角 6px，
  `--line` 描边），`src = proxiedImageUrl(imageUrl)`，`loading="lazy"`、`referrerPolicy="no-referrer"`、`alt=""`；
  加载失败隐藏缩略图位（不留空框）。无图时不占位。
- **R9 来源标识优先级**：条目左侧来源标识改为：订阅源 `imageUrl`（经代理）→ 现有 favicon → 字母色块；
  播客保持耳机图标块，但若订阅源有封面图则用封面图。
- **R10 组件拆分**：把条目行抽成 `components/feeds/feed-item-row.tsx`（`feeds-workspace.tsx` 不再增长，目标 ≤700 行）。

## 3. 数据与迁移（必填）

`20261023_0025_feed_media_and_tombstones.py`：
- `feed_items` 加 `purged_at DATETIME NULL`、`image_url TEXT NULL`；`feeds` 加 `image_url TEXT NULL`。
- 索引：`ix_feed_items_user_purged`（`user_id`, `purged_at`）供清理与列表过滤。
- 降级：删索引、删三列（这两张表没有 FTS 触发器，可直接 `drop_column`）。
- 更新 `DATABASE_SCHEMA_HEADS = {"20261023_0025"}`。

## 4. 接口契约（必填）

```jsonc
// GET /api/feeds  → FeedListResponse.items[*]
{ "id": "…", "url": "…", "title": "…", "kind": "rss|podcast", "site_url": "…|null",
  "image_url": "https://…|null", /* 新增 */ "enabled": true, "item_count": 12, … }
// GET /api/feeds/items, /api/feeds/picks → 条目
{ "id": "…", "feed_id": "…", "title": "…", "summary": "…", "url": "…|null",
  "image_url": "https://…|null", /* 新增 */ "audio_url": "…|null", "duration_seconds": 3480, … }
```
墓碑永不出现在任何响应中。前端契约 `normalizeFeed` / `normalizeFeedItem` 增加 `imageUrl`（缺失 → `null`，非字符串 → 报错）。

## 5. 文件清单（必填）

后端：`feeds/parser.py`、`feeds/service.py`、`feeds/schemas.py`、`feeds/routes.py`（若响应构造在此）、
`db/models/feeds.py`、`db/database.py`、`alembic/versions/20261023_0025_feed_media_and_tombstones.py`、
`agent_access/tools.py`（仅 `get_feed_digest` 查询加过滤，如需）、测试 `tests/feeds/test_feeds.py`（或新文件
`tests/feeds/test_feed_tombstones.py`、`tests/feeds/test_feed_images.py`）、迁移测试。
前端：`lib/feeds-contract.ts`、`lib/feeds-mark.ts`、`components/feeds/feeds-workspace.tsx`、
新 `components/feeds/feed-item-row.tsx`、`app/styles/feeds.css`、`tests/feeds-*.test.*`。

## 6. 测试清单（必填）

- T1 无日期条目：拉取 → 清理（30 天后）→ 再拉取同一文档 → 条目不出现在列表、`feed_items` 只有一行（墓碑）。
- T2 超过每源上限被裁剪的条目再次出现在源里时不复活。
- T3 已收进收藏库的条目不被清理也不被裁剪为墓碑。
- T4 墓碑一年后被硬删除；未满一年保留。
- T5 列表、精选、计数、MCP digest 均不含墓碑。
- T6 图片解析五种来源各一例 + 优先级（同时存在时取更高优先级）+ 非 http(s) / 超长 / 带凭据被拒。
- T7 订阅源 `image_url` 在拉取后更新。
- T8 迁移升级 → 降级 → 再升级。
- T9 前端契约：含 / 缺 / 错误类型的 `image_url`。
- T10 `feeds-mark` 优先级：订阅源图片 > favicon > 字母块；播客规则。
- T11 渲染断言：缩略图使用 `proxiedImageUrl`、`loading="lazy"`、`referrerPolicy="no-referrer"`，失败时隐藏。

## 7. 验收要点（总负责人检查）

- 墓碑行内容字段确实被清空（隐私：清理后不残留标题 / 摘要）。
- 所有读路径都有 `purged_at IS NULL`，只有去重查询没有。
- 截图：`/feeds` 浅 / 深 1440、手机 390，有图与无图条目混排时对齐整齐。
