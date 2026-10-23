# WebHub 系统架构（v2026-10-23）

> 定位：**个人收藏宝藏库**。链接进来 → AI 整理 → 间隔重现 → 被人与 Agent 使用。
> 决策来源：`ITERATION_QUEUE.md` 的“2026-10 方向重定”（D1–D7）。本文描述“现在的系统长什么样、
> 新代码必须放在哪里、哪些规则任何时候都不能破”。

## 1. 运行拓扑

```
浏览器 / 浏览器扩展 ──HTTPS(私网)──▶ Next.js 自定义服务器 apps/web/server.mjs  (0.0.0.0:3100)
                                        │  页面渲染（React 19 / Next 16）
                                        │  /api/backend/*  ──rewrite──▶  FastAPI  (127.0.0.1:8100，单 worker)
                                        │  /api/backend/bookmark-imports（流式上传专用代理，见 server.mjs）
外部 Agent（Hermes / grokbot）──Bearer──▶ /api/backend/mcp ─────────────▶ FastAPI /api/mcp（MCP，无状态 JSON）
                                                                          │
                                                                 SQLite（WAL）+ 数据目录 WEBHUB_DATA_DIR
                                                                 （main.sqlite3、provider-master.key、
                                                                   media-cache/、bookmark-imports/ …）
```

- FastAPI **只监听回环**。所有浏览器流量经 Next 同源代理；Origin 校验只信任来自回环的转发头。
- 部署形态：单容器同时跑两个进程（`deploy/entrypoint.sh`），Compose 只发布 `127.0.0.1:3100`，
  经 `tailscale serve` 提供私网 HTTPS。**不面向公网**。
- 外部出站（抓网页、GitHub、RSS、图片代理、模型与搜索 Provider）一律走安全出站层（§5.3）。

## 2. 后端模块地图（`services/api/src/webhub/`）

| 模块 | 职责 | 对外接口 | 关键文件 |
| --- | --- | --- | --- |
| `auth` | 账号、会话 Cookie、可信 Origin、登录限流 | `/api/auth/*`；依赖 `require_current_identity`、`require_trusted_origin` | `dependencies.py`、`security.py` |
| `library` | 收藏（sites）、分类、标签、类型推断、使用信号、重温、概览、相似排查、重分类、标签合并、来源台账读取 | `/api/library/*`、`/api/discover/*` | `routes.py`、`engagement_routes.py`、`service/`（包）、`kinds.py`、`schemas.py` |
| `ingestion` | 网页抓取、元数据、LLM 资料补全、GitHub 富化、正文快照、分析队列与批量补全任务 | 内部服务 + `schedule_analysis` | `fetcher.py`（安全出站）、`worker.py`、`service.py`、`backfill.py`、`github.py`、`content.py` |
| `search` | FTS 关键词 + 可选向量召回与 RRF 融合、语义索引回填 | `/api/search/*` | `service.py`、`fusion.py`、`vectors.py` |
| `spaces` | Space 与成员、批量回执 | `/api/spaces/*` | `service.py` |
| `bookmarks` | 书签 HTML 导入：解析、暂存、分类、相似整理、应用、来源台账写入 | `/api/bookmark-imports/*` | `apply.py`、`source_ledger.py`、`persistence/` |
| `feeds` | RSS / 播客订阅、拉取调度、每日精选 | `/api/feeds/*` | `service.py`、`fetch.py`、`parser.py`、`scheduler.py` |
| `media` | 账号鉴权的图片代理与磁盘缓存 | `/api/media/proxy` | `media/` |
| `agent_access` | 外部 Agent 令牌、审计日志、MCP 服务与工具 | `/api/agent-tokens`、`/api/agent-activity`、`/api/mcp` | `tokens.py`、`mcp_server.py`、`tools.py` |
| `providers` | 账号级模型 / 搜索 / Embedding / 代码托管 Provider，密钥加密，出站目标校验 | `/api/providers/*` | `service.py`、`registry.py`、`targets.py`、`security.py` |
| `agent`、`chat`、`streaming` | 站内对话（**冻结**：只修缺陷） | `/api/chat/*`、`/api/agent/*` | — |
| `db` | 模型、会话、迁移前置检查、锁 | — | `models/`、`database.py`（`DATABASE_SCHEMA_HEADS`）、`locking.py` |

新增后端能力时，先判断它属于哪个现有模块；**只有职责确实不属于任何现有模块时才新建模块**
（例如 Q53 的 `portability`）。

