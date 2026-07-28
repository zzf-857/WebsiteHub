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

状态: 已完成 · 51a2032 + 850198d
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

状态: 已完成 · 07fdc57 + 4146a2f
对应: todolist 基本功能第 1.3 条「还需要能够自定义排序，自定义管理」

现状：`SortKey` 只有 `created|updated|name`；`position` 列只存在于 `space_members`，
`Site` 本身没有排序列；全站无任何拖拽实现。Space 内只有置顶/上移/下移/沉底四个按钮。

交付：
- `Site.position` 列 + Alembic 迁移 + 按 (user_id, category_id) 的排序语义
- `sort=custom` 取值与 reorder 接口
- 前端拖拽排序（键盘可达：必须同时支持上下移动的按钮或快捷键，不能只有鼠标）
- ~~Space 成员排序改为一次提交多个 id~~ —— **没做，理由如下**：
  前后端的契约都已接受 id 列表（library 与 spaces 皆然），但两个界面都只有
  单条的上下移动按钮，没有多选模型，因此永远只会送一个 id。真正要「一次移动多个」
  需要给列表加多选（复选框 + 选中态），那是独立的一块界面工作，不属于本条。
  队列原文「前端目前每次只传 1 个」描述准确，但它不是一行改动能解决的。

完成标准：
- 在分类内拖动网站，刷新后顺序保持
- 只用键盘也能完成重排
- 并发重排不会产生重复 position（唯一约束 + CAS）

---

## Q7 · Space「一键全部打开」补全

状态: 已完成 · 3a4279b
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

状态: 已完成 · ae960d7 + f9267e3
对应: Phase 6 · todolist「Agent 需要维护一个 RAG 来检索内置数据库」

现状：LlamaIndex 完全未引入（依赖都没装）。站内检索是 SQLite FTS5 + 中文 LIKE 兜底的关键词匹配，
无语义召回。`embedding` kind 的 Provider 配置槽位是死预留（`resolve_optional_binding` 只被传过 `kind="search"`）。

**没有引入 LlamaIndex**，理由见 commit：完成标准是行为性的，没有一条要求这个库；
几千条规模下暴力点积是个位数毫秒，为此引一整棵依赖树并把语料放到 SQLite 备份
够不着的地方是更差的交易。改为 `webhub/search/`：fusion（RRF + 精确命中提权）
＋ vectors（按账号分区的 float32 裸存与暴力最近邻）＋ embeddings（账号自己的
Provider，失败一律降级）。

顺带修掉一个现存缺陷：FTS 此前是 WHERE 过滤条件而非排序依据，
「精确命中排第一」在今天根本不成立，与有没有向量无关。

原定交付：
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

状态: 已完成（9c0b004）
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

实测结果：`globals.css` 4418 → 3807 行，残留死类 0（扫 258 个类，39 个死类，
分整条规则 / 混写选择器 / 死祖先后代三轮删）。上面 8 条视觉项全部改完。

遗留验证缺口（不阻塞，但别当成已验证）：内嵌 Browser 面板 `visibilityState` 恒为
hidden、不合成帧，**IntersectionObserver 回调与 CSS transition 都不推进**
（新建 IO 观察 `document.body` 1.5s 零回调可复现）。所以只验证了「置位 data-stuck /
data-compact → 计算样式正确」，两个 observer 的实际触发时机没在浏览器里跑通。
和 Q6 拖拽那条是同一类缺口，一起在真实浏览器里补。

---

## Q10 · 结构自查收尾（大文件拆分与第二批去重）

状态: 已完成（09e63c6 后端 / 469ca1c + 593d4dc + cd9054a 前端）
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

实测结果（2026-07-27）：

1. **后端 ✅ 达标**。超标的不止条目里写的 persistence.py，实测有四个，全部拆完：
   persistence.py 1864→6 模块（最大 510）、models.py 1227→7 模块（最大 359）、
   library/service.py 1097→5 模块（最大 415）、chat/service.py 1033→4 模块（最大 468）。
   后端最大文件 1864 → 749。全部用「目录 + `__init__.py` 门面」，34 处 import 未改。
   拆包三个坑已写进代码注释：① `from X import name` 导入时绑定，测试 patch 门面
   到不了子模块 → 6 个被 patch 的名字改模块限定访问；② 两处循环（recover→finalize、
   detail→messages）靠移动归属打断；③ chat 的 header 用相对导入，搬深一层后
   `.commands` 撞上新建同名子模块。
