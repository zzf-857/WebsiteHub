# WebHub 当前进度快照

**这份文件必须每轮迭代更新。** 它的唯一职责是让任何新会话（丢上下文、换账号、换 Agent）
在 3 分钟内知道「现在能跑什么、不能跑什么、下一步做什么」，而不必翻 commit 或读代码。

- 调度看 `ITERATION_QUEUE.md`（唯一入口，从上往下取第一个「待做」）
- 本机环境看 `LOCAL_DEV.md`（不进 Git：固定账号、库位置、测试素材）
- 设计与架构决策看 `IMPLEMENTATION_PLAN.md`（第 10 节只指向本文件，不再重复状态）

最后更新：2026-07-27 · Q14（3d7ddd7）代码与自动化门禁完成，页面功能待用户手动验收

---

## 一句话状态

Provider 配置、Agent 增改查、书签导入、网页抓取、批量入库、自定义排序、Space 一键全开、
混合检索、LLM 全库重分类九条链路**已建成**。Q14 已在代码层完成设置页分区、真实 favicon /
页面预览抓取、已收录网站站内详情导航，以及 Agent 流式 Markdown、可折叠 reasoning 与真实
Provider usage 展示。抓取队列同时补齐全局并发/容量、同站点去重、关停回收和旧任务防覆盖。
**本轮未操作浏览器、未调用真实 Provider、未触发 2027 条历史补全，页面验收由用户执行。**

测试基线：**前端 159 / 后端 503**。只能涨不能降。

---

## 能跑的（已实测，不是「代码存在」）

| 能力 | 状态 | 实测证据 |
| --- | --- | --- |
| Provider 配置页 | ✅ | 浏览器实测：三分区、掩码、启用互斥 |
| 连接测试 + 拉模型列表 | ✅ | 真实 DeepSeek：「连接成功，读取到 2 个模型」 |
| Base URL 自动预填 | ✅ | 选 DeepSeek 自动填官方地址 |
| 模型名下拉选择 | ✅ | 拉取后渲染 select，可切手填 |
| Agent 对话 + 站内检索 | ✅ | 真实模型回话，来源标注正确 |
| Agent 收录网站（propose→确认） | ✅ | 确认前 0 条 → 确认后 1 条 |
| Agent 改网站（改名/分类/标签/置顶） | ✅ | 只把真变化的字段进 diff |
| Agent 移入移出 Space | ✅ | 不存在的 Space 如实拒绝，不凭空建 |
| 确认结果回写会话 | ✅ | 同一对话「收录→接着改」一次走通 |
| 书签解析 + 去重 + 预览 | ✅ | 真实 2541 条 → 2024 候选，1.08 秒 |
| 书签落库 | ✅ | created 2024 / failed 0，0.47 秒 |
| 书签导入前端入口 | ✅ | `/settings/import`，浏览器实测 |
| 书签导入 Agent 工具 | ✅ | 三件套 + skill 已写成真契约 |
| 网页元数据抓取 | ✅ | 建站后自动分析 + 详情页「重新分析」，SSRF 每跳复验 |
| 批量 URL 入库 | ✅ | URL 由代码抽取；预览只读、确认后逐项独立提交 |
| 分类内自定义排序 | 后端已测 | 迁移已在实库跑过并回填；**界面未浏览器实测**（API 进程当时已停） |
| LLM 全库重分类 (Q12) | 真实 Provider + Chrome 已测 | 2027 个网站总数不变，851 个网站各更新一次（`version_sum` 2029→2880）；分类内 position 零空值、零重复，标签、Bookmark、analysis、source、provenance 均不变 |
| 分类图标 | 真实 Chrome 已测 | 首页分类与网站卡片均渲染实际 Lucide 图标（含 AI 分类的 `Bot`）；按名称推断链路已生效，**没有前端选择器**，用户暂不能手选 |

## 本轮代码完成、待用户页面验收

