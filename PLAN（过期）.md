# WebHub 实施计划

> 文档版本：v1.0
> 更新日期：2026-07-25
> 需求基线：`PRD.md v0.7 Draft`
> 产品形态：局域网部署的跨平台 Web 网站系统
> 当前阶段：Phase 0 - Foundation

## 1. 计划目的

本文档把 PRD 转换为可执行、可测试、可逐步交付的工程计划。所有范围与验收均以 `PRD.md` 为唯一产品依据，不继承此前的实施设想。

WebHub 是运行在浏览器中的网站系统，不是 Electron、Tauri、PWA 原生壳或桌面客户端。“桌面端优先”表示 MVP 优先完成桌面浏览器网页布局和键鼠体验；Windows、macOS、Linux 用户均通过 URL 访问同一个 WebHub 实例。移动浏览器响应式适配属于 P1。

## 2. 实施原则

1. 纵向切片优先：每个阶段都形成可演示、可测试的闭环，不长期堆积不可运行的分层代码。
2. 手动能力优先：LLM、Embedding 或外部搜索未配置时，账号、关键词搜索和资料库管理仍可完整使用。
3. 后端统一鉴权：浏览器状态、隐藏按钮和 Agent 指令都不能替代服务端账号范围校验。
4. 主数据与派生数据分离：SQLite 是业务事实源，全文索引和向量索引必须可以重建。
5. 人工确认先于副作用：Agent 只能生成白名单候选动作，确认前不得写入业务数据。
6. 网站行为遵循 Web 平台：页面具有稳定 URL，可刷新、可深链；批量开页、Cookie、滚动和浏览器安全限制必须按真实浏览器能力处理。
7. 设计可替换：Foundation 只建立稳定布局、组件状态和 Design Token，视觉稿确认后再精修，不把临时样式固化成架构。

## 3. 技术基线

### 3.1 前端网站

- Node.js `24.x LTS`，项目约束 `>=24 <25`
- pnpm `11.1.3`
- Next.js `16.2.11` App Router
- React / React DOM `19.2.8`
- TypeScript `5.9.3`
- Tailwind CSS `4.3.3`
- Vercel AI SDK `7.0.37`
- `@ai-sdk/react` `4.0.40`

Next.js 是唯一向局域网公开的 Web 入口。网站监听可配置地址，开发期默认 `0.0.0.0:3000`；页面和 API 使用同源路径，浏览器不直接访问 Python 端口。

### 3.2 Python 服务

- Python `3.13.x`，项目约束 `>=3.13 <3.14`
- uv 管理解释器、虚拟环境和锁文件
- FastAPI `0.139.2`（Foundation 保守基线）
- SQLAlchemy 2 + Alembic（Phase 1 引入）
- LangChain `1.3.14`、LangGraph `1.2.9`（Phase 7 引入）
- `llama-index-core` `0.14.23` 与明确集成包（Phase 6 引入）

FastAPI 是唯一业务后端，负责认证、授权、事务、抓取、Provider、会话、任务、RAG 与 Agent。Windows MVP 固定单 API worker，默认仅监听 `127.0.0.1:8000`。

### 3.3 存储

- `main.sqlite3`：账号、网站、组织关系、会话、配置和任务的唯一业务事实源。
- SQLite FTS5：始终可用的关键词检索基线。
- Qdrant Local：可重建语义向量索引，仅由单个 API 进程访问。
- `checkpoints.sqlite3`：LangGraph 执行恢复状态，不作为用户可见会话的事实源。
- `.data/`：默认运行数据目录，必须 Git 忽略，并可通过 `WEBHUB_DATA_DIR` 改写。

## 4. 服务与目录边界

```text
Web browser
    |
    | http://<windows-host>:3000
    v
Next.js website
    |-- pages / routes / theme / UI state
    |-- /api/backend/* same-origin rewrite
    v
FastAPI (127.0.0.1:8000)
    |-- auth / business transactions / jobs / Agent
    |-- SQLite / FTS5 / Qdrant local / provider APIs
```

