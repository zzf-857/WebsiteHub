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

> 这是 Q2 完成时的历史状态。Q20 已按上述前提实现由用户显式触发的最小真实搜索测试，
> 界面会提前告知可能消耗额度；不要再把当前行为改回 `unsupported`。

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

状态: 已完成（`664c5e7`）
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

状态: 已提交（`88dc7e5`）；本机 `0011` 已执行，待用户手动验收
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

该轮按用户要求未运行测试、构建、迁移、浏览器、截图或真实网站抓取，仅进行代码静态审阅和
`git diff --check`；`20260728_0011` 已在 Q19 启动切换时执行。预览图只使用站点公开声明的 `og:image` /
`twitter:image`；未声明预览图是正常完成，不为填满而引入截图浏览器或第三方 favicon CDN。

性能结论：页面简介与预览属于具体 URL，逐页读取 HTML 仍有价值；favicon 属于 origin 级数据。
当前已通过同源 singleflight/LRU 和 Q17 同源串行避免对子页重复轰炸。对现库 2,027 条 URL / 1,456 个
host，持久 origin favicon 缓存可进一步消除约 571 次重复发现；这应作为独立的缓存层改造，不应通过
提高并发或把用户域名交给第三方图标 CDN 达成。当前快照仍在一个 SQLite 事务中写入所有 item：万级
任务的下一步应引入不可领取的 `snapshotting` 状态后分段提交，不能直接拆事务而让 worker 处理半份快照。

---

## Q18 · LLM 网站资料三工具分析与原子回填闭环

状态: 已提交（`88dc7e5`）；本机 `0011` 已执行，待用户手动验收
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

该轮按用户要求未运行测试、lint、构建、迁移、浏览器或真实 Provider 请求，只做代码静态复核。
`20260728_0011` 已在 Q19 启动切换时执行，迁移同时包含 Q17 与 Q18 的新表和状态字段。

---

## Q19 · Agent 收录与 LLM 分析入口修复

状态: 已完成（`a272a0c`），本机运行态已更新，待用户手动验收
对应: 2026-07-28 用户截图反馈 Agent 入库失败、首页 LLM 网站分析 404、详情页缺少明确 AI 分析入库动作

根因与交付：

- 首页 `Not Found` 不是路径拼错，而是 8100 仍运行 7 月 27 日启动且没有热重载的旧 API，同时数据库
  停在 `0010`。已停止旧进程，将 SQLite 主文件、WAL、SHM 作为一个敏感恢复集隔离到
  `F:\AI\AgentMake\temp\WebHub\sensitive-db-backup-2026-07-28-q19`，再用官方 `pnpm dev:api`
  完成 `0010 -> 0011` 并启动带热重载的新进程。OpenAPI 已确认三条 metadata-backfill 路由与单站
  analyze 路由存在；框架默认 404 也会显示明确的升级/重启提示，不再裸露 `Not Found`。
- Agent 的明确 URL 收录不再依赖 LLM 自己挑工具和补齐参数。`/存入 URL`、`URL 入库/收藏/保存/收录`
  由服务端机械生成现有 `propose_sites` 草稿；`http://randomiban.com/入库` 会先剥离动作后缀，还原为
  `http://randomiban.com`。草稿仍是只读预览，必须由用户确认后才调用普通批量入库接口；未知 runner
  错误日志只记录 correlation id、阶段和异常类型，不记录 Provider 正文、URL 或密钥。单次超过 50 个
  URL 会明确拒绝并要求分批，不再静默截断。
- Agent 单条、批量和站外结果确认均显式写入 `source=agent`；公共创建契约只开放 `manual | agent`，
  不能伪造书签导入或备份来源。写入成功会触发首页分类、最近收录、置顶与 Space 数据刷新，不再出现
  “数据库已写入但首页仍像没生效”。确认结果以结构化 system metadata 持久化，刷新或切换历史会话后
  草稿仍保持“已保存”；批量部分失败会显示新增/重复/失败计数并保留重试，不写虚假的成功确认。
- 单站 `POST /library/sites/{site_id}/analyze` 不再丢弃本次 `FetchOutcome`，而是返回
  `{site, outcome, message, llm_applied}`。详情页绿色框位置新增独立“AI 分析入库 / 重新 AI 分析”按钮，
  成功、受限、失败分别显示不同提示；Q21 起分类、标签、简介、详细介绍由四个受控工具生成，人工字段保护不变。
  路由会先释放鉴权留下的 SQLite 读快照，再用新 Session 读取提交结果；历史 LLM marker 不再让重分析
  提前返回，metadata-only 占用结束后会补排真正的 LLM 分析，终态失败也不会无限重排或重复花额度。
- 对新 API + `0011` 的首页持久任务做了静态闭环复核：固定分母、2 路 LLM 并发、3 路后台预算、同源
  串行、Provider 熔断、终态计数、重复点击复用和刷新恢复均已接通。API 现在保留 `completed`、
  `completed_with_errors`、`failed` 与 `stopped_early`，首页会用成功/警告/失败三种终态提示；Provider
  熔断会明确提示检查配置、密钥权限和模型工具调用支持，不再统一显示绿色完成。
- 真实单站复现定位到另一个确定性根因：`_run_tool_graph` 的三个嵌套回调用到了函数局部导入的
  `MessagesState` 注解，LangGraph 编译条件分支时只能从模块全局解析类型，因而每次都在 Provider
  请求前抛 `NameError`。已移除这些无必要注解，并把 `NameError` 归为本地致命故障，批量任务不会再
  把同一个程序错误当成暂态 Provider 故障重试。修后工具图能完成构建并到达 Provider；当前启用上游
  对最小工具图和普通对话均返回 HTTP 503，而只读模型目录可正常列出当前模型，需切换可用 Provider。

