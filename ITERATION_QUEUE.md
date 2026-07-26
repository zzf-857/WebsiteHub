# WebHub 迭代队列

这份文件是**自运行迭代的唯一调度依据**。每一轮迭代：

1. 从上往下找第一个 `状态: 待做` 的条目，只做那一个；
2. 做完必须过该条目的「完成标准」，再跑全量门禁（见文末）；
3. 门禁全绿后提交中文 commit（不推送），把条目改成 `状态: 已完成` 并写上 commit 短 hash；
   **同时更新 `PROGRESS.md`**（状态、基线、能跑/不能跑两张表、下一步）——
   过期的状态快照会让接手的人去重做已完成的事，比没有快照更有害；
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

状态: 已完成 · 8b09093
对应: Phase 4（前端部分，第二半）· todolist 基本功能第 3 条(a)(b)

空白页已消除。交付：`lib/provider-form.ts`（表单纯逻辑，可脱离 DOM 单测）、
`components/settings/` 四个组件（workspace 编排 / form / card / dialog）、
`tests/provider-form.test.ts` 18 个测试（前端 87 → 105）。
样式全部复用 Q1a 的 `provider-` 类名，`providers.css` 只增补 `.provider-page-lead`、
`.provider-notice` 与一条 `.provider-error` 内嵌按钮的图标修正。

完成标准逐条核对：
- 密钥不进 React state：输入框非受控（`ref` + `type="password"`，从不设 `value` prop），
  组件只留 `secretFilled` 布尔量；提交读一次 ref 直接进请求体，成功后清空。
  四个组件 + `provider-form.ts` 零 `console` 调用。
- 编辑不填密钥 = 请求体无 `secret` 键；「意图换新但没填」不会退化成 `clear`。两条都有测试。
- 启用后整表重拉而不是就地打补丁，同 kind 下不会同时出现两个「已启用」。
- Ollama 留空提交有测试覆盖（`secret` 键完全不出现）。
- **未做浏览器端到端验证**：登录需要输入密码、试通需要真实 API Key，两者都得由用户本人操作。
  静态门禁（tsc / eslint / 105 测试 / next build / pytest / ruff）全绿。
  下次有人登录后请实测一遍「配一个模型 Provider → 首页 Agent 回话」，有偏差按 Q9 一并收尾。

刻意没做的：
- 「测试连接」按钮按 registry 的 `connection_test_supported` 显示。后端当前恒 false，
  所以这一版看不到该按钮——处理函数 / 结果条 / 429 限流文案都已接好，Q2 翻真后自动出现。
  不做点了必然无效的假入口。
- 向量服务分区文案如实写「检索链路尚未接入，此处保存的配置暂时不会被调用」
  （`langgraph_runner` 只解析 model 与 search）。Q8 接入后再改文案。

---

## Q2 · Provider 连接测试与自动获取模型列表

状态: 已完成 · b23e712
对应: Phase 4（后端适配器）· todolist 基本功能第 3 条(c)

交付：`providers/connectivity.py`（OpenAI 兼容打 `GET /models`，Ollama 打 `GET /api/tags`）、
registry 新增 `default_base_url`（`agent/provider_binding.py` 的 `DEFAULT_BASE_URLS` 改为从
注册表派生，探针与 Agent 运行时不会对同一厂商的地址产生分歧）、
`connection_test_supported` 由 `kinds` 派生并翻真、前端模型名接 `datalist` 双模式。
后端 277 → 308，前端 105 → 110。

四条安全性质按「构造上成立」写的，不靠记得处理：
- 非 2xx 响应**一个字节都不读**就中止 —— 厂商错误体常回显请求 URL / 请求体 / Key 前缀，
  不读就不可能泄露；所有失败塌缩成固定中文文案，无任何厂商内容被插值
- 全程只发一个 GET、不写任何东西，所以失败时原配置与启用项不可能变
  （另有测试直接比对 `provider_configs` 整表前后快照）
- 调用前重新跑 `validate_connection_target`（DNS 可能保存后被重指）+ `follow_redirects=False`
  （否则一个 302 到 169.254.169.254 就绕开了 SSRF 校验）
- 响应体上限 512KB，流式读取超限即中止