```text
WebHub/
  apps/
    web/
      app/                 # 网站页面、布局与 Route Handlers
      components/          # 无业务请求的基础组件
      features/            # auth/chat/library/ingest/spaces/settings
      lib/                 # API client、AI transport、theme、commands
      public/brands/       # 本地厂商图标及来源清单
      tests/
  services/
    api/
      src/webhub/
        api/
        auth/
        db/
        domain/
        repositories/
        providers/
        fetcher/
        jobs/
        rag/
        agent/
        streaming/
        security/
      alembic/
      tests/
      pyproject.toml
  packages/
    contracts/             # OpenAPI 快照、生成的 TS 类型、SSE 合同样例
  scripts/                 # Windows 初始化、启动、备份和密码重置
  .data/                   # 本地持久数据，不提交 Git
```

调试脚本、测试临时输出、依赖缓存和日志统一放在 `F:\AI\AgentMake\temp\WebHub`，不散落在仓库中。

## 5. 核心工程决策

### 5.1 网站路由与同源边界

- 网站至少保留 `/login`、`/register`、`/chat/[id]`、`/library`、`/spaces/[id]`、`/settings` 等稳定路由。
- 页面刷新或直接打开深层 URL 后，必须能由服务端恢复账号和页面状态。
- Next.js 通过 rewrite 将 `/api/backend/*` 转发到 FastAPI，并保留流式响应。
- Next 的路由保护只改善导航体验，所有真实权限判断都由 FastAPI 完成。
- MVP 不依赖浏览器扩展、桌面系统 API 或原生客户端能力。

### 5.2 账号隔离

- 所有用户资源表必须包含 `user_id`，仓储方法必须显式接收当前账号范围。
- 关联关系使用带 `user_id` 的约束，数据库层阻止跨账号关联。
- 会话使用随机 opaque token；数据库仅保存 token hash。
- Cookie 使用 HttpOnly、SameSite=Lax、host-only；HTTPS 部署时启用 Secure。
- 密码使用 Argon2id；修改或本机重置密码可撤销该账号全部会话。
- 跨账号 ID 猜测、参数篡改、确认令牌重放必须作为自动化测试。

### 5.3 Provider 凭据

- API Key 使用服务端主密钥和 AES-256-GCM 按记录加密，AAD 绑定账号和配置。
- API 只返回掩码，完整 Key 不进入前端状态、日志、聊天记录或普通数据导出。
- 连接测试先使用候选配置；失败不得覆盖上一次可用配置或自动启用。
- 模型、搜索和 Embedding 配置均属于账号。

### 5.4 Agent 与确认

- LLM 工具不能直接持有数据库连接，只能返回结构化候选动作。
- 新增和修改生成可编辑预览；删除和批量动作需要影响预览与二次确认。
- 确认凭证绑定账号、会话、动作、payload hash、资源版本和过期时间，只可使用一次。
- LangGraph 的副作用位于 `interrupt()` 后的独立幂等节点，避免恢复时重复执行。
- 历史消息保留展示数据，但已执行、过期或资源版本失效的动作不可再次执行。

### 5.5 检索与 RAG

- 先进行 SQLite FTS5 精确/关键词检索，再进行账号限定的向量召回。
- 语义索引使用确定性节点 ID，并在 metadata 中同时保存 `user_id` 与 `site_id`。
- MVP 默认可连接宿主机 Ollama `bge-m3`；不可用时明确降级到 FTS5。
- 本地结果不足且用户允许联网时，才调用当前账号启用的 Tavily、Jina 或 Exa。
- 向量库不是主数据；索引损坏、模型切换或版本升级时可从 SQLite 重建。

### 5.6 抓取安全

- 仅允许 HTTP/HTTPS；每次 DNS 解析和重定向都拒绝 loopback、private、link-local、reserved 与云元数据地址。
- 限制重定向次数、响应大小、内容类型、并发和超时。
- 外部页面正文以不可信数据进入模型，不能覆盖系统指令或触发工具。
- 批量收录使用 SQLite 持久任务与单进程异步 worker，不在 MVP 引入 Redis/Celery。

## 6. 分阶段实施

### Phase 0 - Foundation（当前）

交付：

- pnpm workspace、Next.js 网站、uv/FastAPI 服务和锁文件
- 桌面浏览器网站骨架、全站 Header、Agent 页内会话历史、浅色/深色 Token 和 Agent 空状态
- Next 到 FastAPI 的同源 health/readiness 联调
- 独立 lint、typecheck、test、build 命令
- 环境变量样例、Windows 开发启动入口和基础 README

完成标准：

