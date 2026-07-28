# WebHub

WebHub 是一个 Agent First 的个人网站知识中枢。它以浏览器网站的形式运行，用户可以通过自然语言收录、检索和管理网站，并使用分类、标签与 Space 组织自己的网址库。

## 当前状态

Q1-Q25 的代码项已完成，当前已建成九条主链路：Provider 配置、Agent 增改查、书签导入、
网页抓取、批量入库、自定义排序、Space 一键全开、混合检索和 LLM 全库重分类。
真实 Provider、Agent Runner、分类执行与最终入库均已接通；后续迭代补齐了站内详情、
真实 favicon/preview、批量管理、持久化全库回填，以及受控 LLM 分类、标签、简介和详细介绍分析。
当目标网页没有足够公开正文时，单站分析还可使用账号主动启用的搜索 Provider 补充
目标域名证据；Tavily、Jina、Exa 支持显式真实连接测试，另有无需 API Key 的受限
“Exa MCP 免费额度”供低频 Agent 对话和单站分析使用。Q22 又统一了“网址库”命名，加入
默认居中/可选铺满布局、居中弹窗动效，并把 Agent 行动流和网站推荐收敛到稳定的结构化卡片链路。
Q23 补齐了编辑表单内新建并立即选中共享标签、YouTube 子页面的官方站点图标与视频封面回退
（官方图标不可达时使用站内平台图标）、
网址库布局选择的绿色高亮、SSE 经过 Next 代理时的逐块传输，以及新网站分析完成后的有界自动刷新。
Q24 让 Agent 可以创建空 Space，或把一组已收藏网站作为单张任务草稿一次确认后原子加入；
候选可在卡片内剔除和恢复，后续方案会冻结旧草稿；新建和已有 Space 两种批量任务都以命名空间化
工具 ID 绑定不可变操作回执，响应丢失后可重放原载荷且不会重复写入。会话确认只接受真实落库的
Agent 回合，并由服务端交叉校验最新草稿、操作回执和实际写入结果。Space 的创建、重命名、删除和移出弹窗
统一复用网址编辑弹窗的视口居中、进退动画与写入期间关闭锁。
Q25 增加同一份兼容 Chrome 与 Edge 的 MV3 浏览器分组助手。助手连接后，“全部打开”会通过浏览器原生
`tabs` / `tabGroups` API 在当前窗口创建标签，并以 Space 名称建立绿色分组，不再受网页弹窗配额限制；
未安装时仍保留明确标注的普通标签页降级。单次最多打开 100 个标签，超过时直接拒绝而不静默截断。
同一 Space 的请求由跨标签页锁串行，未确认任务保留 7 天的原始名称和 URL 集合；扩展回执绑定浏览器
启动轮次，并在重放成功前核对同名绿色组及标签数量。因此响应丢失可安全重试，扩展重载不会遗留旧任务，
浏览器重启后也不会拿旧标签 ID 清理当前普通页面。

仍未完成的是分类图标前端选择器、Agent 聊天内确认书签导入，以及少数必须在真实
浏览器中人工验证的 Observer 交互。近义标签当前由 Agent 优先复用，尚未在缺少真实样本校准时
引入固定 embedding 阈值；新站分析队列彻底满或 API 进程崩溃时，本次即时 LLM 意图也尚未持久化。
Q21 的 `0012` 与 Q24 的 `0013` 迁移已在本机应用，但 Q20-Q25 尚未经过完整真实搜索和页面验收。
准确的已完成/未完成清单、实测证据和测试基线
以 [当前进度快照](./PROGRESS.md) 为准；下一项开发只从
[迭代队列](./ITERATION_QUEUE.md) 领取。

## 文档

- [产品需求文档](./PRD.md)
- [当前进度快照](./PROGRESS.md)
- [迭代队列](./ITERATION_QUEUE.md)
- [Agent 执行规范](./AGENTS.md)
- [历史架构基线](./IMPLEMENTATION_PLAN.md)
- [浏览器书签导入 Skill](./skills/import-browser-bookmarks/SKILL.md)
- [书签分类执行 Skill](./skills/bookmark-classification-operator/SKILL.md)

