# WebHub 迭代队列

这份文件是**自运行迭代的唯一调度依据**。每一轮迭代：

1. 从上往下找第一个 `状态: 待做` 的条目，只做那一个；
2. 做完必须过该条目的「完成标准」，再跑全量门禁（见文末）；
3. 门禁全绿后提交中文 commit（不推送），把条目改成 `状态: 已完成` 并写上 commit 短 hash；
4. 若中途发现新的阻塞问题，插入到当前条目**之后**，不要打乱已排好的优先级；
5. 队列清空即停止迭代。

排序依据来自 2026-07-26 的需求审计（15 done / 10 partial / 4 missing）与用户实测反馈
「很多功能没闭环，甚至还有一个空白页面」。空白页已定位为 `/settings/providers`。

对应 `IMPLEMENTATION_PLAN.md` 的 Phase 4 / 5 / 6 / 7。

---

## Q1a · Provider 前端契约层与 HTTP 客户端

状态: 已完成 · 444c024
对应: Phase 4（前端部分，第一半）

交付已落地：`lib/provider-contract.ts`（类型 + 归一化 + 三种 secret action 的请求体构造）、
`lib/provider-client.ts`（8 个导出，含 429 的 Retry-After 解析与 409 版本冲突中文文案）、
`app/styles/providers.css`（727 行，全令牌化）、17 个新测试。

安全性已逐条自查（不是靠子代理自述）：
- 三处 secret action 分别正确：create=`write`、update=`replace|clear`、test=`test`
- create 请求体不含 `expected_version`（后端 `extra="forbid"`，多传即 422）
- `normalizeProviderConfig` 会拒绝任何含 `secret`/`api_key`/`apiKey`/`token` 的响应并抛错，
  理由是宁可中断也不让明文进 state
- 两个文件零 `console` 调用

---

## Q1b · Provider 配置页面与组件

状态: 待做
对应: Phase 4（前端部分，第二半）· todolist 基本功能第 3 条(a)(b)

**这是「空白页」真正被消除的那一步。** Q1a 已经把契约层和样式铺好，
但 `app/(workspace)/settings/providers/page.tsx` 仍是纯 `WorkspaceEmptyState` 占位，
`components/settings/` 目录还不存在。所以用户依然无法通过 UI 填 API Key。

直接复用 Q1a 的成果：`@/lib/provider-client` 的 8 个函数、`@/lib/provider-contract` 的类型、
`app/styles/providers.css` 里已写好的 `provider-` 前缀类名（动手前先读那个 CSS，按已有类名写组件，
不要另造一套）。

**为什么排第一**：这就是用户实测看到的空白页。后端 providers 模块已全功能（CRUD、AES-GCM
加密、SSRF 校验、registry 内置厂商预设含申请地址），但前端零消费——
`app/(workspace)/settings/providers/page.tsx` 整页只有一个 `WorkspaceEmptyState`，
`lib/` 下没有 provider 客户端。后果：用户无法通过 UI 填 API Key，
阶段A/B 建成的**整条 Agent 链路对普通用户不可达**。这一条不做，前面所有工作都验收不了。

交付：
- `lib/provider-contract.ts` + `lib/provider-client.ts`（镜像 library-contract/library-client 的拆分与约定）
- 配置页：厂商列表（消费 `GET /api/providers/registry`，展示内置预设、是否需要 Key、申请地址）、
  新增/编辑/启用/删除表单、按 kind（model / search / embedding）分区
- 视觉走设计稿令牌体系，与首页同一套（`app/styles/` 下新增 partial）

完成标准：
- 从零开始，只用浏览器就能配好一个模型 Provider 并让首页 Agent 真的回话
- 密钥输入后，任何 GET 响应里都只出现 `SECRET_MASK`（`********`），前端 state / 日志 / DOM 均无明文
- 编辑时不填密钥 = 保留原密钥，不会把已存的 Key 清空
- 同一 kind 下启用第二个配置时，前一个自动停用（后端唯一索引已保证，前端要正确反映）
- Ollama 这类无需 Key 的厂商可以留空提交
- 新增前端测试覆盖 contract 归一化与 client 请求形状

---

## Q2 · Provider 连接测试与自动获取模型列表

状态: 待做
对应: Phase 4（后端适配器）· todolist 基本功能第 3 条(c)

现状：`providers/service.py` 的 `test_connection` 硬编码返回 `status="unsupported"`、
`code="connection_test_unsupported"`，`registry()` 里 `connection_test_supported=False`。
模型名靠用户手填，只做长度校验。todolist 原文允许延后，但它同时是「连接是否可用」的唯一反馈渠道。