- 一条命令可启动 Web 与 API。
- 局域网只需访问网站 URL；FastAPI 默认不向局域网公开。
- 网站刷新后结构稳定，API 不可用时显示明确状态。
- 前后端自动检查通过。

### Phase 1 - Identity & Data Kernel

交付：

- Alembic 与 SQLite WAL 配置
- users、sessions、preferences、provider_configs 基础表
- 注册、登录、退出、改密、本机密码重置
- Argon2id、会话撤销、Origin/CSRF 校验和登录限流
- 登录/注册网页与受保护工作区
- 两账号资源隔离测试基座

完成标准：

- 两个账号只能访问自己的资源和设置。
- 猜 ID、修改参数、旧 Cookie 和跨账号确认凭证全部失败。
- 服务重启后合法会话和账号偏好仍能恢复。

### Phase 2 - Manual Library

交付：

- Site、Category、Tag、Space 及关系表和 CRUD
- 默认“未分类”、URL 规范化、同账号去重和置顶
- 资料库列表、详情/编辑侧栏、筛选、排序与 Space 成员排序
- SQLite FTS5 和短关键词 fallback
- 删除分类、标签、Space 的影响预览

完成标准：

- 不配置 LLM 也能完成网站资料的新增、查询、修改、删除和组织。
- 单账号 10,000 条网站关键词查询 P95 小于 1 秒。
- 删除 Space 不删除网站；同一网站可同时属于多个 Space。

### Phase 3 - Conversation & Stream Contract

交付：

- 多会话、消息、来源和结构化 artifact 持久化
- 今天、昨天、近 7 天、近 30 天和更早月份分组
- FastAPI 到 AI SDK v7 的 UI Message Stream 适配层
- `useChat`、停止生成、失败恢复和 typed data parts
- 可注册 Slash Command 框架，以及 `/搜索`、`/存入`
- 先用 deterministic fake agent 验证完整协议

完成标准：

- 刷新或在另一台设备登录后，可恢复并继续账号自己的会话。
- Python SSE fixture 可由前端 AI SDK 解析。
- abort、error、部分消息和过期 artifact 状态均可重放。

### Phase 4 - Provider Center

交付：

- OpenAI、DeepSeek、Qwen、Kimi、Ollama、自定义 OpenAI-compatible 模型适配
- Tavily、Jina、Exa 搜索适配
- Embedding 配置与宿主机 Ollama 默认建议
- 加密保存、掩码、启用状态和候选配置连接测试
- 本地打包的官方品牌图标及来源/授权清单

完成标准：

- Provider 配置完全跟随账号。
- 测试失败不覆盖旧配置、不切换启用项、不泄露 Key。
- Ollama 支持空 Key 和局域网 Base URL。

### Phase 5 - Ingestion Vertical Slice

交付：

- 单个/最多 50 个 URL 的校验、规范化、安全抓取和元数据提取
- Jina Reader 可选 fallback
- 可重启恢复的批任务、逐项状态、取消和重试
- LLM 结构化名称/描述/分类/标签建议
- 可编辑预览、一次性确认凭证和事务提交

完成标准：

- 重复、无效、超时和抓取失败不会阻塞有效条目。
- 确认前主数据无变化。
- 过期、重放、跨账号和资源版本陈旧的确认全部被拒绝。

### Phase 6 - Retrieval & RAG

交付：

- 网站 profile、LlamaIndex ingestion 和 Qdrant Local
- FTS 与 dense retrieval 的融合排序
- 按账号分区、增量更新、重建、状态展示和 FTS 降级
- 收藏库不足时接入当前搜索 Provider
- 统一来源模型和可收录的外部结果

完成标准：

- 精确命中优先，语义检索不掩盖已知名称/URL。
- 两账号的向量检索无串库。
- 索引删除后可从 SQLite 完整重建。
- 无可靠来源时不生成虚构链接。

### Phase 7 - Agent & Human-in-the-Loop

交付：

- LangChain 模型/工具适配层
- LangGraph 意图路由、检索、候选动作、interrupt/resume
- 收录、修改、删除和 Space 管理结构化卡片
- 新增/修改确认、删除/批量二次确认和幂等执行

完成标准：

- PRD 中自然语言修改与删除场景全部通过。
- 未确认动作绝不写库。
- 图恢复、请求重试和重复点击不会造成重复执行。