## 环境要求

- Node.js `24.x`
- pnpm `11.1.3`
- uv `0.11+`
- Python `3.13.x`（uv 使用 `services/api/.python-version`）

## 本地启动

首次安装：

```powershell
pnpm install
uv sync --project services/api
```

同时启动网站和 API：

```powershell
pnpm dev
```

生产模式先构建，再从同一个根入口启动网站和单 worker API：

```powershell
pnpm build
pnpm start
```

访问地址：

- 当前电脑：`http://localhost:3100`
- 局域网设备：`http://<Windows 主机局域网 IP>:3100`
- FastAPI：仅监听 `127.0.0.1:8100`，由 Next.js 同源代理访问

Windows 首次启动 Node.js 时如果出现防火墙提示，只允许受信任的专用网络。不要将当前 MVP 直接暴露到公网。

## 自动检查

```powershell
pnpm check
```

该命令依次执行前后端 lint、前端类型检查、前后端测试和 Next.js 生产构建。

## Chrome / Edge 分组助手

普通网页没有创建浏览器原生标签组的权限。首次使用需要在当前浏览器配置文件中侧载一次 WebHub 助手：

1. Chrome 打开 `chrome://extensions`，Edge 打开 `edge://extensions`。
2. 开启“开发者模式”，选择“加载已解压的扩展程序”。
3. 选择 `F:\AI\AgentMake\AgentProjects\WebHub\apps\browser-extension`，然后刷新 WebHub 页面。

Chrome 与 Edge 需要分别安装；网页不能从一个浏览器跨进程控制另一个浏览器。扩展只接受
`localhost:3100` / `127.0.0.1:3100` 的 WebHub 页面，仅允许打开最多 100 个 `http` / `https` 地址并按 Space
名称分组的固定命令，不读取标签页正文、历史记录、Cookie 或账号密钥。

## 联网搜索 Provider

搜索服务必须由用户在 Provider 设置页主动添加并启用，Agent 还需在当前对话开启“允许联网”。
Tavily、Jina 和 Exa 使用用户自己的 API Key；它们的“测试搜索（可能消耗额度）”会执行一次
最小真实查询，不是免费的只读健康检查。

“Exa MCP 免费额度”无需 API Key，连接 Exa 官方 MCP 端点。它只用于低频 Agent 对话和单站
手动分析，不参与全库批量回填；实现限制为单 API 进程并发 1、结果缓存 5 分钟、失败冷却 60 秒。
免费共享额度不承诺可用性，达到限额或用于公共部署、批量处理时应改用自己的搜索 Provider。
批量来源绝不会调用该免费 Provider；若某个批量网站的原页证据不足，只把当前网站记为受限并继续
处理整批中的其他网站。搜索词会发送给所选服务商。

网站分析只在原网页超时、受限或正文不足时搜索，单次最多取 3 条，并只接受目标域名或其子域结果。
专业 HTTP 搜索保持 8 秒截止；免费 Exa MCP 因包含会话初始化，使用 16 秒内部总预算。
搜索摘录作为低权限证据交给分类、标签、简介和详细介绍工具，不能用来伪造 favicon 或预览图。
WebHub 不会直接下载并执行任意 GitHub Skill：Skill 是工作流说明，不等同于稳定搜索数据源；
外部能力只能通过审计后、白名单内的类型化 Provider 适配器接入。

## 浏览器书签 Dry Run

Chrome、Edge、Firefox、Safari 等浏览器导出的 Netscape Bookmark HTML 可以先生成只读预览：

```powershell
uv run --project services/api python skills/import-browser-bookmarks/scripts/preview_bookmarks.py `
  <bookmarks.html> --output-dir <F:\AI\AgentMake\temp\WebHub\bookmark-import\new-preview>