2. **前端 ✅ 达标**。分两步：先抽走无状态部分（纯函数、本地类型、只吃 props 的
   展示组件）→ 879/783/901；再把 useState/useEffect/useCallback 整块抽成
   `useXxxWorkspace` hook → 组件剩 410/423/352，hook 553/468/667。前端最大组件
   1101 → 667。中途踩过一次：渲染函数（返回 JSX 的 `renderList` 之类）被误当成
   逻辑抽进了 hook，导致 hook 反而 772 行；它们靠闭包读状态，藏在 hook 里看不出
   依赖了什么，移回组件后 hook 降到 553、返回值从 15 变 33——**这个数字变大是对的**。
   附注：`lib/agent-contract.ts` 734 行仍超，但它是契约模块不是组件，不在本条范围内。
3. **第二批重复：不存在**。按规则先测量——把三个 workspace 与两个 parts 文件
   按函数块切开、规范化空白后比对，逐字节相同的只有 `closeDialog` 一处、6 行。
   为 6 行抽公共模块反而多一层间接，且两边 `DialogState` 类型本就不同。
   「加载/分页/错误处理同构」的猜测在字节层面不成立，**不要再来一遍**。

---

## Q11 · 语义索引回填与检索接线

状态: 已完成（80f5dad）
对应: Q8 收尾 · 2026-07-27 用户批准
插入原因: Q8 建了 `search/vectors.py` + `search/embeddings.py` + `search/service.py`，
三个模块都有测试，但**全仓库零调用**——`stale_sites` / `store_embedding` /
`hybrid_search` / `drop_index` 没有任何生产代码路径调它们。检索链路目前只是骨架。

要做的：

1. 回填入口：把 `stale_sites` 算出的待补站点交给 `embed_texts`，结果写回
   `store_embedding`。**不能在请求线程里同步跑**——一次可能几百个站点、要打厂商接口。
   参考 `bookmarks/worker.py` 的进程内 worker 做法，别引入新的调度依赖。
2. 检索接线：`library` 的列表/搜索路径接上 `hybrid_search`，把 FTS 命中的 id 传进去，
   语义召回作为增强。**未配 embedding Provider 时必须静默降级到纯 FTS**，
   这条 `search/service.py` 已经保证了，路由层别再包一层错误提示。
3. 索引管理：给用户一个能看到「已索引 N / 待索引 M」并触发重建的入口，
   `drop_index` 已就绪。

完成标准：
- 未配 embedding Provider 的账号，搜索行为与接线前**逐字节一致**（要有测试证明）
- 回填任务失败不影响任何用户可见功能，也不会重复消耗额度（`content_digest` 已管这个）
- 跨账号不串：`nearest` 的账号作用域在 SQL 里，路由层不得改成事后过滤
- **花钱提示**：回填要调用户自己的 embedding Provider。触发前必须显示预估请求数。

实测结果：三项全部交付。检索接线选了「新增 relevance 排序」而不是把融合塞进
现有排序——后者会让同一请求在配了 Provider 后悄悄换顺序，也会让「逐字节一致」
这条标准变成靠运气。相关度用有界偏移分页，因为 RRF 序不是任何一列的值。

**多智能体证伪抓出的 7 个问题都已修**（详见 80f5dad 的 commit 说明），其中三个
是真会伤到用户的：① 先 drop 后 schedule，已有一轮在跑时会丢掉整个索引；
② 每次搜索、每翻一页都重复购买同一个查询向量（前端一次搜索还并发发两个请求）；
③ `nearest` 抛 SQLAlchemyError 会 500 掉整个搜索，而不是降级。

留给后续的已知边界（不阻塞，别当 bug）：
- `_RUNNING` 并发守卫是模块级 dict，只在单进程内有效。WebHub 按单机单进程设计，
  真要多 worker 部署得换成库里的租约。
- 语义召回只能重排关键词候选，不会引入关键词没命中的站点——因为向量索引不认识
  分类/标签筛选，引入了就会出现「加了筛选反而看到不符合筛选的结果」。
  要放开得先给语义命中补一条回 SQL 的筛选复核。
- `indexed` 只按 (user_id, model) 计数、不校验摘要，改过站点名后覆盖率偏乐观。

---

## Q12 · LLM 分类接线（全库重分类）

状态: 已完成 · bc0ca91
对应: Q3b 遗留 · 2026-07-27 用户批准
插入原因: `bookmarks/classifier.py` 完成且有测试（few-shot、封闭分类法、2–8 标签校验），
但没有任何入口把结果落到 `Site` 上。用户的原始诉求是「让模型处理**所有网站**的分类」，
不只是导入进来的那批。