| 能力 | 自动化证据 | 用户需手动检查 |
| --- | --- | --- |
| Provider / 语义索引分区 | 路由、契约、费用估算测试 + TypeScript + 构建通过 | Provider 页不再出现书签/索引信息；账号菜单可进入独立语义索引页 |
| favicon 与预览抓取 | SSRF、DNS 固定、重定向、MIME、大小、图片签名、并发与编辑竞态测试通过 | 新增或改 URL 后真实图标是否逐步出现；无图标时是否稳定回落默认图标 |
| 站内网站详情 | 首页、资料库、Agent 导航源码回归 + 动态路由构建通过 | 各类已收录卡片先开站内详情；只有“访问官网”按钮外跳；预览失败无空占位 |
| Agent 流式富文本 | UI Message Stream、reasoning、usage、历史恢复、Markdown 链接过滤测试通过 | 真实模型流式打字、reasoning 展开/收起、耗时/Token、表格/列表/代码及链接卡片显示 |

## 不能跑的

| 缺口 | 说明 |
| --- | --- |
| 两处 observer 未在浏览器验证 | Q6 拖拽持久化 / Q9 的 `data-stuck`·`data-compact` 触发时机，见下方「环境陷阱」 |
| Q14 页面功能未手动验收 | 本轮按用户要求只做代码与自动化门禁；上表四项需用户在真实浏览器逐项检查 |
| 自定义兼容 Provider 无 Token 用量 | `openai_compatible` 是否接受 `stream_options.include_usage` 无法自动判断；当前只对内置 OpenAI / DeepSeek 开启，需增加显式能力开关 |
| 远程 favicon / preview 未代理缓存 | 出站抓取已安全校验，但页面仍直接加载远程图片；完整修复需要账号鉴权的图片代理、DNS/重定向复验、大小/MIME 校验、缓存与 SVG 策略 |
| 分类图标不能手选 | 只有后端按名称推断，没有前端选择器；契约层 `icon` 已就绪，缺的是一块 UI |
| Agent 聊天里确认导入 | `propose_bookmark_import` 后端出草稿，但前端无契约/无确认通道；只能去 `/settings/import` 手动 apply |


---

## 刻意没做的（**别当成 bug 去"修"**）

这几处是权衡后的决定，不是遗漏。要改请先读理由：

1. **不填 `bookmark_source_occurrences` / `site_import_origins`。**
   这两张表在 schema 里但全仓库无人写，「哪条书签变成哪个网站」的溯源链整体未建。
   半填一份只会造出一张看起来权威、实际只覆盖走过某个函数的导入的表。
2. **书签导入的幂等性不靠台账**，靠 `sites` 的 `UNIQUE(user_id, identity_url)`。
   重复 apply 自然全部 `skipped_existing`。别再加「是否已导入」的表。
3. **解析 worker 是进程内的**，不是 schema 预留的租约 + 心跳式。
   单用户本地应用，为一次 1.7 秒的解析配任务队列是仪式。状态机逐步照走，
   以后换真 worker 不用改 schema。
4. **搜索类厂商（tavily/jina/exa）的连接测试返回 `unsupported`。**
   它们没有只读目录接口，拿用户的 Key 做健康检查等于花他的搜索额度。
5. **向量服务分区文案写「检索链路尚未接入」**——`langgraph_runner` 确实只解析
   model 与 search。Q8 接入后再改文案，别提前吹。
6. **预览截图不做**（需要无头浏览器，成本与安全面不成比例）。
   注意区分：Q13 起 `Site.preview_url` 会存**页面自己声明的** `og:image`
   / `twitter:image`（抓取时顺带拿到，只补空不覆盖）。那不是截图，
   页面没声明就是 None。别因为有了这个字段就去接无头浏览器。
7. **`space-contract` 的 `identifier` 不并入 `contract-guards`。**
   它带长度上限且拒绝数字，共享版为兼容历史整数主键会把数字转成字符串。
   同名不等于同一件事，合并会悄悄放宽 Space 成员 id 的约束。

## 架构约定（2026-07-27 自查后确立）

模块依赖是**无环 DAG**，加新 import 前先确认方向：

