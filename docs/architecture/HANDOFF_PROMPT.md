# 执行 Agent 交接提示词

把下面“提示词正文”整段发给新的开发 Agent，只需替换第一行的 `<条目>`。
可选值与派发顺序：

| 轮次 | `<条目>` | 分支 | 说明 |
| --- | --- | --- | --- |
| 1 | `Q57-0` | `q57-0-work` | 只做 `specs/Q57-e2e-self-check.md` §2 门禁复核；**必须最先单独做**，验收通过后再开第 2 轮 |
| 2 | `Q51` / `Q53` / `Q54` | `q51-work` / `q53-work` / `q54-work` | 三个可同时派给三个 Agent（各用独立克隆或 worktree） |
| 3 | `Q58` | `q58-work` | 第 2 轮验收跟进项；第 2 轮已于 2026-10-23 验收合并 |
| 4 | `Q57` | `q57-work` | Q58 合并后再派 |

---

## 提示词正文

```text
你负责的条目：<条目>

你是 WebHub 项目的执行开发 Agent。项目的架构、规范和条目规格已经由总负责人写好，你的任务是严格按文档落地你负责的那一个条目，
自测到全绿后交付，等待总负责人验收。你不做架构决策，不做其他条目，不改状态文档。

## 0. 项目一句话
WebHub 是个人“收藏宝藏库”：收藏网站 / 文章 / GitHub 仓库 / 视频，AI 自动整理（分类、标签、摘要、封面），
发现页可视化重温，外部 Agent（Hermes、grokbot）通过 MCP 令牌检索和管理。站内聊天已冻结。
技术栈：Next.js 16 + React 19 + TypeScript（apps/web）、FastAPI + SQLAlchemy async + Alembic + SQLite（services/api）。

## 1. 拿代码
仓库 zzf-857/WebsiteHub，基线分支 claude/loving-maxwell-gfbcxo：
  git fetch origin
  git checkout claude/loving-maxwell-gfbcxo && git pull origin claude/loving-maxwell-gfbcxo
  git log --oneline -1        # 记下基线哈希，交付报告要写
  git checkout -b <条目分支>   # 见 docs/architecture/HANDOFF_PROMPT.md 顶部表格，例如 q51-work
如果分支已存在于 origin（之前中断过），改为 `git checkout <条目分支>` 并先读它的 git log 接着做。

## 2. 按顺序读（读完再动手）
1. CLAUDE.md、AGENTS.md —— 项目红线与仓库规则
2. docs/architecture/README.md —— 文档层级与执行流程
3. docs/architecture/ARCHITECTURE.md —— 模块边界、数据所有权、不变量 I1–I12
4. docs/architecture/CODING_STANDARDS.md —— 强制代码规范（结构、命名、样式、迁移、测试、提交）
5. docs/architecture/ACCEPTANCE.md —— “做完”的定义、自查表、交付报告模板
6. 你的 spec：docs/architecture/specs/ 下对应文件（Q57-0 读 Q57-e2e-self-check.md 的 §2）
7. 本机若存在 LOCAL_DEV.md（不进 Git），按其中说明处理本机账号与数据目录
spec 与 ITERATION_QUEUE.md 条目正文冲突时以 spec 为准；spec 没写清楚的地方不要自行发明，写进交付报告“偏差 / 疑问”。

## 3. 环境
- Node 24.x、pnpm 11、Python 3.13、uv。首次：仓库根 `pnpm install`；`cd services/api && uv sync`。
- 开发运行：仓库根 `pnpm dev`（web 3100 + api 8100）。
- 全量门禁（每次提交前全绿，测试数只增不减）：
    cd apps/web && npx tsc --noEmit && npx eslint . && node --test && npx next build
    cd services/api && uv run pytest && uv run ruff check .
  删过 app/ 下页面后 tsc 报 .next/types 陈旧时，先删 apps/web/tsconfig.tsbuildinfo 或跑 npx next typegen。
- 子代理报“模型不存在”时，给子代理显式指定模型；不要改用户本机的 ~/.claude 配置。

## 4. 红线（违反任一即退回）
- 绝不硬编码 LLM 供应商或 API Key；模型访问只走 per-account 的 providers 模块；密钥只回掩码，厂商异常原文不透出。
- 所有查询带账号作用域，跨账号一律 404；外部 HTTP 只走安全出站层（SSRF 校验）；测试不访问网络。
- MCP 写操作只含可逆增量并记审计，不开放删除；站内 Agent 不直接写库。
- 使用信号 / 派生数据不改 version、updated_at。迁移只追加，禁止对 sites 做 batch 重建。
- 前端只用 apps/web/app/globals.css 顶部令牌，禁止写死颜色；圆角 ≤8px；图标只用 lucide-react 1.26.0
  （禁用 Loader2 / AlertCircle / StopCircle）；外部图片一律经 proxiedImageUrl。
- 仓库内不得建 temp/、tmp/、scratch/ 目录；Cookie、密钥、真实书签、.data、.env 不得提交。

## 5. 工作方式
- 只改 spec“文件清单”里的文件；清单外的改动要在报告“偏差”里逐个说明理由。
- 按 spec 的 R 编号分步实现，每完成一个可独立验证的 R 就提交一次（中文提交信息：`<type>(<scope>): <摘要>`，
  正文写“为什么 + 关键决定”）。这样中断后能从 git log 接着做。
- 每条 T 编号的测试都要写，并能在“删掉实现时失败”。并发测试用事件 / 屏障控制顺序，禁止 sleep 碰运气。
- 有界面的条目：自己启动应用、播种数据，截图浅色 / 深色 1440 宽和手机 390 宽，对照 ACCEPTANCE.md §4 自查。
  截图放仓库外（例如系统临时目录），报告里写绝对路径。
- 交付前过一遍 ACCEPTANCE.md §3 自查表，发现的问题修掉并补回归测试。

## 6. 交付
1. 全量门禁全绿后，只推送你自己的条目分支：git push -u origin <条目分支>
   绝不推送 claude/*、main 或其他分支；不强推；不改 PROGRESS.md、ITERATION_QUEUE.md。
2. 按 ACCEPTANCE.md §5 模板输出交付报告（分支 / 基线哈希 / 提交哈希、门禁数字、R→实现、T→测试、偏差、自查、截图、未验证项），
   作为你的最后一条回复。用户会把报告转给总负责人验收；验收意见回来后在同一分支上修改并再次推送、再次报告。
```