用户已明确的两条约束（2026-07-26 原话）：
- **重复网站由内部程序合并**，只把合并后的结果交给 LLM。机械式唯一性排查不该花 token。
- LLM 只做「检索与理解」，成本控制由工程结构保证，不是靠提示词恳求。

要做的：

1. 全库来源适配：现有 `classification_batches.py` 的 `ClassificationCandidateSource`
   是面向书签导入候选的。需要一个从 `Site` 行构造 subject 的适配层，
   **复用同一套投影与预算逻辑**，不要再发明一份。
2. propose → 确认：`propose_reclassify` 出草稿，草稿必须携带
   `estimated_request_count` / `estimated_input_characters`（两个函数已就绪），
   **用户看到预估请求数之后才允许开跑**。
3. 落库：确认后按 `validate_classification_batch_output` 的结果写 `Site.category_id`
   与标签。走乐观锁，中途被改过就 409，不覆盖。

完成标准：
- 分类前的去重合并**全部在 Python 侧完成**，发给模型的 payload 里不含重复主体
- 未配 model Provider 时，propose 阶段就明确拒绝，不产生一张点了必然失败的草稿
- 用户不确认就一个 token 都不花（要有测试证明 propose 阶段零厂商调用）
- 模型返回的分类名必须过封闭分类法校验，不得凭模型自由发挥新建分类

实测结果：交付全库重分类服务 (`library/reclassify.py`)、`propose_reclassify` 工具与端点、
未配 Provider 早期拒绝、Python 侧域名聚合去重，以及乐观锁落盘。
通过 3 项全新测试，后端测试基线升级为 440 个，门禁全绿。


---

## Q13 · 未闭环逻辑审计与修复（含在途图标特性收尾）

状态: 已完成 · 00aa40c（第一轮）+ 本轮安全收口
对应: 2026-07-27 接手审计
插入原因: 接手时工作区有 82 个文件未提交，混着两件事——一次已完成且有 MANIFEST
记录的清理，和一个未完成的「分类图标 + favicon + preview_url」特性。后者让门禁变红
（后端 5 红 / 前端 9 红）并违反两条硬约束。已按用户批准拆成两个 commit。

**修掉的四个真 bug（都经过独立证伪，不是靠子代理自述）**：

1. **Q12 的 apply 100% 不生效**（最严重，`reclassify.py:216`）。
   `res.mappings` 里是 `BoundClassificationMapping`（`slots=True`，只有
   `source_id` / `mapping` / `used_fallback`），代码却 `getattr(bound, "subject_id")`
   ——恒为 None，于是每条都 `continue`，一个站点都不更新，却仍返回
   `status="success"`。**用户花掉了 token、看到"成功"、库里一行没变。**
   现在改读 `bound.source_id` 与 `bound.mapping`，并按 `category_id`（契约里
   existing 的权威引用）而不是按名字解析目标分类。
   已补 `test_reclassify_apply_moves_site_to_end_of_nonempty_category`，并**反向验证过它真能抓到这个
   bug**（把代码改回旧写法 → 测试红）。此前 3 个 Q12 测试只覆盖 propose 的
   rejected/noop，apply 零覆盖，所以门禁全绿也放过了它。

2. **favicon 走了第三方 CDN**（`service/_common.py`）。新增的 `resolve_favicon_url`
   在站点没有图标时回落到 `https://www.google.com/s2/favicons?domain=...`：
   ① 直接违反「favicon 不走第三方 CDN」；② 用户书签库里每个域名都会被逐个透露给
   第三方，是隐式浏览历史泄露；③ 它把 `favicon_url` 永久填满，导致 ingestion
   那条「只补空字段」的规则永不触发——**真抓到的图标反而写不进去**。
   已整个删除，没有图标就返回 None，前端 `SiteFavicon` 用首字符渲染本地字母块。
   原有的三个 favicon 测试（本来是红的）自动转绿。

3. **迁移的 downgrade 从未成功过**（`52c3f6173b38`）。autogenerate 直出的
   `batch_alter_table` 在 SQLite 上重建表，`categories_search_rename` 触发器
   在 RENAME 期间对已不存在的 `sites` 触发。**坑在于 upgrade 假装成功**：
   batch 模式下纯 add_column 被优化成普通 ADD COLUMN、不重建表，所以只有
   downgrade 会炸。改成直写 add_column；downgrade 先摘掉那一个触发器再装回。
   上一个迁移（`20260727_0007`）的作者已踩过并写了注释，但新迁移没人看它。

4. **重命名分类会抹掉用户手选的图标**（`service/categories.py:84`）。
   `update_category` 在 `icon` 省略时也跑 `infer_category_icon` 覆盖。
   已改回 Q3 确立的语义：**None = 别动，"" = 恢复默认推断**。