```
auth →（无）        底座
bookmarks → auth
library / spaces / chat → auth (+library)
providers → auth
agent → 全部        顶层编排，只有它可以依赖所有模块
```

低层模块需要高层的类型时，**声明 Protocol 描述所需，而不是反向 import**
（见 `bookmarks/classifier.py` 的 `ModelEndpoint`）。

前端契约层的校验原语统一在 `lib/contract-guards.ts`，
各模块传入自己的错误工厂。**合并任何"看起来一样"的函数前必须先证明逐字节相同。**

---

## 环境上必须知道的四件事

1. **Provider 主密钥**：开发环境首次使用自动生成，落在 `.data/provider-master.key`。
   **弄丢它，所有已存的 API Key 永久无法解密。** 生产环境仍要求显式设
   `WEBHUB_PROVIDER_MASTER_KEY`。配置了但格式错误时 fail closed，绝不顺手生成新的。
2. **`.env` 现在真的会被加载**（`config.py` 导入时 `load_dotenv`，`override=False`）。
   在此之前 `.env.example` 里每一项都是哑弹——查配置问题时别再假设它没生效。
3. **内嵌 Browser 面板与用户真实 Chrome 是两套 cookie。**
   「我明明登录了你怎么看不见」十有八九是这个。
4. **内嵌 Browser 面板里 IntersectionObserver 和 CSS transition 都不工作。**
   面板 `visibilityState` 恒为 `hidden`、不合成帧，所以 IO 回调不投递、过渡停在起始值。
   复现：新建一个 IO 观察 `document.body`，1.5s 内零回调；给 `.app-header` 加
   `data-compact` 后 `height` 仍读到 64px，`style.transition='none'` 后立刻变 56px。
   **别把这个当代码 bug 去修。** 需要验证吸顶/懒加载/动效的触发时机，只能：
   手动置位属性验证样式那一半，或者请用户在真实浏览器里跑一遍。
   同理，`api` 挂了时页面停在「正在加载账号...」——先 `curl 127.0.0.1:8100/api/health`。
   端口被上一轮的僵死进程占住时，`Get-NetTCPConnection -LocalPort 8100` 找 pid 再杀。

---

## 下一步

**Q1–Q14 的代码项已完成。以下是尚存的验收与优化缺口，按影响排序：**

1. **用户手动验收 Q14 四项页面能力**：Provider/语义索引分区、真实 favicon 与预览、
   已收录网站站内详情、Agent 流式富文本/reasoning/真实用量。检查清单见上表。
2. **受控图片代理与缓存**：favicon / preview 不再由浏览器直连远程地址；服务端代理必须复用
   当前 DNS 固定与重定向校验，并补认证、内容类型/大小、缓存和 SVG 策略。
3. **自定义 Provider 的流式 usage 能力开关**：用户显式确认兼容端点支持
   `stream_options.include_usage` 后再启用，不能按名称猜测。
4. **两处 Observer 真实浏览器验证**：Q6 拖拽持久化与 Q9 的 `data-stuck` /
   `data-compact` 吸顶触发时机。代码已完成并有单测，但内嵌面板里 Observer 回调
   被挂起（见下方环境陷阱第 4 条），需在真实 Chrome 里配合操作验证。
5. **分类图标选择器**：后端 `icon` 字段与契约层都已就绪，只缺一块让用户手选的 UI。
6. **Agent 聊天里确认书签导入**：`propose_bookmark_import` 后端已出草稿，
   前端缺契约与确认通道；`prompt.py` 目前向模型承诺了这个能力，属虚假宣传，
   要么补前端要么改提示。
7. **大契约文件拆分**：`lib/agent-contract.ts` 已超 750 行，超出前端 700 行约定。
   它是契约模块不是组件，优先级低于上面几条。


---

## 维护约定

每轮迭代提交前，连同队列条目一起更新本文件的：一句话状态、测试基线、
「能跑/不能跑」两张表、下一步。**只写验证过的事实**：
「实测通过」和「代码写完了」必须分开写，后者不算能跑。