交付：
- 真实厂商适配器：打 OpenAI 兼容端点的 `GET /models`（Ollama 用 `/api/tags`），
  既验证 Key 又拿到模型列表
- 复用 `providers/targets.py` 的 SSRF 校验（调用前重新解析 DNS）
- 把 `connection_test_supported` 翻真；失败时**不覆盖旧配置、不切换启用项、不泄露 Key**
- 前端：配置页的模型名改为「拉取列表后下拉选择 + 允许手填」双模式

完成标准：
- 填对 Key 能列出真实模型；填错 Key 返回明确但不含厂商原文的中文错误
- 厂商异常文本（可能含 URL / 请求体 / Key 片段）绝不出现在响应里
- 测试连接失败后，原有配置与启用状态完全不变（要有测试断言）
- 私网/环回地址除 Ollama 外一律拒绝

---

## Q3 · Agent 的「改」能力（确认制写工具）

状态: 待做
对应: Phase 7 · todolist「对话就能增改查」

现状：`agent/tools.py` 7 个工具全是只读 + `propose_site` 出草稿，**没有任何 update / move / pin 工具**。
Agent 改不了分类、改不了名、置不了顶、移不进 Space。更糟的是
`components/agent/agent-panel.tsx` 的快捷 chip 写着「把 Figma 移到『设计』并置顶」——
点了必然做不到，是虚假能力宣传。

交付：
- 新增确认制写工具：`propose_site_update`（改名/描述/分类/标签/置顶）、`propose_space_membership`（移入移出 Space）
- 严格沿用 propose → 人工确认的模式：工具**只产出草稿**，绝不直接写库
- 前端确认卡支持「修改类」草稿：并排展示改前/改后 diff，确认后走普通 library PATCH 接口
- 写库授权始终来自用户会话，不来自 Agent

完成标准：
- 「把 Figma 移到设计分类并置顶」这句话真的能走通，且必须点确认才生效
- 未确认时数据库无任何变化（要有测试断言，比对操作前后的行数与字段）
- 跨账号 site_id 一律拒绝
- 乐观并发：草稿生成后网站被别处改动，确认时应报冲突而不是覆盖

---

## Q4 · 网站抓取与元数据提取

状态: 待做
对应: Phase 5 · todolist Agent 能力「自动分析网站，提取 title/icon/previewpic/description/关联网站」

现状：整个抓取子系统**不存在**。后端无任何 HTML 解析库；唯一的出站 HTTP 是
`agent/web_search.py` 调搜索 API，不访问目标网站。`favicon_url` 只是客户端透传，
`analysis_status` 全后端零写入点，详情页「内容分析」永远显示「未分析」。

交付：
- `webhub/ingestion/` 抓取服务：取 `<title>`、`og:title`、`meta description`、`og:image`、favicon
- **必须复用 `providers/targets.py` 的私网拦截**，抓取前重新解析 DNS；限制重定向次数、响应体大小、超时
- 只信 `Content-Type: text/html`；不执行 JS（不引浏览器）
- `analysis_status` 真正流转 `not_analyzed → pending → complete/failed/limited`
- 接入建站流程与详情页「重新分析」按钮
- 关联网站：先做最朴素可讲的——从页面里识别 GitHub 仓库链接，其余不编造

完成标准：
- 给一个真实 URL，能自动填出标题与描述，`analysis_status` 变 `complete`
- 指向 `127.0.0.1` / `10.x` / `169.254.x` / DNS 重绑定的 URL 一律拒绝并有测试
- 抓取超时/404/非 HTML 不阻塞入库，落 `failed` 或 `limited` 并给出中文原因
- 预览截图**不做**（需要无头浏览器，成本与安全面都不成比例）——在详情页明确写清这一点，不留空占位卡

---

## Q5 · 批量 URL 入库闭环

状态: 待做
对应: Phase 5 · todolist「支持单个或者批量」

现状：`ProposeSiteArgs` 只收单个 url；批量完全依赖模型自觉循环调用，无代码保证每个 URL 都被处理；
草稿卡要逐张点确认，后端也没有批量端点。

交付：
- 后端批量端点：一次最多 50 个 URL，逐项状态（pending/ok/duplicate/invalid/failed）、可取消、可重试
- Agent 侧：`/存入` 命令由代码解析出全部 URL 后逐个提交，不再靠模型自觉
- 前端：批量草稿列表 + 全选确认 + 逐项取消

完成标准：
- 粘贴 10 个 URL（含 1 个重复、1 个非法、1 个超时）不会互相阻塞，各自给出状态
- 确认前主数据无变化
- 重复确认（重放）被拒绝，不会写入两条

---

## Q6 · Site 自定义排序