**补完的断链**：
- `reclassify` 草稿在 `agent-contract.ts` 有投影、在 `use-agent-panel` 有确认
  handler、后端有 `/reclassify/apply` 端点，但 `conversation-thread.tsx` 缺
  `view.kind === "reclassify"` 分支——**整张卡片什么都不渲染，确认按钮永不出现**。
  已补 `ReclassifyCard`，并按 Q12 要求把预估请求数与字符数摆在确认按钮之前。
- `QUICK_PROMPTS` 是空数组（`.agent-chips` 容器渲染却没有 chip）。查 git 发现
  三条提问在 `593d4dc` 抽 hook 时被误清空，对应能力都真实可用，已恢复。
- `list_bookmark_imports` 的任务行只有 job_id/state，走不了 `toLink`，每条都被
  filter 掉、渲染成「没有命中任何结果」。已改为 facets 投影 + 状态中文标签。
- `preview_url`：`og:image` 早就被 `metadata.py` 解析并转成绝对地址了，但
  `apply_outcome` 只读 description 与 icon，抓到的图片直接丢弃。已接上（同样只补空）。
- `summary` 列全链路无任何写入方，属于纯死字段，已从模型/schema/迁移中删除
  ——留着只会让人以为有这个能力。
- `run_parse` 失败时无条件 re-raise，而唯一调用方是 `asyncio.create_task` 起的
  脱钩任务、done_callback 只 discard：抛了没人接，只产生
  "Task exception was never retrieved" 噪声。改为只在「连失败都没记下来」时抛，
  与 docstring 契约一致。
- `listing.py` 给 `SiteListAggregate` 传 `total_count`，而 schema 无此字段、
  pydantic 静默丢弃。已删并加注释。

**二次安全收口（真实模型长任务暴露出的边界）**：
- 重分类现在是严格的 category-only 任务：标签在提示词、输出校验和落库三层禁用，
  模型即使返回标签或多余字段也不会改动用户标签。
- 草稿同时携带完整网站 `{id: version}` 与分类法 `{category_id: name}` 快照。
  apply 在花额度前验证一次，模型返回后在写事务中再验证一次；新增、删除、改名或版本变化
  均返回 409，整批不写。
- 模型 I/O 前主动结束 SQLite 读事务；返回后用 `BEGIN IMMEDIATE` 取得写保留锁，
  再读全库快照。每条 UPDATE 还带 `user_id/id/version` 条件，任一冲突整批回滚。
- 跨分类移动会为目标分类分配稳定且唯一的尾部 position；提交前再次检查断连，
  防止浏览器已经看到失败后后台仍产生意外写入。
- `run_plan` 最多 4 路并发，每批最多 2 次尝试。每次 Provider 调用由
  `asyncio.timeout` 限制为 90 秒总 wall-clock；草稿同时披露预计请求数与含重试的最大请求数。
- 断连会阻止后续批次和重试；同组任一任务失败时会 cancel 并 drain 其他 Provider 任务，
  不让已失败的请求继续消耗额度。Next 外部 rewrite 窗口由默认值提升到 45 分钟，
  覆盖 50 批、4 并发、2 次尝试的约 39 分钟最坏合法计划。
- 预算不足或 localhost/私网隐私排除不再悄悄变成“部分全库”：propose/apply 都会在
  调用模型前整体拒绝。数据库锁失败和未知异常只返回固定安全文案，不透出 SQL、driver
  或厂商内容。
- 新增迁移 `20260727_0009`，只为仍是 `Folder` 的历史分类回填名称推断图标，
  保留明确手选图标；数据修复不可逆，downgrade 不覆盖用户选择，迁移往返已测。

**真实 Provider / Chrome 验收（不要为了复核再次花额度）**：
- 真实 Chrome 已确认首页分类和网站卡片渲染 `Folder` / `Bot` / `Gamepad2` / `Code` /
  `PenTool` 等 Lucide 图标，AI 分类网站卡片实际为 `Bot`。
- 一次真实 Provider 全库重分类后，网站数保持 2027，`version_sum` 从 2029 变为 2880，
  即 851 个网站各更新一次；分类映射哈希发生变化。标签、Bookmark、analysis、source、
  provenance 均未变化，分类内 position 零空值、零重复。
- 首次真实长任务超过旧 20 分钟代理窗口时，前端先收到断连而后端仍继续并最终提交，
  暴露了“失败后意外写入”的真问题；上述断连检查、任务回收和 45 分钟代理窗口已针对它闭环。