### Phase 8 - Portability & Release Hardening

交付：

- 当前账号数据 ZIP 导入导出，不含密码、会话 token、API Key、checkpoint 和向量
- 实例备份/恢复脚本与主密钥单独备份说明
- Windows 局域网启动、地址展示和防火墙说明
- Chromium、Firefox、WebKit 桌面网页回归
- 任务恢复、故障降级、容量和安全测试
- 更新 `LearnProjects/00_Docs` 中的 Web 全栈 Agent 架构实战文档

完成标准：

- 备份恢复、任务重启恢复和 PRD 17 项验收场景全部通过。
- Chrome、Edge、Firefox、Safari/WebKit 关键网页流程无阻塞问题。

## 7. 测试策略

### 7.1 后端

- pytest + pytest-asyncio：API、仓储、事务、任务和 Agent 流程。
- Hypothesis：URL 规范化、账号隔离和确认凭证属性测试。
- 临时 SQLite 与真实 Qdrant Local integration。
- Provider 使用 mock transport；真实付费调用只做手工 smoke test。
- 必测 SSRF DNS/redirect、token replay/stale/cross-user、secret masking、任务重启。

### 7.2 前端网站

- ESLint、TypeScript 和 Next production build。
- Vitest + Testing Library：命令注册、日期分组、状态组件和表单。
- Playwright：1440x900、1280x800 桌面网页，Chromium/Firefox/WebKit。
- 视觉回归覆盖浅色/深色、长中文、空/加载/失败/部分成功/预览/已过期。
- 测试直接打开深层 URL、浏览器刷新、前进后退和会话失效跳转。

### 7.3 跨栈合同

- FastAPI OpenAPI 是 REST 合同源，生成 TypeScript 类型并检查漂移。
- Python 生成 SSE fixtures，Node 使用 AI SDK parser 验证 text/data/error/abort/DONE。
- 所有版本化 UI artifact 必须提供向后兼容读取或明确迁移。

### 7.4 性能

- 两账号各 10,000 条 Site 的隔离与 FTS P95。
- 50 URL 批任务的部分失败、取消与恢复。
- SQLite WAL 写冲突、单 worker 队列和向量重建。

## 8. 主要风险与应对

| 风险 | 应对 |
|---|---|
| P0 范围大 | 按 Phase 0-8 纵向交付，每阶段单独验收 |
| AI SDK v7 跨语言流协议变化 | 独立 codec、固定版本和 fixture 合同测试 |
| LangGraph checkpoint 与业务消息双存储 | main DB 作为 UI 事实源，以 run/message ID 对齐 |
| SQLite + Qdrant Local 单写者限制 | Windows MVP 固定单 API worker；扩容时迁移服务型存储 |
| Ollama / bge-m3 不可用 | 显示索引状态，始终保留 FTS 与手动管理 |
| 任意模型不支持 tool/structured output | 明确报错并回退手动流程，禁止猜测执行 |
| 局域网 HTTP 无传输加密 | 明示仅适用于受信任 LAN；公网或不可信网络必须先加 HTTPS |
| SSRF 与网页 Prompt Injection | 网络层地址校验、内容限额、不可信上下文和工具白名单 |
| 浏览器批量打开限制 | 用户手势触发、操作前提示、记录失败项并允许重试 |
| 厂商图标授权和暗色适配 | 只用官方/明确授权资源，本地打包并维护来源清单 |

## 9. 阶段门禁

每个 Phase 只有同时满足以下条件才可标记完成：

1. 该阶段完成标准有自动化或明确的手工验证证据。
2. 不新增跨账号访问路径或绕过确认的写入路径。
3. 文档、环境样例和启动方式与代码一致。
4. `pnpm lint`、`pnpm typecheck`、`pnpm test`、`pnpm build` 及后端测试通过。
5. 运行数据、密钥、日志、缓存和临时输出未进入 Git。

## 10. 当前迭代

当前只执行 Phase 0，目标是先得到稳定可运行的网站与 API 骨架。网页采用站点 Header、独立路由和正常纵向文档流，不建立桌面客户端式全高 Shell。设计 Agent 效果图回来后，先固化 Design Token 和关键组件，再进入 Phase 1 的账号闭环；视觉返工不得改变同源代理、鉴权边界或业务服务职责。
