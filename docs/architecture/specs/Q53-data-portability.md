# Q53 · 数据导出与自有备份导入（PRD P0：FR-DA-001 / FR-DA-002）

- 基线：`claude/loving-maxwell-gfbcxo` 当前 HEAD；分支 `q53-work`
- 迁移：`20261023_0027`，`down_revision = "20261022_0024"`（导入暂存表）
- 新后端模块：`services/api/src/webhub/portability/`；新前端区域：`/settings/data`，类名前缀 `data-`
- 本条目**拥有** `site-header.tsx` 的修改权（只加一个账号菜单链接）与 `server.mjs` 上传代理的扩展
- 并行关系：与 Q51、Q54 并行；不得改 `feeds/`、`ingestion/`、`library/` 的业务逻辑（只调用其服务函数）

## 1. 背景（必填）

PRD P0 要求账号数据可迁移：能导出，也能导入 WebHub 自有备份并在执行前预览变更。当前只有
`sites.source = 'backup'` 的预留，没有任何导出 / 导入能力。

## 2. 功能要求（必填）

- **R1 导出**：`GET /api/portability/export` 返回当前账号的版本化 JSON 附件
  （`Content-Disposition: attachment; filename="webhub-backup-<username>-<YYYYMMDD>.json"`），流式生成，
  内容见 §4.1。需要会话 Cookie；GET 无需 Origin 校验。
- **R2 排除项**：绝不导出 Provider 配置与密钥、会话、Agent 令牌（含哈希）、审计日志、书签原文件与暂存数据、
  媒体缓存、站内对话的推理内容与工具原始输出。
- **R3 上传预览**：`POST /api/portability/imports`（请求体即备份 JSON，`Content-Type: application/json`，上限 64 MiB）
  → 严格校验 → 生成预览并持久化暂存（§3）→ 返回 §4.2 的预览。不修改任何业务数据。
- **R4 确认导入**：`POST /api/portability/imports/{id}/apply`，请求体 `{ "expected_sha256": "<预览返回的值>" }`
  → 单事务按 §5 合并规则写入 → 返回结果统计。重复调用返回首次结果（幂等），不重复写入。
- **R5 预览过期**：暂存 24 小时后过期（`status = expired`，文件删除）；过期或他人预览一律 404 / 409。
- **R6 前端页面** `/settings/data`：
  - “导出”卡：说明包含 / 不包含的内容，主按钮“下载备份”（`<a href download>` 指向导出接口）。
  - “导入”卡：选择或拖放 `.json` → 上传 → 预览表（各类新增 / 合并 / 跳过数量、警告列表、每类前 20 条示例）
    → “确认导入”主按钮（二次确认对话框）→ 结果统计与“去收藏库查看”链接。
  - 加载 / 空 / 错误三态；上传进度（`XMLHttpRequest` 或 fetch + 状态文案即可）。
- **R7 入口**：`site-header.tsx` 账号菜单在“导入书签”后加“数据导出与恢复”→ `/settings/data`（`lucide` `DatabaseBackup` 或 `HardDriveDownload`）。
- **R8 大上传通道**：在 `apps/web/server.mjs` 把 `POST /api/backend/portability/imports` 纳入现有流式上传代理
  （与 `bookmark-imports` 同一机制，独立上限 64 MiB），避免 Next 改写层缓冲大请求体。

## 3. 数据与迁移（必填）

`20261023_0027_portability_imports.py` 新表 `portability_imports`：

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id` | String(36) PK | |
| `user_id` | FK users | 账号作用域 |
| `status` | String(16) | `previewed` / `applied` / `expired`（CHECK 约束） |
| `payload_sha256` | String(64) | 上传内容摘要 |
| `payload_path` | Text | 数据目录下 `portability-imports/<user_id>/<id>.json` |
| `summary_json` | Text | 预览统计（§4.2） |
| `result_json` | Text NULL | 应用结果（幂等重放用） |
| `created_at` / `expires_at` / `applied_at` | DateTime | |

索引 `(user_id, created_at)`。同步 `DATABASE_SCHEMA_HEADS = {"20261023_0027"}`。

## 4. 接口契约（必填）

### 4.1 备份格式 v1

```jsonc
{
  "format": "webhub-backup", "version": 1, "exported_at": "2026-10-23T08:00:00Z",
  "account": { "username": "designer", "display_name": "…" },          // 仅供展示，导入时忽略
  "categories": [ { "key": "c1", "name": "AI 与 Agent", "icon": "Bot", "is_default": false } ],
  "tags": [ { "key": "t1", "name": "Agent" } ],
  "sites": [ {
    "key": "s1", "url": "https://github.com/browser-use/browser-use",   // original_url
    "name": "browser-use", "summary": "…", "description": "…",
    "kind": "repo", "kind_source": "auto", "category_key": "c1", "tag_keys": ["t1"],
    "pinned": false, "favicon_url": "…|null", "preview_url": "…|null",
    "meta": { },                                                       // meta_json 原样（去掉 github.etag）
    "created_at": "…", "open_count": 0, "last_opened_at": null, "seen_at": null,
    "starred_at": null, "resurface_muted": false
  } ],
  "spaces": [ { "key": "p1", "name": "设计工具", "site_keys": ["s1", "s9"] } ],   // 顺序即成员顺序
  "feeds": [ { "url": "https://…/atom.xml", "title": "…", "kind": "rss", "enabled": true } ],
  "conversations": [ { "title": "…", "created_at": "…",
                       "messages": [ { "role": "user|assistant", "text": "…", "created_at": "…" } ] } ]
}
```
`key` 是文件内引用标识（导出时可直接用数据库 id），导入时**只用于文件内关联**，不当作数据库 id。
`conversations` 只导出、v1 导入时跳过（预览里说明）。

### 4.2 预览响应

```jsonc
{ "id": "…", "status": "previewed", "expires_at": "…", "payload_sha256": "…",
  "counts": {
    "categories": { "create": 2, "reuse": 5 }, "tags": { "create": 10, "reuse": 30 },
    "sites": { "create": 120, "merge": 18, "skip_invalid": 1 },
    "spaces": { "create": 1, "merge": 2 }, "feeds": { "create": 3, "skip_existing": 1 },
    "conversations": { "skipped": 4 } },
  "warnings": [ { "code": "invalid_url", "message": "第 37 条收藏的网址无效，已跳过" } ],
  "samples": { "sites_create": [ { "name": "…", "url": "…" } ], "sites_merge": [ … ] } }