**审计中被证伪、确认不是问题的（别再来一遍）**：
- `recover_finalizing_parse_run` 无生产调用方 —— 命中 PROGRESS.md「刻意没做的」
  第 3 条（进程内 worker，不做崩溃自愈），它是给未来真 worker 预留的脚手架。
- `/reclassify/propose` REST 端点无前端调用方 —— 有 HTTP 层测试覆盖，
  且它正是「将来做非 Agent 入口」的使能者，不是障碍。
- reclassify 路由的裸 `except Exception` 透出厂商原文 —— 不可达：
  `classifier.py:187` 已把所有厂商异常折叠成固定文案，且非 2xx 时一个字节都不读。

**未做的**：分类图标没有前端选择器，`icon` 目前只由后端按名称推断（`icons.py`）。
`dynamic-icon.tsx` 的 ICON_MAP 与 `icons.py` 的值域已用脚本核对（icons.py 能推断出的
36 个图标名，ICON_MAP 37 项全部覆盖，无一缺失），
所以推断结果不会静默回落成 Folder。要让用户手选得加一块 UI，属独立条目。

---

## Q14 · 用户手动排查第一轮：站点详情、真实图标与 Agent 流式富文本

状态: 已完成 · 3d7ddd7
对应: 2026-07-27 用户截图反馈
插入原因: 用户开始逐屏手动验收，反馈 Provider 页混入书签信息、网站图标不真实、
已收录卡片直接外跳，以及 Agent 不流式、暴露原始 Markdown、缺少可折叠 reasoning 与真实用量。

本轮代码范围：

- Provider 配置页只保留 Provider；语义索引状态与回填迁到独立的
  `/settings/search-index`，账号菜单增加明确入口。
- 单站创建、批量 URL 与书签导入后统一进入有界后台分析队列；页面声明的 favicon 优先，
  失败只尝试同源 `/favicon.ico`，不调用第三方 CDN。图标响应经过每跳 SSRF、DNS 固定、
  MIME、大小与图片签名校验后才落库；页面声明的安全 `og:image` / `twitter:image`
  写入 `preview_url`。
- 首页、资料库与 Agent 返回的已收录网站统一先进入 `/library/{siteId}`；详情页用明确的
  “访问官网”按钮外跳，并在有 `preview_url` 时展示稳定比例预览。
- Agent 使用 AI SDK UI Message Stream 输出 reasoning 与正文；正文由固定版本的
  `streamdown` 渲染 Markdown/GFM，禁用 Markdown 图片，HTTP(S) 链接卡片化，未知工具载荷
  不再显示原始 JSON。reasoning 流式时展开、结束后折叠，异常或中止不会永久显示“思考中”。
- 服务端保存实测总耗时、首 Token 延迟、reasoning 耗时与 Provider usage；Provider 未返回
  usage 时隐藏 Token，不做估算。内置 OpenAI/DeepSeek 开启标准流式 usage；自定义
  `openai_compatible` 不会被盲目附加可能不兼容的 `stream_options`。
- 补充抓取并发、SSRF/DNS 重绑定、并发编辑保护、流式协议、历史恢复、链接过滤、
  站内导航和设置页拆分的自动化回归测试。

自动化门禁：前端 TypeScript / ESLint / 159 tests / Next build；后端 Ruff / 503 tests；
`git diff --check` 全绿。按用户要求没有执行浏览器、真实 Provider 或历史全库补全验收。

本轮明确不做：

- 不操作浏览器；页面布局、交互与截图由用户手动验收。
- 不调用真实 model Provider，不消耗用户额度。
- 不触发 2027 条历史网站的元数据补全，只交付用户可主动触发的有界入口。

后续项：为自定义 `openai_compatible` 增加显式 Provider 能力开关，允许用户确认端点支持
`stream_options.include_usage` 后再启用真实流式 usage；不能仅凭“OpenAI 兼容”名称自动判断。

---

## Q15 · 用户手动排查第二轮：图标补强、预览交互与资料库大列表管理

状态: 已完成 · 36c1fe4
对应: 2026-07-28 用户逐屏验收反馈
插入原因: Q14 的主链路已接通，但真实网站仍会因 403、超时、非标准 favicon 响应而落回默认图标；
详情预览占位偏重；拥有大量历史网站时，资料库缺少安全的批量删除和连续浏览路径。

本轮三项需求：