顺带修掉一个自相矛盾的规则：`test_connection` 原本无条件要求先填模型名，而连接测试的
目的恰恰是「列出有哪些模型」——最需要这份列表的人反而拉不到。`_validate_complete` 增加
`require_model_name` 参数，测试路径传 False，保存启用配置时仍强制。前端
`validateProviderDraft` 同步加 `mode: "save" | "test"`。

**搜索类厂商（tavily / jina / exa）如实返回 `unsupported`**：它们没有只读目录接口，
拿用户的 Key 做健康检查等于花他的搜索额度。若以后要做，应作为独立条目排期，
并明确告知用户「这次测试会消耗一次搜索配额」。

同样**未做浏览器端到端验证**（需要真实 API Key，只能由用户本人操作）；
静态门禁全绿。

---

## Q3 · Agent 的「改」能力（确认制写工具）

状态: 已完成 · 4473a02
对应: Phase 7 · todolist「对话就能增改查」

交付：`propose_site_update`（改名/说明/分类/标签/置顶）、`propose_space_membership`（移入移出
Space）两个确认制写工具；前端三种新视图（site-update / space-membership / noop）、
改前改后 diff 卡、`confirmAgentSiteUpdate` / `confirmAgentSpaceMembership`。
后端 308 → 319，前端 110 → 118。快捷 chip「把 Figma 移到『设计』并置顶」不再是虚假宣传。

几处刻意的设计：
- 每个可编辑字段都是 `X | None = None`，None = 别动。**None 与「清空」保持可区分**：
  省略 `description` 保留原文，传 `""` 才是清空。合并这两者会让一次改名悄悄抹掉说明。
- 草稿只包含**真正发生变化**的字段；一个变化都没有时返回 `status="noop"` 而不是生成草稿
  ——一张点了等于没点的确认卡，用户没法和真的修改区分开。
- 草稿携带该行当时的 `version`，确认时原样回传；中途被别处改过就 409 而不是覆盖。
  有测试完整走这条路：propose → 别处先改 → 用陈旧版本确认 → 409 且原修改完好。
- `propose_space_membership` **不会新建 Space**（建 Space 本身就是写操作），
  目标不存在时如实拒绝并列出已有的 Space。
- Space 按名称解析时的归一化与 `spaces/service._space_name` 写入时完全一致
  （NFKC + 折叠空白 + casefold），否则从界面建的 Space 在这里查不到。
- 前端解析 `changes` 用 `typeof` 而不是真值判断，否则 `description:""` 与 `pinned:false`
  这两个合法改动会被当成「没改」；缺乏乐观锁版本或改动集合的草稿降级为 raw，绝不半渲染。

同样**未做浏览器端到端验证**（需要登录与可用的模型 Provider）；静态门禁全绿。

---

## Q3b · 书签导入的落库与前端入口

状态: 已完成 · 5894251 / 12877da / 102fcef / 3b6cdc2
对应: todolist「导入浏览器书签」· 用户 2026-07-26 实测请求
插入原因: 用户提供 `MockData/bookmarks_2026_7_26.html` 要求「跑通自动导入流程」，
实测发现流程只建了前一半。按队列规则插在当前条目之后、不打乱后面的优先级。

**已经能用的部分（本轮实测确认）**：
`skills/import-browser-bookmarks/scripts/preview_bookmarks.py` 对真实文件跑通，1.68 秒、
峰值内存 1.3MB：2541 条书签 / 368 个文件夹 / 最深 8 层 → 2024 个去重候选、511 条重复被聚合、
6 条被拒（`chrome:` 2 / `file:` 3 / `note:` 1）、1325 个内联 favicon（占源文件 70.6%）被丢弃、
7 个疑似含敏感参数的 URL 被标记、14 组仅 fragment 不同的疑似重复只标记不自动合并。
规则分类给出 9 个建议分类（未分类 537、学习与文档 447、效率工具 394…）。

**实际有三个断层（当初这条记少了一个）**：
1. **没有落库端点。** 线上 OpenAPI 里 `bookmark-imports` 只有 6 个：`POST` 上传 +
   5 个 `GET` 预览。`bookmarks/persistence.py` 只**读** `Site`（算 `identity_url` 命中），
   全模块没有任何一处创建 `Site`。暂存层已经算好了每个候选的 `proposed_action`
   （create / skip_existing / merge_missing_metadata / reject / needs_review），
   缺的只是执行这一步。