## 3. 前端结构（`apps/web/`）

| 位置 | 放什么 | 规则 |
| --- | --- | --- |
| `app/(workspace)/<route>/page.tsx` | 路由入口 | 只设置 `metadata` 并渲染一个 `<Area>Workspace`/`<Area>Page` 组件，不写逻辑 |
| `components/<area>/` | 该区域的 UI 组件与 `use-<area>-*.tsx` 状态 hook | 单文件 ≤700 行；状态逻辑放 hook，展示放组件 |
| `lib/<area>-contract.ts` | 类型 + `normalize*` 解析（基于 `contract-guards.ts`） | 对缺失的可选字段容错，对格式错误严格报错 |
| `lib/<area>-client.ts` | `/api/backend/...` 请求封装与 `<Area>ApiError` | 只做 HTTP 与错误映射，不含 UI 逻辑 |
| `lib/<topic>.ts` | 纯逻辑（排序、格式化、reducer、模型推导） | 无 React、无 DOM，可被 Node 测试直接导入 |
| `app/styles/<area>.css` | 区域样式，经 `globals.css` 引入 | 只用设计令牌；类名带区域前缀 |
| `tests/` | Node 测试（`.ts` 逻辑 / `.mjs` 渲染源断言） | 见 `CODING_STANDARDS.md` §7 |

导航：顶栏 `components/site-header.tsx`（发现 · 收藏库 · 订阅 · Space；账号菜单放设置类入口）。
**同一时间只允许一个执行条目修改 `site-header.tsx`**，spec 会写明归属。

## 4. 数据模型要点

- **一切业务表带 `user_id`**，外键按 `(user_id, id)` 组合指向同账号记录（`*_same_account` 约束）。
- `sites`：收藏本体。关键列——`identity_url`（账号内唯一，保守规范化）、`kind`/`kind_source`、
  使用信号（`open_count`、`last_opened_at`、`seen_at`、`starred_at`、`resurface_*`）、`meta_json`（按类型的附加数据，
  如 `github`）、`version`（乐观锁，只因用户编辑而增加）、`analysis_status/phase`。
- `site_contents` + FTS `site_content_search`：每站最新一份正文 / README 快照。
- `site_import_origins` + `bookmark_source_*`：书签来源台账（Q55）。
- `feeds` / `feed_items`：订阅源与条目（只展示，不入收藏库；收进收藏库时建 `sites` 行并回填 `saved_site_id`）。
- `agent_tokens`（只存 SHA-256）/ `agent_audit_log`（只记写操作）。
- `provider_configs`：每账号每类最多一个启用项；密钥 AES-256-GCM，AAD 绑定账号/配置/类型/厂商。
- 迁移链当前 head：`20261022_0024`（以 `db/database.py` 的 `DATABASE_SCHEMA_HEADS` 为准，启动时校验）。

## 5. 横切机制

### 5.1 鉴权两条路

| 调用方 | 方式 | 依赖 | 写请求额外要求 |
| --- | --- | --- | --- |
| 浏览器 | HttpOnly 会话 Cookie | `CurrentIdentityDependency` | `WriteOriginDependency`（可信 Origin） |
| 外部 Agent / 扩展 | `Authorization: Bearer whk_…` | MCP 路由的 ASGI 鉴权包装 | 令牌需 `write` 权限；只开放可逆增量操作；写入记审计 |

Bearer 路由**不读 Cookie**；Cookie 路由**不接受 Bearer**。

### 5.2 错误模型

- 每个后端模块有一个领域异常基类（如 `LibraryError(code, status_code, message)`），服务层只抛领域异常。
- 路由层用模块内的 `_call()` 把领域异常转成 `HTTPException(detail={"code","message"})`。
- `message` 是给用户看的中文；**绝不透出厂商 / 上游原文**。前端按 `code` 分支，按 `message` 展示。

### 5.3 安全出站

所有对外 HTTP 必须经过 `ingestion/fetcher.py` 提供的固定连接通道（逐跳 SSRF 校验、DNS 固定、保留 Host/SNI、
Fake-IP 处理、禁止环境代理和自动跳转、大小与时间上限）。新代码**禁止**直接 `httpx.AsyncClient().get(外部URL)`。
Provider 地址另有 `providers/targets.py` 校验。

### 5.4 后台任务