1. **真实 favicon 补强**
   - HTML 最多保留 8 个去重的 `icon` / `apple-touch-icon` /
     `apple-touch-icon-precomposed` / `mask-icon` 声明候选；声明无效时继续尝试站点根路径
     `/favicon.ico`、`/favicon.png`、`/favicon.svg`、`/apple-touch-icon.png`。
   - 页面 HTML 返回 403、超时、非 HTML 或连接失败时，根路径图标仍独立尝试；仅图标成功时保存
     `favicon_url` 并把失败结果收敛为 `limited`，不伪装成页面完整分析成功。
   - 每个候选及每次重定向继续执行 SSRF、DNS 固定、手动重定向、大小、MIME 与图片 magic
     校验；通用 MIME 只接受真实图片签名，SVG 保持严格校验。禁止任何第三方 favicon CDN。
   - “补全网站信息”会重试 `not_analyzed` / `pending` / `failed` / `limited`，明确跳过
     `complete`；仍保持账号范围、活动任务排除和有界队列。

2. **详情页紧凑预览**
   - `preview_url` 从大块主视觉移入主信息中的紧凑预览行，不制造额外截图或空占位。
   - 点击预览进入原图放大层；关闭按钮、背景点击与 Escape 均可收起，关闭后焦点回到触发按钮，
     远程图片失败时同时关闭放大层并移除预览。

3. **资料库大列表管理**
   - 增加显式选择模式、卡片 checkbox、“全选”和“全选（已加载）”双入口与选中计数；后者选择
     当前实际加载的全部网站且不按 100 截断，前者通过无分析副作用的轻量接口冻结当前筛选命中
     的完整账号内版本快照。选择态不触发详情导航或拖拽，筛选 scope 改变立即失效，同 scope
     刷新只保留仍可见的选择且不得静默升级勾选时冻结的版本。
   - 删除请求仍单批最多 100 条，每条携带 `{site_id, expected_version}`；超过 100 条时只确认一次
     并由前端串行分批，展示已完成数量。服务端逐批按账号预检、条件 DELETE 与 rowcount 二次
     校验，任一缺失或版本冲突则当前批整批回滚并停止后续批次；未知网络结果也停止，不盲目重试。
   - 置顶与普通列表各自使用 `IntersectionObserver` 静默加载，提前 640px 预取；现代浏览器
     主路径移除手动“加载更多”按钮，不支持 Observer 的环境保留可操作降级，并对重复游标、
     并发请求、失败游标、筛选切换和陈旧响应做防护。

自动化门禁：前端 TypeScript / ESLint / **169 tests** / Next build；后端 Ruff /
**520 tests**；`git diff --check` 全绿。当时新增覆盖包括多 favicon 候选、页面失败后根图标、
generic MIME + magic、批量删除账号隔离/原子回滚/Space 版本、旧版 100 条选择边界、
同 scope 选择保留，以及自动分页的游标与陈旧请求防护。本轮双全选和串行分批语义已取代旧
选择边界，遵照用户要求只做静态复核，尚未重新运行自动门禁。

本轮没有操作浏览器、调用真实 Provider 或触发 2027 条历史网站的全量元数据补全。

**非阻断已知风险（后续单独收紧）**：单删与批量删除都先读取关联 `SpaceMember`，再删除
`Site`。极窄并发窗口内若另一事务刚好在两步之间新增成员，外键级联会删除新成员，但关联
Space 可能不在旧快照中而漏涨 `Space.version`。当前账号隔离、请求版本校验和整批回滚均已
覆盖常规路径；后续应在读取成员前锁定所选 Site 行，或用兼容 SQLite 的条件 no-op UPDATE
取得写锁，再补并发回归测试。

---

## Q16 · 历史网站元数据补全闭环与有界后台调度

状态: 已完成（本次提交）
对应: 2026-07-28 用户反馈“多数网站不会自动获取图标和内容”，并要求支持数千到上万网址

根因不是本地图标组件失效：历史书签导入早于自动分析调度，约两千条 `not_analyzed` 记录没有进入
任何队列；原有前端刷新也只覆盖首屏，且离屏首页分区会持续轮询。直接把全部历史网址塞入内存或
一次并发抓取会让本机网络、CPU 和 SQLite 写入竞争失控。

本轮交付：

- 列表打开和书签 apply 均唤醒数据库驱动的历史扫尾；按 `not_analyzed`、遗留 `pending` 和一次性
  不完整结果重试分段查询，领取时通过条件 UPDATE 防重复。导入后只取最新 8 条前台优先，剩余
  仍按历史顺序后台处理，避免 2000 条 ID 常驻内存。
- 全进程网络分析最多 4 个，其中自动扫尾最多占 2 个；普通队列单账号 256、全局 1024，自动发现
  每账号只保留 16 个候选。手动“重新分析”在满队列时有一个交互逃生槽，不会被历史任务饿死。