2. **前端零入口。** `apps/web` 下没有任何 `.tsx`/`.ts` 提到 bookmark，
   上传、看预览、确认导入都没有界面。
3. LLM 分类（`classification.py` / `classification_batches.py` /
   `classification_contract.py`）只被 `preview.py` 的规则分类用到，没有接 Provider，
   也没有路由或 worker 调用它。

交付：
- 落库端点：`POST /api/bookmark-imports/{job_id}/apply`，按候选的 `proposed_action` 执行，
  账号作用域，幂等键防重放，分批提交（2000+ 条不能一个事务吞下）
- `skip_existing` / `merge_missing_metadata` 必须真的按 `identity_url` 命中来走，不能盲插
- 文件夹路径 → 分类/标签的映射沿用暂存层已算好的结果，不在落库时重新发明一套
- 前端：设置页下的导入入口（上传 → 预览摘要 → 确认导入 → 结果），
  预览要显示「将新建 N 条 / 跳过 M 条已存在 / 拒绝 K 条」

实测结果（真实文件，2026-07-26）：
上传 1.6MB → 解析 1.08s → 2909 事件 / 368 文件夹 / 2541 记录 → 2024 去重候选、511 重复；
落库前 sites=0 → apply 0.47s → created 2024 / failed 0 → sites=2024；
重复 apply → created 0 / skipped_existing 2024，行数不变。
浏览器端另用小文件验证 UI 接线（面板无文件选择工具）：新增 4 条、跳过已存在 1 条。

**当初漏记的第三个断层**：没有 parse worker。上传后 job 停在 `queued_parse`，
`finalize_parse_run` 全仓库只有测试和崩溃恢复函数在调。已补 `bookmarks/worker.py`
（进程内，非 schema 预留的租约式 worker）。

刻意没做：不填 `bookmark_source_occurrences` / `site_import_origins`——
这两张表全仓库无人写，「哪条书签记录变成哪个网站」的溯源链整体未建，
半填一份只会造出一张看起来权威、实际只覆盖走过本函数的导入的表。

## Q3c · 确认结果回写会话历史（Agent 会否认自己刚存过的东西）

状态: 已完成 · 0b34f16
对应: Phase 7 收尾 · 2026-07-26 全流程实测发现
插入原因: 实测 Q3 时发现的缺陷，属于已交付功能的正确性问题。
**建议先于 Q3b 做**——它会让 Agent 对用户的数据说假话，比缺一个导入界面严重。

**复现（已实测，全程真实 DeepSeek 调用）**：
1. 「帮我收录 https://www.figma.com，分类放设计，标签 UI 和 原型」→ Agent 出草稿 → 点确认
   → 库里确实写入了（sites 0→1，分类「设计」与标签自动建好）
2. 同一对话里接着说「把 Figma 移到设计并置顶」
3. Agent 回答：「Figma **还没有正式保存**到你的资料库（之前只是生成了草稿，你还没确认保存），
   所以我目前查不到它的 site_id」——而同一屏上那张卡片明明写着「已保存到资料库」

**两个独立缺陷**：

1. **模型不查就断言。** `conversation_messages` 第 4 条的 `sources_json` 是 `[]`，
   这一轮**一次工具都没调**，却对库里有没有这个网站下了结论，标注还是「来源：llm推荐」。
   系统提示只要求「我有没有/我收藏过」这类问句先调 `search_library`，
   而「把 X 置顶」不是这种句式，规则没兜住。
2. **确认结果永远进不了会话历史。** 第 2 条消息里存的 `propose_site` 结果被永久冻结在
   `"status": "awaiting_confirmation"`。用户在浏览器点确认后写了 Site 行，
   但没有任何东西把「已确认、site_id=xxx」写回历史。**即使模型行为完美，
   它每轮回放到的也是一份说「还没保存」的历史。** 这是更深的那一层。

新开一个对话再说同一句话就完全正常（已实测：只把 `pinned` 放进 diff、确认后 `version` 1→2、
说明和标签未被牵连），所以工具本身没问题，问题在历史与提示。

交付：
- 确认成功后把结果回写会话（新增一条 tool/system 性质的消息，或把原 tool result 标记为
  已确认并补上 site_id / space_id）。要幂等，重复确认不产生第二条
