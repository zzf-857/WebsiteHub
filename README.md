# WebHub

WebHub 是一个 Agent First 的个人网站知识中枢。它以浏览器网站的形式运行，用户可以通过自然语言收录、检索和管理网站，并使用分类、标签与 Space 组织自己的网址资料库。

## 当前状态

项目正在实施 `Phase 1 - Identity & Account Foundation`：

- Next.js 16 网站与 FastAPI 服务骨架
- 稳定 Web 路由、站点 Header 和正常网页文档流
- Agent 首页、Slash Command 注册入口和搜索范围切换
- 浅色、深色、跟随系统主题基础
- 局域网账号注册、登录、退出与 Cookie 会话闭环
- 账号身份、主题偏好与跨设备持久化已接入 FastAPI
- Next.js 到 FastAPI 的同源代理与 LAN Origin 校验
- 浏览器书签流式解析、磁盘暂存预览与内置导入 Skill
- pnpm、uv 锁文件与整仓自动检查

账号基础闭环已可用；密码管理、Provider 配置、真实聊天和资料库 CRUD 将按 [实施计划](./IMPLEMENTATION_PLAN.md) 的后续纵向阶段逐步接入。

## 文档

- [产品需求文档](./PRD.md)
- [正式实施计划](./IMPLEMENTATION_PLAN.md)
- [浏览器书签导入 Skill](./skills/import-browser-bookmarks/SKILL.md)

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

## 环境变量

- 网站服务端变量样例：`apps/web/.env.example`
- 全局部署变量参考：`.env.example`

默认配置不需要创建环境文件即可启动。需要修改内部 API 地址时，在 `apps/web/.env.local` 中设置 `WEBHUB_API_INTERNAL_URL`，并使用同一配置重新执行 `pnpm build` 后再启动生产网站。网站入口默认允许当前机器名、loopback 和启动时检测到的局域网 IP；自定义域名或别名通过 `WEBHUB_ALLOWED_HOSTS` 显式增加。
