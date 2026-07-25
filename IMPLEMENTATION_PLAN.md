# WebHub 实施计划

> 文档版本：v1.2
> 更新日期：2026-07-26
> 需求基线：`PRD.md v0.8 Draft`
> 产品形态：局域网部署的跨平台 Web 网站系统
> 当前阶段：Phase 1 进行中；后端账号认证、Alembic 数据内核、本机密码重置和网站端账号入口已验证，可复用的业务资源账号范围与拒绝测试基座仍待完成；书签 parser/dry-run/Skill 合同已验证，正式账号导入闭环未实现

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
8. 分开两类导入：普通收录保持单次最多 50 URL 的抓取/分析流程；浏览器书签使用可承载 100,000 occurrences 的独立持久化管线、数据模型和预览 API。

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

Next.js 是唯一向局域网公开的 Web 入口。网站监听可配置地址，开发期默认 `0.0.0.0:3100`；页面和 API 使用同源路径，浏览器不直接访问 Python 端口。开发与生产启动均经过仓库内置 Node 入口，在 Next 处理请求前用 socket 信息覆盖外部传入的 forwarding headers，并只接受启动时检测到的本机地址、机器名或 `WEBHUB_ALLOWED_HOSTS` 显式增加的 Host。

### 3.2 Python 服务

- Python `3.13.x`，项目约束 `>=3.13 <3.14`
- uv 管理解释器、虚拟环境和锁文件
- FastAPI `0.139.2`（Foundation 保守基线）
- SQLAlchemy 2 + Alembic（Phase 1 引入）
- LangChain `1.3.14`、LangGraph `1.2.9`（Phase 7 引入）
- `llama-index-core` `0.14.23` 与明确集成包（Phase 6 引入）

FastAPI 是唯一业务后端，负责认证、授权、事务、抓取、Provider、会话、任务、RAG 与 Agent。Windows MVP 固定单 API worker，默认仅监听 `127.0.0.1:8100`。

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
    | http://<windows-host>:3100
    v