- 提示补一条硬规则：调用 `propose_site_update` / `propose_space_membership` 前**必须**先
  `search_library` 拿到真实 site_id；没查过就不得断言某个网站不在库里
- 前端确认成功后应让后续轮次能看到这一事实，而不是只改自己那张卡片的按钮文案

完成标准：
- 「收录 → 确认 → 同一对话里接着改」这条最自然的链路能一次走通，不需要新开对话
- Agent 不再在零工具调用的情况下断言库里有没有某个网站
- 重复确认不写入两条历史

---

## Q4 · 网站抓取与元数据提取

状态: 已完成 · f3ca0b9 + 19d266b
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
  （2026-07-27 自查实测：`globals.css` 4321 行，是前端最大的文件，需逐类核对 tsx 引用）
- `home.css` 的 `.home-recent-favicon` 裁切 hack 已冗余
- `agent-help-card.tsx` 三个「让 Agent 帮你」快捷入口 `href="/"`，不带预填提问
- SiteHeader 的 IntersectionObserver 缺 `rootMargin`，紧凑态触发略迟滞

完成标准：设计稿 1a–1f 逐屏比对无可见偏差；`globals.css` 无未被任何 tsx 引用的类。

---

## Q10 · 结构自查收尾（大文件拆分与第二批去重）

状态: 待做
对应: 2026-07-27 结构自查发现
插入位置: 放在 Q9 之后而不是插队。这些是健康度问题，不阻塞任何用户可见能力；
队列既有顺序是按用户价值排的，先把功能补完再统一收尾。
**但如果 Q4–Q8 期间要动到下列文件，就地拆比事后拆便宜，届时可提前处理。**

已在 `0ffbe30` 修掉的（不必重做）：
- `agent ⇄ bookmarks`、`library ⇄ bookmarks` 两处循环依赖 → 依赖图现在是干净 DAG
- 四个 `*-contract.ts` 里约 30 份逐字节相同的校验原语 → 抽成 `lib/contract-guards.ts`
- 注意：`space-contract` 的 `identifier` **刻意保留在本地**（带长度上限且拒绝数字，
  与共享版语义不同）。别再"顺手"把它合并进去。

本条要做的：

1. **`bookmarks/persistence.py` 1864 行**，占后端代码近 10%，是全仓库最大的文件。
   里面混着 job / run / checkpoint / staging 四类关注点。按这四类拆成子模块，
   公开 API 保持不变（`queries.py`、`routes.py`、`worker.py` 都依赖它）。
2. **`space-workspace.tsx` 1071 行 / `agent-panel.tsx` 1038 行**：
   两个组件各自塞了列表 + 详情 + 多种弹层。至少把弹层与列表拆出去。
3. **三个 workspace 组件（library / space / provider）的加载、分页、错误处理是同构的**，
   疑似存在第二批可抽取的重复。先测量再动手——像契约层那样先确认逐字节相同，
   不要凭"看起来像"就合并。

完成标准：
- 单文件行数：后端无 >1000 行，前端组件无 >700 行
- 拆分不改变任何公开 API，测试基线只涨不降
- 合并任何重复前，必须先证明语义相同（`0ffbe30` 里 `identifier` 的教训）

---

## 全量门禁（每轮提交前必须全绿）

```bash
cd apps/web && npx tsc --noEmit && npx eslint . && node --test && npx next build
cd services/api && uv run pytest -q && uv run ruff check .
```

基线：前端 124 测试 / 后端 332 测试。新增功能必须带测试，基线只能涨不能降。

## 不可动摇的约束

- 绝不硬编码任何 LLM 供应商或 API Key；模型访问一律走 per-account 的 providers 模块
- 完整密钥绝不进前端 state、日志、聊天记录或普通数据导出；对外只给 `SECRET_MASK`
- 厂商/供应商异常文本绝不透出到客户端（可能含 URL、请求体、凭据片段）
- 所有 Agent 工具强制服务端账号作用域；Agent 的指令永远不能替代授权
- Agent 不得直接写库，写操作一律 propose → 人工确认
- 非 Ollama 的 Provider base URL 必须 HTTPS 且过 SSRF 校验
- 不推送 GitHub