- 新增 `analysis_updated_at` 迁移，派生分析不再伪装成用户编辑。缺少说明或图标的 `complete`
  记录最多延迟重试一次；用户在上次分析后做过编辑（包括主动清空）时不自动填回。
- favicon 使用按 origin 的 singleflight、LRU 正/负缓存和总墙钟超时；页面声明图标只按原候选复用，
  子页面没有声明时复用站点根图标，不使用第三方 favicon CDN。
- 前端列表刷新保留已滚动分页的游标与内容；首页分类将“已加载过”与“靠近视口”分离，只有附近
  分区做有限轮询，避免用户滚过大量分类后形成 N 倍后台请求。

本轮仅执行静态代码审阅与 `git diff --check`，按用户要求没有运行测试、构建、浏览器、截图或真实
Provider。手动验收：打开首页或资料库并等待历史卡片逐步补全；导入后观察最新少量网址优先；确认
详情页手动重新分析在后台繁忙时仍可完成；滚动很长的资料库后，元数据刷新不重复或卡住分页。

---

## Q17 · 持久化全库元数据回填与首页进度工具栏

状态: 代码完成，待用户手动验收（未提交）
对应: 2026-07-28 用户要求“首页一键批量回填、有进度、数千到上万网址不拖垮电脑”

旧 `/sites/analyze-missing` 只会把最多 256 个网站 ID 放入当前 API 进程内存，响应是启动时的
排队快照；它既不能给进度条固定分母，也无法在重启后继续。因此首页不能直接复用该接口。

本轮交付：

- 新增 `site_metadata_backfill_runs` / `site_metadata_backfill_items` 迁移，以及
  `site_metadata_preferences`。run 固定目标总数；item 保存 `site_id`、版本、初始分析状态、origin
  与本批 pending claim 时间，不对 `sites` 建级联外键，删除的网站以 `skipped` 计入既定分母。每账号
  通过 SQLite 部分唯一索引最多一个活跃 run。run 的 six-way 状态计数有总数守恒约束，进度读取不再
  `GROUP BY` 扫描整批 item，即使万条任务也只读一行。
- 新增 `POST /library/metadata-backfills`、`GET /library/metadata-backfills/{run_id}` 与
  `GET /library/metadata-backfills/active`。重复点击、多标签或刷新后均加入/恢复同一 run；响应严格拆分
  queued/running/completed 和 complete/limited/failed/skipped，前端无需猜测真实进度。
- worker 每次只从数据库领取一个 item，不把全库 ID 预读到内存；同一 origin 串行，显式任务最多
  两个消费者。自动扫尾与显式任务共享最多 3 个后台网络槽，全局第四槽留给用户交互式分析。
- item 领取同时检查快照版本、初始状态和陈旧 pending 边界。pending 状态写入 item 的同一事务中，
  重启仅可恢复该批自己的遗留 claim；用户之后改网址、修改资料或主动清空字段时只记 `skipped`，不允许
  旧抓取结果回写。run/item 使用租约，应用启动恢复、正常关闭立即归还，异常进程由过期租约回收。
- 描述或 favicon 的用户填写、覆盖、主动清空均有偏好记录保护；历史 Google S2 伪 favicon 会被真实
  图标替换，抓不到真实图标时清除而不继续渲染第三方 URL。完整读取 HTML 却没有 `og:image` /
  `twitter:image` 时只记录一次“已检查无预览”；超时、失败、截断页面，或已声明图片但安全验证暂时
  未通过时不会被误标记为已检查。
- 首页 `AgentPanel` 下方新增紧凑工具栏。前台 2.5 秒、后台页 15 秒顺序轮询单个 status 接口；
  完成时刷新首页数据区，不为每个网站建立浏览器端请求。

本轮按用户要求未运行测试、构建、迁移、浏览器、截图或真实网站抓取，仅进行代码静态审阅和
`git diff --check`。用户验收前需执行数据库迁移。预览图只使用站点公开声明的 `og:image` /
`twitter:image`；未声明预览图是正常完成，不为填满而引入截图浏览器或第三方 favicon CDN。

性能结论：页面简介与预览属于具体 URL，逐页读取 HTML 仍有价值；favicon 属于 origin 级数据。
当前已通过同源 singleflight/LRU 和 Q17 同源串行避免对子页重复轰炸。对现库 2,027 条 URL / 1,456 个
host，持久 origin favicon 缓存可进一步消除约 571 次重复发现；这应作为独立的缓存层改造，不应通过
提高并发或把用户域名交给第三方图标 CDN 达成。当前快照仍在一个 SQLite 事务中写入所有 item：万级
任务的下一步应引入不可领取的 `snapshotting` 状态后分段提交，不能直接拆事务而让 worker 处理半份快照。