```

输出目录必须是尚不存在的临时目录。该命令只解析、规范化、去重并生成分类簇，不联网、不调用模型，也不写入 WebHub 业务数据。真实书签导出可能包含私有地址和敏感查询参数，`MockData/` 已被 Git 忽略，不得提交到公开仓库。

## 书签导入 HTTP 合同

FastAPI 的公开路径固定为 `POST /api/bookmark-imports`，网站通过同源 rewrite 调用 `/api/backend/bookmark-imports`。请求体是浏览器导出的原始 HTML 字节，不使用 JSON、Base64 或 `multipart/form-data`；允许 `text/html`（可带 charset 参数）和 `application/octet-stream`。登录 Cookie、可信 `Origin` 与 16–512 字符的 `Idempotency-Key` 是必需条件。

以下示例通过网站同源代理调用已经完成的上传 API：

```powershell
curl.exe -i -X POST http://localhost:3100/api/backend/bookmark-imports `
  -H "Origin: http://localhost:3100" `
  -H "Content-Type: text/html" `
  -H "Idempotency-Key: 550e8400-e29b-41d4-a716-446655440000" `
  -H "Cookie: webhub_session=<session-token>" `
  --data-binary "@C:\path\to\bookmarks.html"
```

新任务返回 `201`，相同账号、幂等键和文件内容的重放返回 `200`；相同幂等键绑定不同内容返回 `409`。相同内容使用新幂等键仍创建任务，并通过 `same_source_warning` 提示，不静默复用。成功响应只公开 `job_id`、状态、版本、重放标志和同源提示，不返回 snapshot ID、SHA-256、临时路径或 `storage_key`。

状态读取合同为 `GET /api/bookmark-imports/{job_id}`；网站对应 `/api/backend/bookmark-imports/{job_id}`。它只返回账号自己的公开状态、`{completed,total}` 进度、公开 failure code 和时间戳，并使用 `Cache-Control: no-store`。未知任务与跨账号任务统一返回 `404`。只读预览端点已经提供：

- `GET /api/bookmark-imports/{job_id}/preview`
- `GET /api/bookmark-imports/{job_id}/preview/folders`
- `GET /api/bookmark-imports/{job_id}/preview/candidates`
- `GET /api/bookmark-imports/{job_id}/preview/occurrences`

FastAPI 上传 admission 会在读取请求体前和流式接收期间执行门禁。默认全局最多 4 个上传、同一账号最多 1 个并发上传、同一账号每 60 秒最多 6 次 admission 尝试；每账号已发布源文件与 incoming 暂存文件合计最多 2 GiB。后端始终保留至少 512 MiB 磁盘余量，并在流式接收期间每 8 MiB 复查一次。并发、频率与追踪窗口是单 API worker 的进程内状态；当前启动合同只支持单 worker，多 worker 部署前必须改为共享门禁。

网站 custom server 只对 `POST /api/backend/bookmark-imports` 使用专用流式代理，绕过 Next 的 10 MiB request clone。真实 Chrome/Edge mock 的精确字节与哈希、请求/响应头保留、12 MiB 流式上传、声明长度超限和 chunked 超限测试均已通过；其他 `/api/backend/*` 路径仍使用 Next rewrite。网站代理独立设置 512 MiB 单请求上限，FastAPI 的 512 MiB 则是必须保留的磁盘余量，两者不是同一个限制；当前没有完成 512 MiB 文件容量门禁实测。FastAPI 继续只监听 loopback，不要通过向局域网暴露后端端口绕过网站代理。

## 环境变量

- 网站服务端变量样例：`apps/web/.env.example`
- 全局部署变量参考：`.env.example`

默认配置不需要创建环境文件即可启动。需要修改内部 API 地址时，在 `apps/web/.env.local` 中设置 `WEBHUB_API_INTERNAL_URL`，并使用同一配置重新执行 `pnpm build` 后再启动生产网站。网站入口默认允许当前机器名、loopback 和启动时检测到的局域网 IP；自定义域名或别名通过 `WEBHUB_ALLOWED_HOSTS` 显式增加。
