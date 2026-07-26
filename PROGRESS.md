# WebHub 当前进度快照

**这份文件必须每轮迭代更新。** 它的唯一职责是让任何新会话（丢上下文、换账号、换 Agent）
在 3 分钟内知道「现在能跑什么、不能跑什么、下一步做什么」，而不必翻 commit 或读代码。

- 调度看 `ITERATION_QUEUE.md`（唯一入口，从上往下取第一个「待做」）
- 本机环境看 `LOCAL_DEV.md`（不进 Git：固定账号、库位置、测试素材）
- 设计与架构决策看 `IMPLEMENTATION_PLAN.md`（第 10 节只指向本文件，不再重复状态）

最后更新：2026-07-27 · 对应 commit `f3ca0b9`+

---

## 一句话状态

Provider 配置、Agent 增改查、书签导入、网页抓取四条链路**已跑通**；
批量入库、自定义排序、RAG、视觉收尾尚未开工。

测试基线：**前端 124 / 后端 368**。只能涨不能降。

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

## 不能跑的

| 缺口 | 说明 |
| --- | --- |
| LLM 分类**未接线** | `bookmarks/classifier.py` 已完成且有测试，但没有入口把结果落到 Site 上 |
| 批量 URL 入库（Q5） | `ProposeSiteArgs` 只收单个 url，无批量端点 |
| Site 自定义排序（Q6） | `Site` 没有排序列，全站无拖拽 |
| Space 一键全开（Q7） | 只在首页且只覆盖最近 8 个，重试会重复开标签 |
| 检索与 RAG（Q8） | LlamaIndex 依赖都没装，`embedding` Provider 槽位是死预留 |
| 视觉收尾（Q9） | 1c 紧凑态 3 处偏差、`globals.css` 有死代码 |
| 结构收尾（Q10） | `persistence.py` 1864 行、两个 >1000 行的组件待拆 |

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

## 环境上必须知道的三件事

1. **Provider 主密钥**：开发环境首次使用自动生成，落在 `.data/provider-master.key`。
   **弄丢它，所有已存的 API Key 永久无法解密。** 生产环境仍要求显式设
   `WEBHUB_PROVIDER_MASTER_KEY`。配置了但格式错误时 fail closed，绝不顺手生成新的。
2. **`.env` 现在真的会被加载**（`config.py` 导入时 `load_dotenv`，`override=False`）。
   在此之前 `.env.example` 里每一项都是哑弹——查配置问题时别再假设它没生效。
3. **内嵌 Browser 面板与用户真实 Chrome 是两套 cookie。**
   「我明明登录了你怎么看不见」十有八九是这个。

---

## 下一步

1. 给 LLM 分类接线：做成对整个资料库通用的重分类（用户要的是「所有网站」，
   不只是导入的），`propose_reclassify` → 带成本预估的确认草稿 → 确认后才花 token。
   `estimated_request_count` / `estimated_input_characters` 已备好。
2. 之后按 `ITERATION_QUEUE.md` 顺序走 Q5 → Q10。

### 花钱的决策（需要用户点头，别自作主张）

- 规则分类器够不着的那批（真实数据里 2024 条中有 538 条「未分类」）交给模型处理，
  会消耗用户自己的 Provider 额度。成本控制已做在结构里（按簇提问、封闭分类法、
  只发主机名不发 URL、few-shot 收敛），但**开跑前必须让用户看到预估请求数**。

---

## 维护约定

每轮迭代提交前，连同队列条目一起更新本文件的：一句话状态、测试基线、
「能跑/不能跑」两张表、下一步。**只写验证过的事实**：
「实测通过」和「代码写完了」必须分开写，后者不算能跑。