状态: 待做
对应: todolist 基本功能第 1.3 条「还需要能够自定义排序，自定义管理」

现状：`SortKey` 只有 `created|updated|name`；`position` 列只存在于 `space_members`，
`Site` 本身没有排序列；全站无任何拖拽实现。Space 内只有置顶/上移/下移/沉底四个按钮。

交付：
- `Site.position` 列 + Alembic 迁移 + 按 (user_id, category_id) 的排序语义
- `sort=custom` 取值与 reorder 接口
- 前端拖拽排序（键盘可达：必须同时支持上下移动的按钮或快捷键，不能只有鼠标）
- Space 成员排序改为一次提交多个 id（后端已支持，前端目前每次只传 1 个）

完成标准：
- 在分类内拖动网站，刷新后顺序保持
- 只用键盘也能完成重排
- 并发重排不会产生重复 position（唯一约束 + CAS）

---

## Q7 · Space「一键全部打开」补全

状态: 待做
对应: todolist V0.0.2

现状（首页可用但有洞）：入口只在首页且只覆盖最近更新的 8 个 Space，第 9 个及以后全站无入口；
`/spaces` 列表页与详情页都没有这个按钮；部分被拦截后重试会**重复打开已成功的标签**。

交付：
- `/spaces` 列表页与详情页补入口
- 重试只针对失败项，不重开已成功的
- 文案与行为对齐：「在当前窗口依次打开」实际是首个占用当前标签、其余开新标签，说清楚

完成标准：
- 任意 Space 都能一键全开，两种模式都真实生效
- 被拦截后重试不会产生重复标签

---

## Q8 · 检索与 RAG

状态: 待做
对应: Phase 6 · todolist「Agent 需要维护一个 RAG 来检索内置数据库」

现状：LlamaIndex 完全未引入（依赖都没装）。站内检索是 SQLite FTS5 + 中文 LIKE 兜底的关键词匹配，
无语义召回。`embedding` kind 的 Provider 配置槽位是死预留（`resolve_optional_binding` 只被传过 `kind="search"`）。

交付：
- LlamaIndex ingestion + 本地向量库，按账号分区
- FTS 与向量的融合排序：**精确命中必须优先**，语义结果不得掩盖已知名称/URL
- 增量更新、重建、索引状态展示、向量不可用时降级回 FTS
- 消费 `embedding` Provider 配置

完成标准：
- 两个账号的向量检索无串库（要有测试）
- 删掉索引能从 SQLite 完整重建
- 搜准确名称时，精确命中排第一
- 未配置 embedding Provider 时自动降级到 FTS，不报错

---

## Q9 · 视觉收尾与死代码清理

状态: 待做
对应: 阶段F 收尾

- 1c 紧凑态还差 3 处：吸顶底色不透明度、字标 16px、导航 padding 5px
- 分类 Tabs 未吸顶时不该常驻投影（需 sentinel + `data-stuck`）
- `.agent-result-label` 缺 `letter-spacing: 1px`；`.agent-panel .chat-text` 缺 900px 行长上限
- `PopCheck` 未完成态圆环应 13px（现 14）
- `globals.css` 里 `.site-header` / `.brand` / `.icon-button` / `.site-detail-*` 等已成死代码
- `home.css` 的 `.home-recent-favicon` 裁切 hack 已冗余
- `agent-help-card.tsx` 三个「让 Agent 帮你」快捷入口 `href="/"`，不带预填提问
- SiteHeader 的 IntersectionObserver 缺 `rootMargin`，紧凑态触发略迟滞

完成标准：设计稿 1a–1f 逐屏比对无可见偏差；`globals.css` 无未被任何 tsx 引用的类。

---

## 全量门禁（每轮提交前必须全绿）

```bash
cd apps/web && npx tsc --noEmit && npx eslint . && node --test && npx next build
cd services/api && uv run pytest -q && uv run ruff check .
```

基线：前端 70 测试 / 后端 277 测试。新增功能必须带测试，基线只能涨不能降。

## 不可动摇的约束

- 绝不硬编码任何 LLM 供应商或 API Key；模型访问一律走 per-account 的 providers 模块
- 完整密钥绝不进前端 state、日志、聊天记录或普通数据导出；对外只给 `SECRET_MASK`
- 厂商/供应商异常文本绝不透出到客户端（可能含 URL、请求体、凭据片段）
- 所有 Agent 工具强制服务端账号作用域；Agent 的指令永远不能替代授权
- Agent 不得直接写库，写操作一律 propose → 人工确认
- 非 Ollama 的 Provider base URL 必须 HTTPS 且过 SSRF 校验
- 不推送 GitHub