| 子系统 | 位置 | 持久性 |
| --- | --- | --- |
| 单站分析队列 | `ingestion/worker.py` | 内存队列（Q54 改为持久意图） |
| 首页 / 收藏库批量补全 | `ingestion/backfill.py` + `site_metadata_backfill_*` 表 | 持久，重启恢复 |
| 订阅拉取 | `feeds/scheduler.py` | 状态在 `feeds` 表，定时轮询 |
| 语义索引回填 | `search/worker.py` | 可重建 |
| 图片缓存清理 | `media/` | 磁盘缓存，可丢弃 |

规则：**用户可感知的意图必须持久化**；内存队列只能是加速层，不能是唯一事实。所有后台并发有上限，
外部 Provider 失败有退避 / 熔断。

### 5.5 前端数据流

- 读：`lib/*-client.ts` 拉取 → `normalize*` 解析 → hook 状态 → 组件。请求带序号或 AbortController，丢弃过期响应。
- 写：可逆操作乐观更新 + 5 秒撤销（失败回滚并提示）；破坏性操作二次确认。
- 跨区域同步：`lib/library-events.ts` 事件总线（如快速收藏后刷新收藏库）；新增跨区域联动优先复用它。
- 图片：一律经 `lib/media-proxy.ts` 的 `proxiedImageUrl()`，失败退回生成封面 / 字母块。

## 6. 全局不变量（违反任一即验收不通过）

| 编号 | 不变量 |
| --- | --- |
| I1 | 每条 SQL 都带账号作用域；跨账号访问一律 404（不暴露存在性）。 |
| I2 | 不硬编码任何 LLM 供应商或密钥；模型访问只走账号级 Provider。密钥只回掩码 `********`。 |
| I3 | 上游 / 厂商异常原文不进响应、不进日志里的用户可见字段。 |
| I4 | 外部出站只走安全出站层（§5.3）。私网 / 本机地址可保存但永不抓取。 |
| I5 | 站内 Agent 写操作 propose → 人工确认；外部 Agent 只有可逆增量写并记审计，不开放删除。 |
| I6 | 使用信号（打开、星标、已看、推迟、静音、GitHub 富化、正文快照）**不改** `version` 与 `updated_at`。 |
| I7 | 迁移只追加；`sites` 表禁止 batch 重建；降级必须可用并有测试；`DATABASE_SCHEMA_HEADS` 同步。 |
| I8 | 前端只用 `globals.css` 设计令牌；圆角 ≤8px（圆点 / 头像除外）；图标只用 lucide-react 1.26.0（禁用 Loader2、AlertCircle、StopCircle）。 |
| I9 | 外部图片不由浏览器直连，统一走 `/api/media/proxy`；favicon 不用第三方 CDN。 |
| I10 | 用户可感知的后台意图持久化（§5.4）；内存结构不是唯一事实来源。 |
| I11 | 请求模型 `extra="forbid"`；响应用显式 Pydantic 模型。 |
| I12 | 测试基线只涨不降；新增行为必须有测试。 |

## 7. 扩展指南（照做即可）

### 7.1 新增一个后端接口
1. 在所属模块 `schemas.py` 加 `StrictRequest` 请求模型与响应模型。
2. 在 `service`（或 `service/` 包内对应文件）加业务函数：签名 `async def verb_noun(session, user_id, ..., *, now=None)`，
   只抛领域异常。
3. 在 `routes.py` 加路由：注入 `CurrentIdentityDependency`、`DatabaseSessionDependency`，写操作加 `WriteOriginDependency`，
   用 `_call()` 包装服务调用。
4. 测试：成功、422、401、403（Origin）、跨账号 404，以及业务分支。
5. 前端契约与客户端同步（§7.3）。

### 7.2 新增一张表 / 一列
按 `CODING_STANDARDS.md` §6 写迁移与模型，更新 `DATABASE_SCHEMA_HEADS`，补迁移升降级测试。

### 7.3 新增一个前端区域
`app/(workspace)/<route>/page.tsx` → `components/<area>/<area>-workspace.tsx` + `use-<area>-workspace.tsx`
→ `lib/<area>-contract.ts` + `lib/<area>-client.ts` → `app/styles/<area>.css`（在 `globals.css` 引入）→ 测试。
导航入口只能由 spec 指定的条目改 `site-header.tsx`。

### 7.4 新增一个 MCP 工具
在 `agent_access/tools.py` 实现（复用服务层，不复制业务逻辑），在 `mcp_server.py` 注册；描述用英文写给 LLM；
读工具无需 `write`；写工具检查 `write`、只做可逆增量操作、追加审计；结果保持紧凑。