本轮按用户要求未运行测试、lint 或构建。已执行 `git diff --check`、数据库版本确认、API/前端启动、
一次单站浏览器复现、一次最小工具图调用和一次普通对话探针；页面完整交互与切换 Provider 后的真实
LLM 结果继续由用户手动验收。

---

## Q20 · 网站分析搜索补证、真实搜索连接测试与免费受限 Provider

状态: 已完成（`a272a0c`），待用户手动验收
对应: 2026-07-28 用户询问国外网站能否借助专业搜索服务，并希望内置安全的默认免费 WebSearch 选择

原有 Agent 已能主动调用账号配置的 `web_search`，但网站“AI 分析入库”只消费直接抓到的页面正文。
当国外站点超时、403、无正文或只有标题时，分类、标签、简介和详细介绍工具拿不到足够证据；同时搜索 Provider
连接测试仍沿用早期 `unsupported` 约定，用户只能保存后再猜 Key 和端点是否可用。GitHub 上的 Skill
大多只是调用第三方 API、自建服务或浏览器抓取的工作流说明，并不天然提供免费、稳定的数据源；把任意
仓库脚本直接放进 WebHub 运行时还会引入命令执行和供应链风险。

本轮交付：

- 单站 LLM enrichment 会先判断页面是否有足量 meta description 或可见正文，并排除常见的
  Cloudflare、人机验证、访问拒绝与登录墙提示页。证据充足时完全保持原流程；
  证据不足时才解析账号启用的搜索 Provider，使用 `site:<hostname> <安全站名>` 发起一次查询。URL path、
  查询参数和页面内指令不会进入搜索词；单次最多 3 条，专业 HTTP Provider 总截止 8 秒、无重试，并在
  返回后再次按目标 hostname 过滤，不能把搜索引擎对 `site:` 的理解当成安全边界。
- 搜索标题与摘要作为低权限 `search_evidence` 注入 `choose_site_category`、`set_site_tags`、
  `write_site_summary` 和 `write_site_description` 工具；提示词明确禁止执行摘录里的命令或从单条摘要扩写无依据事实。搜索结果
  只补分类、标签和纯文本介绍，不得伪造 favicon 或 preview。标题、过短正文或拦截页提示不能单独
  支撑介绍；搜索失败或没有同域结果时只限制当前网站，不把厂商响应透给客户端。
- Tavily、Jina、Exa 的 Provider 定义显式开启真实搜索测试。用户点击“测试搜索（可能消耗额度）”后，
  后端只发送固定的最小查询并最多请求 1 条结果；继续执行 HTTPS/SSRF 复验、禁止重定向、响应大小限制
  和错误脱敏。鉴权失败、额度不足、限流、上游失败和端点不匹配分别映射为稳定提示。测试不写配置，
  也不会把第三方搜索内容返回前端。
- 新增搜索 Provider `exa_mcp_free`，界面名为“Exa MCP 免费额度”，默认固定到 Exa 官方
  `https://mcp.exa.ai/mcp`，无需 Base URL 或 API Key，但仍必须由用户主动添加并启用。它通过官方 MCP
  Python SDK 调用固定的 `web_search_exa` 工具，不下载、不安装、不执行任意 GitHub Skill。Agent 搜索
  最多 6 条；单 API 进程并发 1、同查询缓存 5 分钟、失败冷却 60 秒，降低共享额度和本机资源压力。
- Provider 注册表新增 `search_bulk_supported` 与 `usage_notice` 并贯通后端响应、前端契约和表单。
  免费 Provider 只允许 Agent 对话和单站手动分析，`bulk=True` 时绝不调用；页面证据不足时只把当前
  网站记为受限并继续整批中的其他网站，不把免费搜索不可用于批量误判成全批 Provider 故障。
  Tavily、Jina、Exa 等 BYOK Provider 才可参与持久批量搜索补证。
  搜索工具响应使用账号配置的真实 Provider 显示名，不再写死厂商名。