```

### 4.3 应用结果
`{ "id": "…", "status": "applied", "applied_at": "…", "created": { "sites": 120, … }, "merged": { "sites": 18, … } }`

### 4.4 错误码
`payload_too_large`（413）、`invalid_format`（422，含首个错误路径）、`unsupported_version`（422）、
`limits_exceeded`（422：sites > 200,000、tags > 20,000、单字段超长）、`preview_expired`（409）、
`checksum_mismatch`（409）、`not_found`（404）。

## 5. 合并规则（必填）

- 校验用 Pydantic v1 备份模型（`extra="forbid"`，字段长度与 DB 约束一致）；`key` 引用必须可解析，否则 `invalid_format`。
- 分类：按规范化名称匹配已有分类 → 复用；否则新建（`icon` 只接受 Q52 的图标白名单，非法退回自动推断）。
  `is_default` 不导入（目标账号已有默认分类）。
- 标签：按收藏库现有规范化规则（NFKC、空白折叠、大小写无关）匹配 → 复用或新建。
- 收藏：用**当前代码**的规范化函数从 `url` 重新算 `identity_url`（不信任文件里的值）。
  - 新建：`source = "backup"`，字段照搬；`analysis_status = "complete"` 若 summary 或 description 非空，否则 `not_analyzed`；
    `kind` / `kind_source` 照搬（非法值退回推断）；不排队分析（由现有扫尾按 `not_analyzed` 处理）。
  - 已存在：**只补空字段**（summary、description、favicon_url、preview_url、meta 中缺失的键）；标签取并集；
    使用信号取“更有信息量”的值（`open_count` 取最大、`last_opened_at` 取较晚、`seen_at` / `starred_at` 取较早的非空）；
    名称、分类、置顶、`version` 不改。
  - 无效 URL：跳过并记警告。私网 / 本机 http(s) 地址照常保存（与书签导入规则一致，永不抓取）。
- Space：按名称匹配 → 追加缺失成员（保持文件顺序，追加在末尾）；否则新建。
- 订阅：按 `feeds/service.normalize_feed_url` 匹配 → 已存在跳过；否则新建（`next_fetch_at = now` 交给调度器）。
- 全部在一个事务内完成；任何异常整体回滚，暂存状态保持 `previewed`。
- 应用前比较 `expected_sha256` 与暂存摘要；应用成功写 `result_json` 并删除 payload 文件。

## 6. 文件清单（必填）

后端：新 `portability/__init__.py`、`schemas.py`（备份模型 + 请求响应）、`export.py`（流式导出）、`importer.py`
（校验、预览、应用）、`routes.py`；`routes.py`（根路由注册）；`db/models/portability.py` + `db/models/__init__.py`；
`db/database.py`；迁移；`tests/portability/test_export.py`、`test_import.py`、迁移测试。
前端：`app/(workspace)/settings/data/page.tsx`、`components/settings/data-portability-workspace.tsx`
（+ 需要时 `use-data-portability.tsx`）、`lib/portability-contract.ts`、`lib/portability-client.ts`、
`app/styles/data.css`（在 `globals.css` 引入）、`components/site-header.tsx`（一行入口）、`server.mjs`、
`tests/portability-*.test.ts`、`tests/server.test.mjs`（上传代理新路径）。
文档：`README.md`“本地数据与隐私”一节补“导出与恢复”说明。

## 7. 测试清单（必填）

- T1 导出结构：各段字段齐全；排除项（密钥、令牌、会话、审计、书签原文件）断言不存在。
- T2 往返：账号 A 导出 → 新账号 B 导入 → 收藏、分类、标签、Space 成员顺序、订阅等价（忽略 id 与时间戳差异）。
- T3 合并：已存在收藏只补空字段、标签并集、名称 / 分类 / version 不变；使用信号取值规则。
- T4 identity 重算：文件里伪造的 `identity_url` 被忽略。
- T5 校验：缺字段、多字段、错版本、坏 `key` 引用、超限各返回对应错误码；不产生任何写入。
- T6 幂等：重复 apply 返回同一结果，数据库行数不变；`expected_sha256` 不符 409。
- T7 过期：24 小时后 apply 返回 409，payload 文件被删除。
- T8 账号隔离：B 不能读取 / 应用 A 的预览（404）；导出只含本账号数据。
- T9 Origin：上传与应用需要可信 Origin（403 用例）。
- T10 迁移升降级。
- T11 前端契约与客户端（含错误码映射）；上传代理识别新路径并执行 64 MiB 上限。

## 8. 验收要点

- 用真实种子数据走一遍导出 → 新账号导入，截图预览与结果页（浅 / 深 1440、手机 390）。
- 导出文件人工检查：无任何 `secret`、`token`、`key`（Provider）字样。
