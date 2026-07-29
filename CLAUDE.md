# WebHub — Claude 接手指南

Agent 主导的个人网址导航站。任何新会话（换账号、换机器、上下文丢失）按本文件即可无缝接手，
不依赖任何聊天记录。

## 接手三步

1. 读 `ITERATION_QUEUE.md` —— **唯一调度依据**。从上往下找第一个「状态: 待做」的条目，只做那一个。
2. 读 `PROGRESS.md` —— **当前状态快照**（能跑什么、不能跑什么、刻意没做什么）。
   这份必须每轮更新；`IMPLEMENTATION_PLAN.md` 只是 2026-07-26 冻结的历史架构基线，不能用于当前调度。
3. `git log --oneline -15` —— 每个 commit 都有详细中文说明，是最可靠的历史。

**动手前先读 `LOCAL_DEV.md`**（本机专用，已 gitignore，不进仓库）：它只保留无敏感值的入口，
并指向 `F:\AI\AgentMake\temp\WebHub` 下的敏感隔离说明；本机账号、凭据、是否允许新建测试账号、
库位置、重新播种命令和测试素材都只在隔离说明中维护。另需注意「内嵌 Browser 面板与用户真实
Chrome 是两套 cookie」这个坑。

需求原文在 `F:\AI\AgentMake\AgentProjects\00_Todolist\WebHub_Todolist\todolist.md`；
最新设计稿（唯一视觉真源）在 `Arts/handoff/untitled/project/WebHub 效果图.dc.html`（6 块画板 1a–1f，
1f 是规范板：色彩/字阶/圆角/图标映射/动效，全部设计令牌都从它来）。

## 运行与验证

```bash
pnpm dev          # web 3100 + api 8100（concurrently）
```

全量门禁（每次提交前必须全绿；测试基线只能涨不能降，当前数值只以
`ITERATION_QUEUE.md` 的「全量门禁」为准，避免多处维护后漂移）：

```bash
cd apps/web && npx tsc --noEmit && npx eslint . && node --test && npx next build
cd services/api && uv run pytest -q && uv run ruff check .
```

注意：删过 `app/` 下的 page 之后，`tsc` 可能因 `.next/types` 陈旧报错——
先删 `apps/web/tsconfig.tsbuildinfo` 或跑一次 `npx next typegen` 再判。

## 不可动摇的约束（详见 ITERATION_QUEUE.md 文末）

- 绝不硬编码任何 LLM 供应商或 API Key；模型访问一律走 per-account 的 providers 模块
- 完整密钥只回掩码 `********`；厂商异常原文绝不透出到客户端
- Agent 工具强制服务端账号作用域；Agent 不直接写库，一律 propose → 人工确认
- 非 Ollama 的 base URL 必须 HTTPS 且过 `providers/targets.py` 的 SSRF 校验
- 提交用中文 commit，**不推送 GitHub**
- 前端只用 `apps/web/app/globals.css` 顶部的设计令牌，禁止写死颜色；圆角全站 ≤8px；
  图标只用 lucide-react（1.26.0，无 Loader2/AlertCircle/StopCircle）；favicon 不走第三方 CDN

## 本机环境陷阱（重要）

- 用户用 ccswitch 切换账号配置。当 `~/.claude/settings.json` 的 env 把模型路由到 DeepSeek 时，
  **子代理/Workflow 会报「模型不存在」**——解决办法：每个 `agent()` 调用显式带 `model: 'fable'`。
  不要去改用户的本地配置。
- 权限分类器也可能走同一路由；它临时不可用时 Bash 会被挡，改用只读工具（Read/Grep/Glob）等一会再试。
- Claude Code 的会话转录按项目目录存在
  `C:\Users\admin\.claude\projects\F--AI-AgentMake-AgentProjects\*.jsonl`，与账号无关。
  换账号后在项目目录跑 `claude --resume` 即可恢复历史会话；若 ccswitch 换了整个 `~/.claude`，
  把旧 profile 里的 `projects\F--AI-AgentMake-AgentProjects\` 目录拷回同路径即可。

## 后端速查

- FastAPI 在 `services/api/src/webhub/`；前端走 `/api/backend/*`（next.config.ts 重写到 `127.0.0.1:8100/api/*`）
- 所有请求模型 `extra="forbid"`：多传一个字段就 422
- providers 的 secret action 三处不同：创建 `write` / 更新 `replace|clear` / 测试 `test`
- 每 (user_id, kind) 至多一个启用的 ProviderConfig（唯一索引）；Agent 每轮用
  `agent/provider_binding.py` 解密该账号自己的 Key
- 会话历史回放用 `chat/service.py` 的 `list_recent_messages`（倒序取尾），不要用 `list_messages` 的第一页