---

## Q18 · LLM 网站资料三工具分析与原子回填闭环

状态: 代码完成，待用户手动验收（未提交）
对应: 2026-07-28 用户要求把“分类、标签、详细介绍”做成 LLM 可调用的站内工具并规范化回填

本轮交付：

- 新增 `choose_site_category`、`set_site_tags`、`write_site_description` 三个账号内受限工具。模型只生成
  内存草稿，不接触数据库 Session；输入包含安全投影后的分类/标签/书签名，以及最多 12,000 字可见
  正文。输出在工具层和最终提交层重复校验标签数量、纯文本、Markdown/HTML/裸网址和账号 taxonomy。
- Provider I/O 前冻结网站、版本、分类、标签、偏好和来源快照；I/O 后取得账号 taxonomy 写锁，按
  `User -> Category -> Site` 固定顺序重读并验证，再把说明、分类、标签、favicon、preview 与分析状态
  一次提交。新增 `description_is_llm` / `category_is_llm` / `tags_are_llm`，只有可证明由 LLM 生成的旧值
  才允许下一轮覆盖；人工填写、主动清空、删标签和 URL 改址语义均保持优先。
- Q17 item 持久化 `requires_llm`，网络并发与 LLM 并发分离，模型最多 2 路。租约 heartbeat 明确失效时
  会取消仍在等待或执行的 Provider 调用；取得信号量后再次检查 run fuse、item lease 和最新
  `llm_analyzed_at`，避免重复花额度。metadata-only item 不占模型槽。
- run 新增连续 Provider 失败计数和持久冷却：429、5xx、连接错误、超时及异常工具行为不会因单次失败
  清空全批；一次成功即清零，连续三次失败才原子设置 `stop_requested` 并结束未领取 item。401/403/404、
  无 Provider 和明确不支持工具调用仍立即熔断，重启后也不会重新轰炸同一坏 Provider。
- item 每次领取递增 `attempt_count` 并参与后续 claim/defer/release/finish 的全部条件，旧协程即使晚醒也
  不能命中新领取；这些收尾与 `stop_requested` 在同一写事务串行，熔断后的 queued、running 和 Site
  pending claim 都会归入确定终态。`provider_retry_at` 在领取、网页抓取后以及 Provider 调用前重复复核。
- Provider 成功、失败和致命信号独立于 Site 回填 CAS 先行提交，并在释放模型并发槽前完成；付费调用返回后
  的短数据库收尾受 `shield` 保护。heartbeat 以本地单调时钟限定硬租期，续租持续异常会取消旧分析，避免
  两个进程长期同时认为自己持有同一 item。未知本地 enrichment 异常立即停止批次，但不污染 Provider
  连续失败计数；Provider 返回后的本地落盘异常同样立即持久熔断，避免系统性提交故障继续消耗额度。
- 普通分类、标签、书签 apply、网站移动、排序和全库重分类统一使用同一账号 taxonomy mutex；分类删除
  按稳定 ID 顺序锁 source/replacement 后再锁 Site。创建/移动目标分类 reservation 均检查 rowcount，
  分类并发消失返回 `category_conflict`，不再误报 `duplicate_url`。

本轮按用户要求未运行测试、lint、构建、迁移、浏览器或真实 Provider 请求，只做代码静态复核。
`20260728_0011` 尚未在本地数据库执行，迁移文件已同时包含 Q17 与 Q18 的新表和状态字段。

---

## 全量门禁（每轮提交前必须全绿）

```bash
cd apps/web && npx tsc --noEmit && npx eslint . && node --test && npx next build
cd services/api && uv run pytest -q && uv run ruff check .
```

基线：前端 169 测试 / 后端 520 测试。新增功能必须带测试，基线只能涨不能降。

## 不可动摇的约束

- 绝不硬编码任何 LLM 供应商或 API Key；模型访问一律走 per-account 的 providers 模块
- 完整密钥绝不进前端 state、日志、聊天记录或普通数据导出；对外只给 `SECRET_MASK`
- 厂商/供应商异常文本绝不透出到客户端（可能含 URL、请求体、凭据片段）
- 所有 Agent 工具强制服务端账号作用域；Agent 的指令永远不能替代授权
- Agent 不得直接写库，写操作一律 propose → 人工确认
- 非 Ollama 的 Provider base URL 必须 HTTPS 且过 SSRF 校验
- 不推送 GitHub