Next.js website
    |-- pages / routes / theme / UI state
    |-- /api/backend/* same-origin rewrite
    v
FastAPI (127.0.0.1:8100)
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
        imports/           # 浏览器书签解析、暂存、分类和幂等提交
        jobs/
        rag/
        agent/
        skills/            # 网站内置 Agent Skill 定义与版本适配
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
- FastAPI 使用 `--no-proxy-headers` 监听 loopback；只有来自 loopback 网站入口的单跳原始 Host 和客户端 IP 可用于 Origin 校验与登录限流，非 loopback 或多值/畸形 forwarding headers 不受信任。
- 登录在执行 Argon2 前原子占用“客户端 + 账号”与“客户端总量”两级窗口，限制并发穿透和轮换随机用户名；桶状态有容量上限与过期清理。MVP 固定单 API worker，重启会清空限流窗口，多 worker 前必须迁移到共享限流存储。
- Next 的路由保护只改善导航体验，所有真实权限判断都由 FastAPI 完成。
- MVP 不依赖浏览器扩展、桌面系统 API 或原生客户端能力。

### 5.2 账号隔离

- 所有用户资源表必须包含 `user_id`，仓储方法必须显式接收当前账号范围。
- 关联关系使用带 `user_id` 的约束，数据库层阻止跨账号关联。
- 会话使用随机 opaque token；数据库仅保存 token hash。
- Cookie 使用 HttpOnly、SameSite=Lax、host-only；HTTPS 部署时启用 Secure。
- 密码使用 Argon2id；修改或本机重置密码可撤销该账号全部会话。
- 跨账号 ID 猜测、参数篡改、确认令牌重放必须作为自动化测试。
- 数据库 schema 只由 Alembic 演进。应用启动和 readiness 只校验当前 revision，不调用 `create_all()`、不自动 stamp，也不接管有表但无版本号的旧库。
- 本地开发通过显式 `webhub-db upgrade` 初始化或升级；相对 `WEBHUB_DATA_DIR` 始终以仓库根目录解析，避免从不同 cwd 启动时连接不同数据库。

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

### 5.7 大规模浏览器书签导入

- 浏览器书签是独立的纵向管线，不复用 Phase 5“单个/最多 50 URL”逐条抓取队列。入口接受 Chrome、Edge、Firefox、Safari 常见 Netscape Bookmark HTML，解析过程不执行脚本、不加载外部资源。
- 使用流式/增量 parser 写入账号隔离的 staging 表；API 只提供聚合统计、目录簇和游标/分页明细，禁止把全量条目加载进一个 HTTP 响应、React 状态或 Agent 上下文。
- 数据模型区分 `sites`、`bookmark_imports`、`bookmark_source_folders` 与 `bookmark_occurrences`。源目录树和每个 occurrence 是来源事实；多个 occurrences 可关联一个 Site，不因 URL 合并而丢失原始标题、路径或顺序。
- URL identity 只做保守语法规范化，query 与 fragment 的内容和顺序必须保留。相同 identity URL 只合并关联，不覆盖已有 Site 业务字段；新值只能作为候选出现在第二阶段预览中。
- parser 在入口忽略 `ICON`、`ICON_URI` 等内嵌 favicon 属性。非 HTTP(S)、浏览器内部协议和无效项进入 unsupported staging；HTTP(S) 私网/本机/保留地址可标记为“仅保存”，但绝不进入服务端 fetcher。
- 分类器按“源目录簇/用户映射/已有分类/域名规则 -> 置信度 -> 低置信度簇批量 LLM”执行。模型预算在分析前固化到任务快照；未配置、超预算或失败时降级到规则结果/未分类，不回滚已完成分片。
- 敏感 URL 检测在任何模型调用之前执行。凭据、token、session、签名等 query/fragment 和相关秘密属性不得出现在 Provider 请求、日志或错误追踪中。
- 两阶段确认分别签发账号绑定、payload hash 绑定、一次性的预检确认与提交确认。第二次确认之前不写业务表；提交以 chunk checkpoint 和幂等键恢复，重复点击、重试或进程重启不重复写入。
- 网站内置 `import-browser-bookmarks` Skill 只编排同一组后端工具和状态合同；Skill 版本随导入报告记录。Agent 无权自建 parser、直接写库或绕过预算、隐私过滤和确认。

## 6. 分阶段实施

### Phase 0 - Foundation（已完成）

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

补充现状（不扩大 Phase 0 的完成含义）：浏览器书签 parser/dry-run 纵向切片、`import-browser-bookmarks` Skill 合同与本地 CLI 已完成。用户提供的 2,541 occurrence mock 已得到确定性预览；8,109,840 bytes / 100,000 occurrence 合成文件的完整预检为 61.02 秒，额外 Python 分配峰值低于 1 MiB。以上只证明本地只读管线与 Skill 合同可行，不代表账号隔离、业务表、持久任务 API、真实 LLM 分类、两阶段写库确认或 Agent 运行时注册已经完成。

### Phase 1 - Identity & Data Kernel

当前进展（2026-07-26）：后端已实现注册、登录、退出、`/me`、主题偏好、改密、本机密码重置、Argon2id、opaque Cookie token hash、会话撤销、Origin 校验、按代理后真实客户端隔离的两级原子登录限流、SQLite WAL 和首个 Alembic migration；网站已接入登录、注册、路由守卫、退出和账号主题持久化。自动化测试覆盖迁移往返与漂移、未版本化库拒绝启动、畸形/伪造 Origin、Host allowlist、代理后客户端隔离、并发限流占位、跨用户名总桶、限流容量回收、跨重启会话/偏好恢复和双账号偏好隔离。后续业务资源的统一账号范围仓储与跨账号拒绝模板仍未完成，因此本阶段保持“进行中”；Provider 配置 API 仍按 Phase 4 实施，不作为 Phase 1 完成前置。

交付：

- Alembic 与 SQLite WAL 配置
- users、sessions、preferences、provider_configs 基础表
- 注册、登录、退出、改密、本机密码重置
- Argon2id、会话撤销、Origin/CSRF 校验和登录限流
- 登录/注册网页与受保护工作区
- 两账号资源隔离测试基座
- 为后续 `bookmark_imports`、staging、source folder、occurrence 和确认资源建立统一的账号范围仓储约束与跨账号拒绝测试模板

完成标准：

- 两个账号只能访问自己的资源和设置。
- 猜 ID、修改参数、旧 Cookie 和跨账号确认凭证全部失败。
- 服务重启后合法会话和账号偏好仍能恢复。

### Phase 2 - Manual Library

交付：

- Site、Category、Tag、Space 及关系表和 CRUD
- 默认“未分类”、保守 URL identity、同账号去重和置顶；query/fragment 不删除、不重排、不折叠
- `bookmark_imports`、`bookmark_source_folders`、`bookmark_occurrences` 与 staging 表迁移；所有表带 `user_id`，同 URL 合并 Site 时仍保留每个 occurrence
- 资料库列表、详情/编辑侧栏、筛选、排序与 Space 成员排序
- 面向 100,000 Site 的服务端游标分页与聚合查询，前端不得加载全量资料库
- SQLite FTS5 和短关键词 fallback
- 删除分类、标签、Space 的影响预览

完成标准：

- 不配置 LLM 也能完成网站资料的新增、查询、修改、删除和组织。
- 单账号 100,000 条网站关键词查询 P95 小于 1 秒，列表/筛选使用有界分页响应。
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
- 大规模书签任务能固化账号当前 Provider 与预算快照；只有经过隐私过滤的低置信度目录簇可调用模型。

### Phase 5 - Ingestion Vertical Slice

交付：

- 单个/最多 50 个 URL 的校验、规范化、安全抓取和元数据提取
- Jina Reader 可选 fallback
- 可重启恢复的批任务、逐项状态、取消和重试
- LLM 结构化名称/描述/分类/标签建议
- 可编辑预览、一次性确认凭证和事务提交
- 可供后续书签管线复用的持久任务状态、chunk checkpoint、进度/取消 API 和一次性确认基础设施

完成标准：

- 重复、无效、超时和抓取失败不会阻塞有效条目。
- 确认前主数据无变化。
- 过期、重放、跨账号和资源版本陈旧的确认全部被拒绝。

边界：本 Phase 仍只验收普通的单个/最多 50 URL 安全抓取流程。浏览器 HTML 不拆成十万次本流程调用，也不在这里逐条抓取、逐条调用 LLM。

### Phase 5B - Large-scale Browser Bookmark Import（独立纵向阶段）

依赖与交付节奏：

固定依赖映射为：账号隔离依赖 Phase 1，业务表依赖 Phase 2，受控 Provider API 依赖 Phase 4，目录簇分类与大规模任务编排依赖 Phase 5，最终确认与 Agent Skill 闭环依赖 Phase 7。

| 能力 | 前置阶段 | 状态/说明 |
|---|---|---|
| Netscape HTML parser dry-run、目录树/occurrence 解析和预检合同 | 无 | 已完成 2,541 occurrence mock 与 100,000 occurrence 合成基准；仍需提交脱敏 golden fixture，并在 Release 阶段复测硬件/数据集版本 |
| 账号范围、会话与跨账号拒绝 | Phase 1 | 未完成；所有任务、暂存和结果必须接入统一 `user_id` 边界 |
| Site identity、业务表、源目录和 occurrence 持久化 | Phase 2 | 未完成；不得用 parser 临时结构替代 Alembic 业务模型 |
| 可选 LLM 所需 Provider API、凭据与预算快照 | Phase 4 | 未完成；本阶段只提供受控 API 能力，本地分类不依赖 Provider |
| 目录簇分类编排、大规模导入任务 API、持久 worker、checkpoint、进度、取消和预览基础设施 | Phase 5 | 未完成；复用任务原语但使用独立队列策略和分页合同，不复用 50 URL 数据流 |
| 第二次确认提交、Agent 调用与内置 Skill | Phase 7 | Skill 合同和 dry-run 脚本已完成，运行时注册/账号工具/提交闭环未完成；Phase 7 前不得宣称 Agent Skill 闭环完成 |

Phase 5B 是跨依赖的独立纵向轨道：各里程碑随前置阶段落地，完整阶段门禁在 Phase 7 集成后关闭；它不改变 Phase 5 的 50 URL 验收边界，也不要求阻塞无关的 Phase 6 检索工作。

交付：

- Chrome、Edge、Firefox、Safari 常见 Netscape Bookmark HTML 的版本化 parser；兼容常见大小写、字符集声明和扩展属性
- 增量暂存源目录树、空目录、同级顺序和所有 bookmark occurrences；入口丢弃 favicon，不加载文件引用的任何资源
- 保守 URL identity 与聚合：严格保留 query/fragment；同 URL 关联一个 Site 和多个 occurrences，已有 Site 默认不覆盖
- invalid、unsupported、private-only、sensitive、文件内重复、账号内已有等状态与原因码
- 目录簇优先的确定性分类器、可编辑目录映射和置信度；只对低置信度去重簇调用受预算约束的 LLM
- 敏感 URL 模型隔离与私网“可收藏、不可服务端抓取”策略；预算为零/耗尽、Provider 不可用时确定性降级
- 第一阶段解析预检和第二阶段变更预览；两次确认均使用账号/payload/版本绑定的一次性 token
- chunk checkpoint、取消、重启恢复、失败分片重试、文件 hash 检测和端到端幂等提交
- 聚合任务页、目录簇视图、抽样与分页明细；不向前端或 Agent 返回全量数据
- 版本化 `import-browser-bookmarks` Skill、后端 tool schema、运行说明和 golden examples；所有 Agent 通过同一工具链执行

完成标准：

- 用户提供的 2,541 occurrence mock 在重复运行中产生确定一致的目录、顺序、URL identity 和状态统计；两阶段确认前后数据库变化符合 PRD。
- 合成 100,000 occurrence 文件满足 PRD 10.1 的 120 秒预检、128 MiB 额外 Python 分配、512 MiB worker 峰值、5 分钟纯本地提交、分页 P95 和 5 秒取消门槛。
- 服务/浏览器重启、分片失败、重复确认和重试不产生重复 Site 或 occurrence；取消后不再调度新分片。
- 跨账号任务/分页/确认访问全部失败；敏感 URL 不进入模型载荷，私网目标没有服务端网络请求，内嵌 favicon 不落库。
- 无 LLM 配置或预算耗尽时仍可完成规则分类、预览和确认入库，无法判断项明确进入“未分类”。

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
- 注册 `import-browser-bookmarks` Skill 与导入工具，仅传递任务/目录簇/分页引用；Agent 不接收全量书签，不直接解析文件或写数据库
- 接通书签导入的预检确认与最终提交确认，Skill 运行记录版本、预算、降级和结果摘要

完成标准：

- PRD 中自然语言修改与删除场景全部通过。
- 未确认动作绝不写库。
- 图恢复、请求重试和重复点击不会造成重复执行。
- Agent 发起、网页继续和服务重启恢复的是同一个账号任务；任何入口都不能绕过两阶段确认。

### Phase 8 - Portability & Release Hardening

交付：

- 当前账号数据 ZIP 导入导出，不含密码、会话 token、API Key、checkpoint 和向量
- 实例备份/恢复脚本与主密钥单独备份说明
- Windows 局域网启动、地址展示和防火墙说明
- Chromium、Firefox、WebKit 桌面网页回归
- 任务恢复、故障降级、容量和安全测试
- 2,541 mock golden regression 与 100,000 occurrence 容量/内存/取消/恢复基准报告
- 更新 `LearnProjects/00_Docs` 中的 Web 全栈 Agent 架构实战文档

完成标准：

- 备份恢复、任务重启恢复和 PRD 全部 MVP 验收场景通过。
- Chrome、Edge、Firefox、Safari/WebKit 关键网页流程无阻塞问题。

## 7. 测试策略

### 7.1 后端

- pytest + pytest-asyncio：API、仓储、事务、任务和 Agent 流程。
- Hypothesis：URL 规范化、账号隔离和确认凭证属性测试。
- 临时 SQLite 与真实 Qdrant Local integration。
- Provider 使用 mock transport；真实付费调用只做手工 smoke test。
- 必测 SSRF DNS/redirect、token replay/stale/cross-user、secret masking、任务重启。
- 浏览器书签 golden tests 固定覆盖提供的 2,541 occurrence mock：目录嵌套/空目录、顺序、重复 URL、不同标题、query/fragment identity、unsupported 原因和重复运行确定性。
- 导入属性测试覆盖 Netscape 标签/属性大小写、截断/畸形 HTML、字符集、超深目录、超长字段、危险协议、HTML/script 文本转义和内嵌 favicon 丢弃。
- 网络层使用禁止联网的 mock/spy 证明 private/loopback/link-local/reserved/metadata 书签只保存不抓取；模型 transport 断言敏感 URL、完整 query/fragment 与秘密属性从未进入请求。
- 两账号分别覆盖 job、folder、occurrence、staging、分页、取消和确认 ID 猜测；重启、重复 chunk、重复确认、stale payload 与相同文件 hash 路径均验证幂等。
- 预算测试覆盖零预算、簇数/token/费用上限、预算中途耗尽、Provider 超时和失败降级，断言不会退化为逐 URL 模型调用。

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
- 书签导入合同固定任务阶段、聚合计数、原因码、目录簇、游标分页、预算/降级、两类确认 token 和 Skill tool schema；100,000 条数据不得产生全量响应 fixture。

### 7.4 性能

- 两账号各 100,000 条 Site 的隔离、分页与 FTS P95。
- 50 URL 批任务的部分失败、取消与恢复。
- 提供的 2,541 occurrence mock 作为每次 CI 的 parser/dry-run 回归集，计数、树和 identity 输出必须确定一致。
- 100,000 occurrence 合成 Netscape HTML 作为独立容量门禁：预检 <= 120 秒、额外 Python 分配峰值 <= 128 MiB、worker 峰值 <= 512 MiB、纯本地提交 <= 5 分钟、分页/聚合 API P95 < 1 秒、取消状态 <= 5 秒；记录 CPU、内存、磁盘和 SQLite 版本。
- 100,000 条基准验证使用增量暂存、批量 SQL 和有界队列；禁止构造全量 Agent prompt、单次 JSON 响应、浏览器 DOM 或逐条 LLM/fetch 调用。
- SQLite WAL 写冲突、单 worker 队列和向量重建。

## 8. 主要风险与应对

| 风险 | 应对 |
|---|---|
| P0 范围大 | 按 Phase 0-8 与独立 Phase 5B 纵向交付，每阶段单独验收 |
| AI SDK v7 跨语言流协议变化 | 独立 codec、固定版本和 fixture 合同测试 |
| LangGraph checkpoint 与业务消息双存储 | main DB 作为 UI 事实源，以 run/message ID 对齐 |
| SQLite + Qdrant Local 单写者限制 | Windows MVP 固定单 API worker；扩容时迁移服务型存储 |
| 进程内登录限流在重启后清空 | MVP 固定单 API worker 并使用两级有界窗口；多 worker 或更高安全等级前迁移到共享持久限流 |
| Ollama / bge-m3 不可用 | 显示索引状态，始终保留 FTS 与手动管理 |
| 任意模型不支持 tool/structured output | 明确报错并回退手动流程，禁止猜测执行 |
| 局域网 HTTP 无传输加密 | 明示仅适用于受信任 LAN；公网或不可信网络必须先加 HTTPS |
| SSRF 与网页 Prompt Injection | 网络层地址校验、内容限额、不可信上下文和工具白名单 |
| 浏览器批量打开限制 | 用户手势触发、操作前提示、记录失败项并允许重试 |
| 厂商图标授权和暗色适配 | 只用官方/明确授权资源，本地打包并维护来源清单 |
| 十万书签导致内存、响应和数据库锁膨胀 | 增量 parser/staging、有界 chunk、游标分页、批量 SQL、单写 worker 和独立容量门禁 |
| 按 URL 调用模型造成成本失控 | 目录簇/规则优先，只批量处理低置信度去重簇；分析前锁定预算并支持零模型降级 |
| 书签 URL 泄露 token 或内部地址 | 敏感检测先于 Provider；query/fragment 不进模型/日志；私网只保存不抓取；自动化 transport spy 验证 |
| 同 URL 合并丢失目录或覆盖既有资料 | Site 与 occurrence 分表，来源树长期保留；默认 append-only，覆盖只能作为第二阶段显式候选 |
| 重试/恢复重复导入 | file hash、稳定 source node key、chunk checkpoint、数据库唯一约束和一次性确认 token |

## 9. 阶段门禁

每个 Phase 只有同时满足以下条件才可标记完成：

1. 该阶段完成标准有自动化或明确的手工验证证据。
2. 不新增跨账号访问路径或绕过确认的写入路径。
3. 文档、环境样例和启动方式与代码一致。
4. `pnpm lint`、`pnpm typecheck`、`pnpm test`、`pnpm build` 及后端测试通过。
5. 运行数据、密钥、日志、缓存和临时输出未进入 Git。
6. Phase 5B 只有在 2,541 mock golden、安全/隔离/幂等测试和 100,000 occurrence 性能门槛均有证据，且内置 Skill 通过 Phase 7 两阶段确认闭环后，才能标记完整完成。

## 10. 当前迭代

Phase 0 网站与 API Foundation 已完成，主线正在收尾 Phase 1 账号闭环。后端认证、迁移、本机密码重置以及网站端注册/登录/退出/主题同步已经接通；当前整仓后端测试基线为 50 项，可复用的业务资源账号范围仓储与跨账号拒绝模板是本阶段剩余工作。浏览器书签方向已经完成 parser/dry-run、2,541 occurrence mock、100,000 occurrence 合成基准和 `import-browser-bookmarks` Skill 合同；接下来提交脱敏 golden fixtures，并等待 Phase 2/4/5/7 的业务表、Provider、任务 API 与确认能力逐步接入。不得把本地预检与 Skill 合同表述为正式导入、真实自动分类、两阶段写库确认或 Agent 运行时闭环。
