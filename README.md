# WebHub

WebHub 是一个 Agent First 的个人网站知识中枢。它以浏览器网站的形式运行，用户可以通过自然语言收录、检索和管理网站，并使用分类、标签与 Space 组织自己的网址资料库。

## 当前状态

Q1-Q13 已完成，当前已建成九条主链路：Provider 配置、Agent 增改查、书签导入、
网页抓取、批量入库、自定义排序、Space 一键全开、混合检索和 LLM 全库重分类。
真实 Provider、Agent Runner、分类执行与最终入库均已接通；Q13 又补齐了重分类确认、
分类图标、favicon/preview 元数据和并发断连保护。

仍未完成的是分类图标前端选择器、Agent 聊天内确认书签导入，以及少数必须在真实
浏览器中人工验证的 Observer 交互。准确的已完成/未完成清单、实测证据和测试基线
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