- 新增直接依赖 `mcp>=1.24,<2`，锁文件当前解析为 `mcp 1.29.0`。官方边界参考
  [Exa MCP 文档](https://exa.ai/docs/reference/exa-mcp) 与
  [Exa MCP Server](https://github.com/exa-labs/exa-mcp-server)。若以后需要完全自托管的免费方案，
  可另做白名单 `SearXNG` Provider；当前没有借用不受控公共实例，也不使用 DDGS/OpenSERP 抓搜索页面。

静态检查：Python `py_compile`、后端定向 Ruff、前端 `tsc --noEmit`、`git diff --check` 已通过。
按用户要求没有运行测试、Next build、浏览器检查、真实 Tavily/Jina/Exa 探针或 Exa MCP 请求；因此这里只能
写“代码完成”，不能宣称搜索链路已经真实跑通。`uv sync` 更新 API 虚拟环境时移除了不属于 API 项目
依赖的 Playwright，但没有修改仓库 Playwright 配置。

用户手动验收：

1. 在搜索服务页添加 Tavily、Jina 或 Exa，确认按钮提示可能消耗额度；主动点击后核对成功及
   鉴权/额度/限流错误提示。不要为了验收连续点击，测试每次都是真实搜索。
2. 添加“Exa MCP 免费额度”，确认无需填写 Base URL/API Key且有共享额度、隐私与低频提示；启用后在
   Agent 打开“允许联网”，执行一次搜索并核对卡片上的 Provider 显示名和跳转结果。
3. 对一个正文充足站点和一个超时、403 或正文不足的国外站点分别执行“AI 分析入库”：前者不应多发
   搜索，后者可用同域摘要补齐分类、标签、简介和详细介绍；两者的 favicon/preview 仍只能来自目标站点本身。
4. 仅启用免费 Provider 时启动首页全库补全，确认系统不调用共享 MCP；原页证据不足的当前网站应记为
   受限，任务继续处理整批中的其他网站。切换到 Tavily、Jina 或 Exa 后再验证持久批量任务可以使用
   搜索补证且仍受 Q17/Q18 的并发与熔断保护。

剩余边界：Exa 免费 MCP 是第三方共享额度，不保证 SLA，不适合公共部署或全库批量；并发、缓存和冷却
目前是单 API worker 的进程内状态。自建 SearXNG 适配器、可安装 Skill 市场和任意 GitHub Skill 执行均
未实现，后两者在没有签名、权限声明、沙箱和人工审计前不应进入产品运行时。

---

## Q21 · 搜索预算修复与简介/详细介绍双层文案

状态: 已完成（`a272a0c`），本机迁移并启动，待用户手动验收
对应: 2026-07-29 用户以 linux.do 反馈免费搜索仍超时，并要求简介 20-50 字、详细介绍 100-300 字

本轮交付：

- 修复 `site_enrichment` 对所有搜索统一施加 8 秒外层截止的问题。Tavily/Jina/Exa BYOK 仍保持 8 秒；
  `exa_mcp_free` 自身以 16 秒覆盖排队、MCP 初始化、工具调用与清理，外层给 17 秒监督窗口。临时 MCP
  会话关闭不再发送可选 DELETE，避免已经拿到结果后又因清理请求超时而丢弃结果。
- 搜索证据仍必须属于目标 hostname，但现在接受 `docs.example.com` 这类目标子域；匹配使用点分隔的
  单向后缀判断，`evil-example.com` 不会命中 `example.com`，子域目标也不会反向放宽到父域。
- `sites.summary` 作为真实独立字段加入 `20260729_0012`；`summary_is_manual` / `summary_is_llm` 与原有
  description 来源分开。URL 改址会重置旧目标的两类文本来源，人工填写或主动清空任一字段都会被保护。
  旧记录不伪造摘要；持久全库任务把缺失 summary 的旧 LLM 记录重新纳入一次补全。
- 网站 enrichment 从三个工具扩为四个。分类和标签逻辑不变；简介必须是 20-50 字单句且不能机械截断
  详细介绍，详细介绍必须为 100-300 字并聚焦核心内容、主要能力和适用场景。两段文本继续拒绝 Markdown、
  HTML、URL 与不可见字符，并与分类/标签一起经过最终快照重验后原子写入。
- 详情顶部只展示简介，下方只展示详细介绍；首页、资料库、Space、相关网站和 Agent 站内结果卡优先
  使用简介，旧数据在摘要为空时回退详细介绍。编辑表单拆成两个字段，前后端均执行简介非空 20-50 字约束。
- 原网页 fetch 失败但搜索证据与四工具成功时，终态从错误的 `failed` 改为 `limited`，并明确说明 AI
  资料已通过搜索完成、原网页仍不可直读且图标/预览可能缺失，避免正确入库结果显示红色失败。

按用户要求未运行测试、Next build、浏览器或真实 Provider 请求。已执行 Python 编译、后端 Ruff、
TypeScript `tsc --noEmit`、`git diff --check`、`0012` 实库迁移与 Alembic schema check；Web/API 已在
3100/8100 启动且 API readiness 正常。真实文案、页面布局和 Provider 可用性由用户继续验收。

用户手动验收：重新分析 linux.do，分别用免费 Exa MCP 与 Tavily 观察搜索结果；核对详情顶部简介、
详细介绍、卡片回退和黄色受限成功提示。再手工改写/清空简介后重新分析，确认人工选择不被模型覆盖。

---

## Q22 · 网址库布局、Agent 流式状态与推荐卡片收口

状态: 已完成（`a272a0c`），待用户手动验收
对应: 2026-07-29 用户确认网站 AI 分析已跑通，并反馈编辑弹窗位置、网址库宽度、流式 React 错误与推荐卡片不稳定

本轮交付：

- 编辑网站的原生 `dialog` 强制在视口居中；进入与退出均有 160ms 动画。关闭请求先同步受控父状态，
  再等待退场完成后执行 `dialog.close()`；保存、删除或加入 Space 期间，关闭按钮、背景点击和 Escape
  都被锁定，原生意外关闭也不会再造成 DOM 与 React 状态分裂。
- 网址库继续以 1360px 居中模式作为默认阅读布局，并提供“居中阅读/铺满页面”二段图标控件；偏好写入
  `localStorage`，宽度变化使用现有动效变量平滑过渡。仅在两种宽度确实有差异的 1408px 以上视口展示
  切换；偏好恢复完成前不启用宽度动画，避免刷新后自动伸缩。
- 产品、Agent、设置、Space、导入与错误文案中的“资料库/收藏库”统一为“网址库”；内部模块名、数据库表
  和 API 路径不做无意义重命名。
- 修复 reasoning 折叠区受控 `details.open` 与 `toggle -> setState` 的反馈环；Streamdown 配置、`useChat`
  回调、工具事件和滚动调度均使用稳定引用或按 ID 去重，流消息以 50ms 节流。所有活动工具在结果卡接管前
  显示真实行动阶段，内部 React/transport 诊断不再原样暴露给用户。新提问、新对话和历史切换会清除旧
  transport 错误；生成期间禁用历史切换和新对话，用户停止后再切换，避免 AI SDK 已排队的旧流写入串台。
  历史读取按请求序号隔离，迟到响应不能覆盖用户已经选择的新画面。
- 新增只读工具 `present_website_recommendations`。模型的最终推荐清单必须通过结构化 name/url/description
  输出；服务端复用保守 URL 规范化，拒绝无效项、去重并按账号回查已收录网址。前端优先使用这份最终清单：
  推荐卡只绑定最新一轮回答；一轮多次调用时仅最后一次清单有效，即使全部 URL 被拒绝也不会退回展示原始
  搜索候选。新消息用 metadata 标记推荐协议版本，模型漏交清单时不再把检索池冒充推荐；无版本的旧会话
  仍兼容原检索结果。已收录项渲染站内详情卡，未收录项复用“收录/打开”卡；全部站内命中显示网址库
  来源，启用联网时站外卡使用当前真实搜索 Provider 名，没有联网能力时才显示模型推荐。

按用户要求未运行测试、构建、浏览器或真实 Provider 请求。最终工作区只执行 `git diff --check` 和源码扫描；
页面动效、真实流和不同模型是否遵循结构化推荐工具由用户手动验收。

用户手动验收：

1. 打开/关闭编辑弹窗，检查居中、背景、按钮、Escape 与进退动画；切换网址库居中/铺满并刷新确认偏好。
2. 发起包含长 reasoning 和多个工具动作的 Agent 请求，确认错误红条不再闪现、阶段提示持续且折叠区可操作。
3. 分别测试网址库命中、Tavily 联网结果和无联网模型知识推荐；所有最终具体网站应使用统一卡片，已收录项
   进入站内详情，未收录项可“收录/打开”，正文不再重复 Markdown 表格或纯文字网址列表。

---

## Q23 · 共享标签、YouTube 媒体与分析状态刷新闭环

状态: 已完成（`a272a0c`），待用户手动验收
对应: 2026-07-29 用户反馈标签不能手动添加、YouTube 视频缺图标/封面、布局选项缺少绿色高亮、
Agent 回答仍整段出现，以及新增网址长期停在“正在分析网站资料”

本轮交付：

- 用户与 Agent 原本就共用账号级 `tags/site_tags`，本轮补齐新增/编辑网站表单中的显式“新建标签”动作。
  创建成功立即加入当前标签列表并自动勾选；网址库和详情页都同步最新 taxonomy。浏览器、Agent 确认链与
  后端草稿统一采用 NFKC、合并空白和大小写无关身份；规范化同名或并发创建返回 409 时，前端重读标签
  列表并复用已有项。Agent 提示与工具契约要求打标签前读取已有列表，同义或近义时优先使用原名称。
- 近义关系没有直接接入 embedding/RAG 固定阈值。标签文本短且多义，目前没有用户确认的同义/非同义
  样本可校准；拍脑袋设置阈值会把不同含义自动合并。先保留模型结合账号现有标签判断，待积累真实重复
  样本后再评估 per-user embedding top-k、阈值和必要的 LLM rerank。
- 抓取器新增严格 YouTube 平台白名单，覆盖 `youtube.com`、`youtu.be`、`youtube-nocookie.com` 的
  `watch`、`shorts`、`embed`、`live` URL。只接受完整合法的 11 位视频 ID，再回退到 YouTube 官方
  `https://www.youtube.com/favicon.ico` 与 `https://i.ytimg.com/vi/<id>/hqdefault.jpg`；真实页面声明
  的 favicon/`og:image` 始终优先，只补空媒体。该逻辑不是通用第三方 favicon 服务，不会把任意网站
  域名逐个透露给图标 CDN；外层抓取超时后平台回退仍会保留。浏览器若无法读取 YouTube 官方 favicon，
  共享图标组件会使用站内播放标识，不再退回字母块。
- 网址库“居中/铺满”二段控件的当前项使用现有产品绿色变量显示文字、浅色背景和内描边，未选项保持
  原有中性状态。
- 后端 LangGraph、UI Message Stream 和前端渲染原本都按 chunk 工作。浏览器整段显示的确定性根因是
  Next 外部 rewrite 默认对 SSE 启用 gzip，短回答被压缩层缓冲到结束才释放。SSE 响应现增加
  `Cache-Control: no-cache, no-store, no-transform`，阻止代理改写流，同时保留其他页面正常压缩。
- 截图中的 NodeSeek 并非后端一直分析：只读数据库确认它约 146 秒后已进入 `limited`，LLM 时间戳、
  分类、标签和简介均已落库。详情页只读取一次、网址库轮询总预算仅 32 秒，才让页面长期显示陈旧快照。
  两处现共用约五分钟有界刷新，后段间隔封顶 15 秒；页面重新聚焦或恢复可见时补读一次，进入终态后停止。新建网站的显式
  enrichment 调度改用单个受控交互溢出槽，减少被历史批量队列挤掉的概率。

仍有一个明确的持久化边界：若 256 条账号队列和单个交互溢出槽同时占满，或 API 进程在执行前崩溃，
网站写库本身不会丢，数据库扫尾也仍可补网页元数据，但“本次立即执行 LLM enrichment”的意图尚未写入
持久任务。网站可进入下一次首页显式补全；若要做到进程重启后本次意图必达，应把它并入持久 run，而不是
继续扩大进程内队列。

按用户要求，本轮未运行测试、lint、构建、浏览器自动化或真实 Provider 请求；页面效果与真实流由用户
手动验收，文档收口仅执行 `git diff --check`。

用户手动验收：

1. 在新增和编辑网站弹窗分别新建标签，确认成功后立即勾选；用大小写、全角或多余空白的等价名称再建，
   应复用同一标签。再让 Agent 分析相近主题网站，检查它优先复用已有近义标签。
2. 重新分析一个 YouTube `watch`、短链或 `shorts` 视频；即使当前网络无法读取 YouTube 页面，也应显示
   YouTube 官方图标和对应封面。无效视频 ID、相似恶意域名及普通网站不得套用该平台回退。
3. 在宽屏网址库切换“居中/铺满”，确认当前项使用主题绿色高亮，另一项保持中性。
4. 从 Next 同源页面发起较长 Agent 回答，观察正文和 reasoning 是否逐块出现；不要以直连 8100 的 SSE
   代替页面验证，因为本次修复针对的正是 Next rewrite 压缩层。
5. 新增一个需较长分析的网站并保持详情页打开；超过旧 32 秒后仍应继续有界刷新并最终显示真实终态。
   切到其他标签页再返回应立即补读，完成后不应继续轮询。

---

## Q24 · Agent 创建/批量加入 Space 与 Space 弹窗复用

状态: 已完成（`a272a0c`），待用户手动验收
对应: 2026-07-29 用户反馈 Agent 不能主动创建 Space、多个网站需要逐个确认且版本冲突，
以及 Space 新建弹窗位于左上角

本轮交付：

- 新增 `propose_space_batch`，一张草稿可表达纯创建空 Space、创建并加入多个网站，或向已有 Space
  批量加入。调用前仍要求模型读取 Space 和网址库的真实数据；旧 `propose_space_membership` 的
  工具 schema 与运行时都禁止 `add`，仅保留已写入成员的移出操作，不能再生成逐网站加入卡片。
- 新增 `POST /spaces/member-batches`。服务端先校验所有 site ID 均属于当前账号，再在一个事务中创建
  Space 或领取一次 `space.version`，按稳定顺序写入全部缺失成员；任何失败整批回滚。已有 Space
  版本已变化但全部请求网站都已是成员时按重放成功，否则返回 409。新建与已有 Space 两种模式都必须
  携带本轮服务端命名空间 `toolCallId` 作为 `operation_id`；服务端用不可变回执绑定账号、规范化载荷、
  目标、首次选择的网站和完整响应。相同操作与相同载荷可安全重放，不同载荷明确返回幂等冲突；创建模式
  另以 `uuid5(user_id + operation_id)` 生成确定性 Space ID，因此包括空 Space 在内都能安全重放，而同名
  但不同操作的 Space 仍严格冲突。
- 前端只渲染一张批量任务卡和一个确认按钮。候选可逐项剔除、恢复或全部恢复；create 模式剔除为空
  仍可只创建 Space，existing 模式为空时禁用确认。任意后续 Space 批量草稿都会冻结此前未完成卡片，
  防止旧方案继续执行。两种模式确认都只发一次 `target + site_ids + operation_id`，成功记录
  `space_batch_applied + space_id + site_ids`，并刷新首页网址和 Space 快捷入口。业务写入已成功但会话
  marker 同步失败时，卡片重试只补 marker，不会再次执行批量写入。批量卡仅在所属 assistant 消息真实
  落库并标记 `turnPersisted=true` 后启用；未完整持久化的回答不会留下可执行写入口。
- 最新未确认 Space 草稿会以严格字段白名单、长度限制和账号作用域来源存成服务端 artifact，下一轮模型
  能理解“不要 Bitcoin Forum”并用剩余完整清单生成替代草稿。每次工具结果都会写入最新状态；`noop`、
  `rejected` 和畸形结果也会留下 tombstone。历史与确认接口只认最新合法状态，且确认必须绑定原会话、
  草稿目标、候选子集和真实成员状态；它一旦确认就不再回放，也不会向前复活更旧方案。Provider 原始
  工具 ID 以本轮 assistant message ID 加命名空间，兼容模型跨轮重复 `call_1` 也不会碰撞。动态资源名称
  仅以明确标注的低权限事实回放，不会提升为模型 `SystemMessage`。通用工具结果仍不进入模型历史。
- `SpaceDialog` 直接复用 `LibraryDialog`。新建、重命名、删除、移出四类弹窗共享视口居中、160ms
  进退场、遮罩/Escape、原生 close 防御与写入期间关闭锁；删除旧的平行 `.space-dialog*` 样式。
  共享弹窗额外记录遮罩 `pointerdown` 起点，避免从表单内拖到遮罩松开时误关闭未保存内容。

本机 `0013` operation receipt 迁移已应用。按用户要求，本轮未运行测试、lint、构建、浏览器自动化或
真实 Provider 请求；只做源码交叉审查、`git diff --check` 与服务 readiness 检查。页面交互和真实模型
工具选择由用户手动验收。

用户手动验收：

1. 让 Agent “创建一个论坛 Space”，确认出现可一次确认的空 Space 草稿并成功创建。
2. 让 Agent “创建论坛 Space 并加入这 5 个网站”，以及“把这些网站加入已有 Space”；每轮只能出现
   一张任务卡、一个确认按钮和一次整体写入，不得再出现第二项开始全部版本冲突。
3. 在卡片内剔除、恢复候选后确认，实际成员必须等于最终选择。另发“不要 Bitcoin Forum”，应生成
   只缺该项的新草稿，所有旧的未完成 Space 批量卡必须显示已被替代且不可执行。
4. 对同一卡快速双击，或模拟响应超时后重试；不得重复成员或创建第二个 Space。刷新页面或从另一标签页
   重放必须返回同一结果；未完整持久化的 Agent 回合所带卡片必须不可执行。
5. 打开 Space 页的新建、重命名、删除、移出弹窗，检查视口居中、背景遮罩、进退动画、Escape 与
   点击遮罩关闭；提交进行中所有退出路径都应被锁定。

---

## Q25 · Chrome / Edge 原生 Space 标签分组

状态: 已完成（`a272a0c`），待用户侧载扩展并手动验收
对应: 2026-07-29 用户截图反馈 Space 只能打开 1/3，并要求自动调用 Chrome 或 Edge 标签分组，
组名使用 Space 名称

根因与交付：

- 原实现循环调用普通网页 `window.open()`。Chrome/Edge 的一次用户激活通常只允许一个弹窗，因此“允许
  弹窗后重试”只能降级补救，无法赋予网页原生标签组权限。截图中“拦截 1 个但剩 2 个”也符合旧的
  当前标签模式：后两项先尝试为一成一败，第一项为了保留重试机会尚未跳转，并非后端漏成员。
- 新增无依赖 MV3 `apps/browser-extension`，同一目录可分别侧载到 Chrome 与 Edge。content script 只匹配
  `localhost` / `127.0.0.1`，通过版本化 `postMessage` envelope 桥接；service worker 再严格验证来源页、
  request/operation ID、Space ID/名称、URL 协议、去重结果和数量，使用来源 `sender.tab.windowId`，不会
  把组创建到其他浏览器窗口。扩展不读取网页正文、Cookie、历史或任意现有标签 URL。
- 扩展先以 `active:false` 创建全部标签；任一创建失败会尽力关闭本轮已建标签。全成后一次执行
  `tabs.group`，再用 `tabGroups.update({title: spaceName, color: "green", collapsed: false})` 命名，最后
  激活首个标签。网页不能跨进程控制另一个浏览器，因此 Chrome/Edge 各自安装、各自执行。
- 网页共享 `OpenAllDialog` 会先 PING 助手。已连接时主按钮走原生分组；未连接时明确显示状态并保留旧的
  普通新标签/当前标签降级，不再把降级说成自动分组。首次操作 ID 在响应不确定时保持不变；扩展在首个
  浏览器副作用前保存不含 URL 的 pending 回执。每个新标签先加载扩展自有 `pending.html`，由该页独立登记
  tab ID 后再导航到目标网址，完成后替换为哈希结果回执；这覆盖 Worker 在 `tabs.create` 回调缝隙中断的
  恢复。操作 ID 短期跨刷新保存，未决检查完成前和超时后均禁用普通降级，只允许同操作重试；不同页面
  任务全局串行、最多积压 3 项。同载荷并发合并后会为每个 operation ID 分别写 alias 完成回执；自登记、
  恢复、过期清理和结果落盘按 operation hash 共用锁。Worker 中断后逐个清理已登记标签，同操作同载荷
  完成后直接重放结果，异载荷明确冲突。网页端再以 Web Locks 串行同一 Space 在多个 WebHub 标签页中的
  任务预留和清理，未确认记录会保留原 Space 名称、规范化 URL 和任务创建时间；Space 后续改名或增删成员
  时先恢复旧载荷，不再卡在载荷冲突。未确认身份和完成回执统一保留 7 天；扩展使用本地持久化的浏览器
  启动轮次，扩展 reload/update 时保持不变，只在 `runtime.onStartup` 时轮换，所以既能清理扩展重载前的
  真实 pending 标签，也不会用跨浏览器启动后可能重新分配的旧 tab ID 删除普通标签。完成回执重放前会
  核对同名绿色组和标签数量，任何查询异常都保留回执并暂停恢复；恢复 alias 只匹配本任务开始之后产生
  且仍实际存在的同载荷完成回执。若浏览器恰在创建中途退出且旧启动轮次已有 tab 副作用，扩展返回
  `CROSS_SESSION_PENDING`，不会在无法证明旧 tab ID 归属时自动删除或静默重建。
- 前端与扩展均设单次 100 个标签硬上限。旧实现最多拉 5 页共 500 条，游标仍有下一页时却静默当成
  “全部”；现改为读取安全上限内的完整集合，成员数或响应游标超限均明确拒绝，不会把数百上千页面同时
  推给浏览器造成卡死。

按用户要求，本轮不运行测试、lint、构建、浏览器自动化或页面检查；只做源码静态复核、manifest/契约
扫描、`git diff --check` 和现有服务存活确认。页面与真实浏览器扩展由用户手动验收。

用户手动验收：

1. Chrome 打开 `chrome://extensions`，Edge 打开 `edge://extensions`，开启开发者模式后分别“加载已解压的
   扩展程序”，选择 `F:\AI\AgentMake\AgentProjects\WebHub\apps\browser-extension`，再刷新 WebHub。
2. 打开含 3 个网站的“论坛网站” Space，弹层应显示浏览器已连接；点击“在浏览器分组打开”，一次生成
   3 个标签，并出现名称严格为“论坛网站”的绿色展开组，首个标签被激活。
3. 停用扩展并刷新 WebHub，弹层应显示未连接；普通新标签仍可使用，但如被拦截必须如实显示，不能宣称
   已分组。重新启用扩展后刷新，状态恢复。
4. 对同一操作模拟页面等待超时后重试，不应建立第二个组；改变载荷但复用 operation ID 应明确冲突。
5. 成员超过 100 个的 Space 应在任何标签创建前明确拒绝，不能只打开前 100/500 个或让浏览器失去响应。
6. 在两个 WebHub 标签页同时对同一 Space 点击分组打开，只应生成一个组；浏览器完全退出重开后再次执行，
   不得误关任何已存在的普通标签。

---

## Q26 · Agent 回合可靠性、真实来源与交互动效收口

状态: 已完成（`829d824`），`0014` 已应用且全仓自动门禁通过，待用户手动验收
对应: Vercel AI SDK 与 React Bits 稳定性优先优化

范围与顺序：

1. 为每个 Agent 用户回合增加账号级稳定 `turn_id` 与持久回执；Provider 调用前写入用户消息和
   `streaming` Assistant 占位，流中有界检查点，最终明确收口为 `complete/error/aborted`。相同载荷
   重放已存快照，活跃回合拒绝并发重复调用，异载荷复用同 ID 明确冲突；以短租约回收过期回合。
2. 将受信 Tavily、Jina、Exa 工具结果转换为 AI SDK 原生 `source-url`，按规范化 HTTP(S) URL 去重并
   持久化。前端以可折叠来源区展示证据，现有网站推荐卡继续负责操作，模型正文不得伪造来源。
3. 历史恢复完整呈现四种消息状态；未完成 Assistant 回合中的所有业务草稿禁用。对话滚动改为
   stick-to-bottom：用户向上阅读时停止跟随，提供返回底部按钮，发送时只主动滚动一次。
4. 工具阶段整理为可恢复的任务时间线；reasoning 流中显示本地计时，结束后使用服务端耗时；仅展示
   Provider 实际返回的 Token 分项。增加复制回答与复制链接，不开放重新生成。
5. 将旧 `LoaderCircle` 加载态统一到共享 Spinner，集中动效样式、补齐 reduced-motion / forced-colors
   与动态计数读屏语义，并记录 React Bits 上游来源和本地改动。

本轮明确不做：真正跨刷新续流、AI SDK 原生 tool approval 迁移、任意网站 WebPreview、附件、语音、
模型选择器、费用估算、在大型网址/Space 列表加入高成本逐项动画，以及不具备幂等安全保证的重新生成。

验收边界：协议、前端契约、后端回合与跨模块回归测试均已运行；根级 `pnpm check` 覆盖 lint、
TypeScript、前后端全量测试和 Next.js 生产构建。浏览器自动化与真实 Provider/搜索请求仍未执行，
页面功能继续由用户手动验收。

本轮交付：

- Web 端以本轮用户消息 ID 作为稳定 `turn_id`；服务端新增账号级 `agent_turn_runs`，以请求摘要、
  60 秒租约和 15 秒心跳围住一次 Provider 执行。新旧客户端兼容，同 ID 同载荷只执行一次，运行中
  返回明确状态，完成/错误/中止均重放已保存快照，异载荷复用同 ID 明确冲突。
- 用户消息、`streaming` Assistant 占位和回合绑定在 Provider 前以同一事务写入；正文与 reasoning
  最多每两秒检查点一次，工具结果与可信来源立即落库，终态把消息和执行回执原子收口。启动和请求入口
  会围栏过期回合；单条异常不会阻塞其他账号或后续回合，旧执行器也不能越过新租约覆盖消息。删除会话
  前解除回执的会话/消息外键并保留最小 tombstone，完成态和执行中回合的延迟首轮重试仍只重放旧结果。
- 只有 Tavily、Jina、Exa 受信工具结果可提升为 AI SDK `source-url`；服务端过滤非公开 HTTP(S)、
  敏感查询参数并按规范化 URL 去重，以稳定哈希生成 `sourceId`，同时持久化标题和实际 Provider。
  前端折叠展示来源证据，推荐网站仍使用独立站内/站外操作卡。
- 历史恢复 `streaming/complete/error/aborted` 四态；非完整回答永久禁用写入草稿。对话改为底部跟随，
  用户上滚后停止自动拉回并显示返回底部按钮。工具时间线、reasoning 计时、Provider 真实 Token、复制回答
  和复制会话链接均已接通；分享链接从 `origin + pathname` 构造，只保留受控 `c` 参数并清除原 query/hash。
  停止或异常会移除未完成步骤，不再残留“正在执行”。未完成回答的站外推荐
  仍可打开查看，但“收录”永久禁用；占位前极早停止也会立即关闭回执并补本地中止状态。
- 已通过账号授权的 Slash 命令解析/元数据错误也进入同一回执：同载荷重放、异载荷冲突且不调用
  Provider。不能证明归属的会话 ID 仍停在授权边界外，不会为了记录错误而写入其他账号会话外键。
- 旧加载图标统一为共享 `Spinner`，React Bits 动效样式集中到 `motion.css`，补齐 reduced-motion、
  forced-colors 和动态计数读屏语义；组件源码旁记录上游地址、提交和本地适配。
- 完整门禁发现 ingestion worker 永久保存裸数据库对象 ID 会在 Python 复用 ID 后误判新实例已停止，
  并可能让 TestClient 关闭阶段挂起；当前改为弱引用与严格对象身份围栏，并补确定性回归测试。
- 严格全量门禁继续定位到自动回填关停时可能取消尚未入池的 aiosqlite 连接初始化；consumer 现在以
  shield + drain 排空 discovery/session 清理后再传播取消，并在查询返回后重查停止状态，避免连接在
  事件循环关闭后才由 GC 回收。新增确定性取消回归测试。
- 根级 `pnpm check` 已通过：前端 175 项、后端 561 项、两端 lint、TypeScript 与 Next.js 生产构建
  全绿；后端全量同时在未处理线程异常视为错误的严格模式下通过。
  `20260729_0014` 已升级到 head，API/前端已重新启动并确认监听 `8100/3100`；浏览器与真实
  Provider/搜索请求仍由用户验收。

P2 不阻塞 Q26：CountUp 节流数字过渡已完成；书签/Provider Stepper 与短时 AnimatedContent 尚未实施，
等 P0/P1 经用户页面验收稳定后再做，不能用定时假进度代替真实任务状态。

用户手动验收：

1. 在长回答流中向上滚动，确认页面不再强制拉回；点击返回底部后恢复跟随。
2. 点击停止并刷新会话，确认部分正文、思考、来源与“已停止”状态仍存在，所有业务草稿不可执行。
3. 对同一次请求模拟响应丢失或并发重试，确认不重复创建会话、消息或调用 Provider；新问题仍是新回合。
4. 开启 Tavily、Jina 或 Exa 后询问联网问题，确认折叠来源只含真实搜索结果，标题、域名、
   本地图标回退和外跳正常；搜索证据不得被当作目标站 favicon 来源。
5. 检查工具步骤完成后折叠、思考秒数与 Provider 实际 Token；缺失用量字段不显示估算值。
6. 检查认证、网址库、Space、批量补全、自动加载和语义索引等加载态，以及系统 reduced-motion / 高对比模式。

---

## Q27 · Provider 预检精确诊断与 Fake-IP 防复发

状态: 已完成 · c68022b
对应: Provider 可靠性与 Q26 Agent 错误回执收口

范围与约束：

1. 保留 TUN、Fake-IP 模式与现有 SSRF 拒绝策略，不允许把 `198.18.0.0/15` 当成公网地址放行。
2. 将模型 Provider 的“确实未启用、配置不完整、密钥不可解密、代理 Fake-IP、目标被安全策略阻止、
   DNS 解析失败/超时”拆成固定错误码与固定中文文案；不得转发厂商异常文本、Base URL、响应体或密钥片段。
3. 同一分类必须贯通设置页连接测试、Agent 流式错误、持久 Assistant metadata、同 `turn_id` 重放和
   单站/批量分析的停止原因。搜索/embedding 仍是可选能力，失败时不得反向阻塞不需要它的模型回合。
4. 前端只对已知 Provider 预检错误提供设置页操作入口，未知运行时错误保持通用提示，不制造错误指引。

本轮交付：

- Provider DNS 校验识别 RFC 2544 `198.18.0.0/15` 并返回 `provider_fake_ip_detected`；连接测试在任何
  HTTP 请求发出前终止。该专用分类只用于 Provider 连接校验，不改变 favicon/网页资源抓取边界。
- Agent 新增六类 `AgentProviderError` 固定安全契约；密钥解密失败不再伪装成“未配置”，普通私网/保留
  地址仍由 SSRF 阻止。异常对象不保留调用方文本，响应和持久回执只保存固定 code/message。
- LangGraph 终态与历史重放按持久错误码恢复对应安全文案；同一失败 `turn_id` 重放不会再次解析 DNS、
  解密密钥或调用 Provider。详情分析沿用同一固定安全原因。
- Agent 页面为配置、密钥、Fake-IP、目标受阻与 DNS 不可用显示不同操作文案，统一进入 Provider 设置页；
  未知错误码不显示误导按钮。
- 等价全仓门禁分拆执行全绿：前端 177 项、后端 569 项、两端 lint、TypeScript 与 Next.js 生产构建
  通过。根级组合命令受当前执行器约 124 秒硬超时中止；后端全量单独完成，569 项耗时 249.31 秒。

用户手动验收：

1. 在可控 Clash 订阅中临时缺少目标域名的 `fake-ip-filter`，确认设置页连接测试和新 Agent 回合明确
   显示 Fake-IP 诊断，而不是“未配置”；检查该请求没有到达模型服务商。
2. 将规则放入全局 Merge，保存并应用后确认 DNS 返回真实公网 IP，新 Agent 回合恢复；刷新旧会话时
   旧失败回执保持原错误且不会自动重试 Provider。

---

## 全量门禁（每轮提交前必须全绿）

```bash
cd apps/web && npx tsc --noEmit && npx eslint . && node --test && npx next build
cd services/api && uv run pytest -q && uv run ruff check .
```

基线：前端 177 测试 / 后端 569 测试。新增功能必须带测试，基线只能涨不能降。

## 不可动摇的约束

- 绝不硬编码任何 LLM 供应商或 API Key；模型访问一律走 per-account 的 providers 模块
- 完整密钥绝不进前端 state、日志、聊天记录或普通数据导出；对外只给 `SECRET_MASK`
- 厂商/供应商异常文本绝不透出到客户端（可能含 URL、请求体、凭据片段）
- 所有 Agent 工具强制服务端账号作用域；Agent 的指令永远不能替代授权
- Agent 不得直接写库，写操作一律 propose → 人工确认
- 非 Ollama 的 Provider base URL 必须 HTTPS 且过 SSRF 校验
- 不推送 GitHub
