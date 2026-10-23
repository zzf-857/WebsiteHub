# WebHub 代码规范（强制）

适用于所有执行 Agent。验收时逐条对照（`ACCEPTANCE.md`）。“必须 / 禁止”是硬性要求，“建议”可在报告里说明后偏离。

## 1. 通用

- 必须：改动最小、聚焦本条目；不顺手重构无关代码，不重命名无关符号。
- 必须：新增行为配测试；测试基线只涨不降。
- 必须：注释写“为什么”，不复述代码；中文注释与现有代码风格一致，面向接手者。
- 禁止：遗留调试输出、注释掉的代码、TODO 而无对应队列条目。
- 禁止：在仓库内创建 `temp/`、`tmp/`、`scratch/` 等目录（见 `AGENTS.md`）。
- 文件规模：Python 单文件 ≤800 行、TSX 组件 ≤700 行、CSS 单文件 ≤1200 行。超限时按职责拆分
  （参考 `library/service/` 包的拆法）。已超限的历史文件不得继续变大。

## 2. Python 后端

### 2.1 风格与工具
- Ruff 配置以 `services/api/pyproject.toml` 为准（行宽 100；规则 E、F、I、UP、B、SIM）。提交前 `uv run ruff check .` 必须干净。
- 必须：完整类型标注；模块顶部 `from __future__ import annotations`（与现有文件一致）。
- 值对象用 `@dataclass(frozen=True, slots=True)`；枚举型字符串用 `Literal[...]` 并在 DB 加 CHECK。
- 禁止：裸 `except:`、吞掉异常不记录；`except Exception` 只允许在后台任务边界，并保证不泄露上游原文。
- 时间：一律 UTC aware（`datetime.now(UTC)`）。从 SQLite 读出的 naive 时间先经模块内 `_utc()` 归一。
- 依赖：新增第三方库必须在 spec 里写明，用 `uv add` 并提交锁文件。

### 2.2 分层
```
routes.py      HTTP：依赖注入、请求/响应模型、_call() 错误映射、调度副作用（如排队分析）
   │ 只调用 ↓
service(.py|/) 业务：参数 (session, user_id, ...)，返回领域对象或响应模型，只抛领域异常
   │ 使用 ↓
db/models      SQLAlchemy 模型与约束
纯函数模块      无 I/O 的规则（如 kinds.py、fusion.py），可被任意层调用并独立测试
```
- 禁止：服务层 import FastAPI / Request；路由层写 SQL；模块之间直接改对方的表（通过对方服务函数）。
- 服务函数命名 `动词_名词`：`list_feeds`、`create_feed`、`merge_tag`；私有辅助加下划线前缀。
- 账号归属检查统一用模块内 `_owned_<thing>(session, user_id, id)`，不存在或不属于当前账号都抛 NotFound。

### 2.3 请求 / 响应
- 请求模型继承模块的 `StrictRequest`（`extra="forbid"`）；字段带长度 / 范围约束；可选字段 `None` 表示“不改”。
- 响应模型显式声明；时间字段为 `datetime`；不返回裸 `dict`（MCP 工具输出除外，但需稳定结构并在测试里断言）。
- 列表接口：游标分页（`cursor` + `limit`，`next_cursor`），游标作用域包含所有筛选与排序参数。

### 2.4 写操作与并发
- 用户编辑：乐观锁 `expected_version`，冲突返回 409。
- 使用信号与派生数据：显式 SQL `UPDATE`，保持 `updated_at = Site.updated_at`，不改 `version`（不变量 I6）。
- 需要“先锁后读”的地方（如删除前锁行）用 SQLite 兼容的条件 no-op `UPDATE` 取写锁，并配并发回归测试。
- 批量写：单事务全有或全无；或明确的“逐批独立事务 + 持久回执”，二选一并在 spec 写明。

### 2.5 安全
- 外部 HTTP 只走安全出站层（ARCHITECTURE §5.3）；新代码复用 `ingestion/fetcher.py` 的公开函数，必要时在该文件新增
  公开封装，**不要**再从其他模块 import 它的私有 `_` 函数。
- 日志：不记录密钥、令牌、Cookie、完整带查询参数的 URL、上游异常原文。
- 用户可见错误文案固定、中文、不含内部细节。

## 3. TypeScript / React 前端

### 3.1 风格与工具
- `tsc --noEmit`（strict）与 `eslint .` 必须干净。禁止 `any`、`@ts-ignore`；必要时 `unknown` + 守卫。
- 具名导出；默认导出只用于 Next 的 `page.tsx` / `layout.tsx`。
- 文件名 kebab-case；组件名 PascalCase；hook 以 `use` 开头且文件名 `use-*.tsx`。
- 判别联合 + 穷尽 `switch`（`default` 里 `const _: never = x`）。

### 3.2 分层（见 ARCHITECTURE §3）
- `lib/<area>-contract.ts`：导出类型与 `normalize<Thing>(value: unknown, path?: string)`；使用 `createContractGuards`；
  后端字段 snake_case → 前端 camelCase 只在这里转换。
- `lib/<area>-client.ts`：常量 `BACKEND = "/api/backend"`；每个接口一个 `async function`；错误抛 `<Area>ApiError`
  （含 `status`、`code`、`message`）；写请求 `credentials: "same-origin"`、`Content-Type: application/json`。
- 纯逻辑放 `lib/<topic>.ts`，**组件里不写可测试的业务规则**。
- 状态：组件内 `useState`/`useReducer`；复杂流程用纯 reducer（放 `lib/`）+ hook；跨区域事件用 `lib/library-events.ts`。
- 过期响应：每次请求递增序号或用 `AbortController`，只采纳最新一次的结果。

### 3.3 交互规范
- 可逆操作：乐观更新 + 5 秒撤销提示（复用现有 toast / undo 机制）；失败回滚并给出中文提示。
- 破坏性操作：二次确认（原生 `<dialog>` 或现有对话框模式），Escape 关闭并恢复焦点。
- 每个数据视图都要设计 **加载 / 空 / 错误** 三态；错误态提供“重试”。
- 键盘：所有操作可 Tab 到达；不在输入框内响应单键快捷键（复用 `lib/keyboard-shortcuts.ts`）。
- 图片：`proxiedImageUrl()` + `loading="lazy"` + `referrerPolicy="no-referrer"` + `onError` 降级。
- 外链：`target="_blank" rel="noopener noreferrer"`；打开收藏前先 `recordSiteOpen`（在点击处理函数里同步 `window.open`）。

## 4. 样式

- 必须：颜色、阴影、字号、间距、时长只用 `app/globals.css` 顶部令牌；新令牌必须同时写入 `:root`、
  `:root[data-theme="dark"]`、`@media (prefers-color-scheme: dark) :root:not([data-theme="light"])` 三处。
- 例外：按站点哈希出的色相通过 CSS 变量（如 `--site-hue`）注入，颜色仍由令牌的明度 / 饱和度组成。
- 必须：圆角 ≤8px（圆点、头像、播放按钮可为圆形）。
- 类名前缀按区域：`disc-`（发现）、`sd-`（详情）、`lib-`（收藏库）、`feeds-`、`agents-`、`data-`（数据导出）等；
  新区域在 spec 里指定前缀。不得修改其他区域前缀的样式。
- 动效：只表达状态变化；时长用 `--duration-*`；必须尊重 `prefers-reduced-motion`。
- 390px 宽度下页面不得横向滚动；宽内容在自身容器内滚动。
- 图标只用 `lucide-react`（1.26.0），禁用 `Loader2`、`AlertCircle`、`StopCircle`；加载态用 `components/react-bits/spinner.tsx`。

## 5. 文案

- 界面中文；术语统一：收藏 / 收藏库 / 发现 / 今日重温 / 订阅 / Space / Agent 接入 / 星标 / 已看 / 推迟 / 不再推荐。
- 不写“网站 / 网址库”指代收藏（D1）。错误提示说清“发生了什么 + 用户能做什么”。

## 6. 数据库迁移

### 6.1 命名
`services/api/alembic/versions/YYYYMMDD_NNNN_<slug>.py`，`revision = "YYYYMMDD_NNNN"`，`NNNN` 全局递增。

### 6.2 写法
- 加列用原生 `op.add_column`；`NOT NULL` 必须带 `server_default`；**禁止对 `sites` 做 batch 重建**（FTS 触发器）。
- 删列的降级参考 `20260731_0019`：先卸下引用该表的触发器，删列，再重建触发器。
- 数据回填在迁移内分块（按 id 分页），不触碰 `version`、`updated_at`。
- 迁移禁止 import 应用代码；需要的规则复制一份冻结副本并在注释说明。
- 同步更新 `db/database.py` 的 `DATABASE_SCHEMA_HEADS`。

### 6.3 测试
每个迁移一个测试：在已有数据的库上升级 → 断言结构与回填 → 降级 → 断言恢复 → 再升级。

### 6.4 并行条目的迁移串链（总负责人执行）
并行条目各自 `down_revision` 指向分支基线 head。合并时按合并顺序把后合入者的 `down_revision`（与文件头 `Revises:`）
改为前一个迁移，`DATABASE_SCHEMA_HEADS` 设为最终 head，再跑 `alembic heads` 确认只有一个 head，并跑全量后端测试。

## 7. 测试

### 7.1 后端（pytest）
- 位置：`services/api/tests/<module>/test_<topic>.py`；复用现有 fixture 模式（`TestClient(create_app(...))`、临时数据目录）。
- 每个新接口至少：成功路径、422 校验、401 未登录、403 Origin（写接口）、跨账号 404、关键业务分支。
- 外部 HTTP 一律注入假传输 / 打桩，**测试不得访问网络**。
- 并发相关缺陷必须有能稳定复现的并发测试（事件 / 屏障控制顺序），不能靠 sleep 碰运气。

### 7.2 前端（node --test）
- 位置：`apps/web/tests/<topic>.test.ts`（逻辑）或 `.test.mjs`（渲染 / 源码断言）。
- 契约：合法输入、旧响应缺字段、格式错误三类。
- 客户端：URL、方法、请求体、错误映射（mock `fetch`）。
- 纯逻辑：边界与回归。渲染断言只用于防止关键结构被误删，不替代逻辑测试。

### 7.3 门禁（每次提交前全绿）
```bash
cd apps/web && npx tsc --noEmit && npx eslint . && node --test && npx next build
cd services/api && uv run pytest -q && uv run ruff check .
```
`pyproject` 已设 `addopts=-q`，要看汇总行时去掉 `-q`。Node 需 24.x（云端沙箱：`export PATH=/root/.local/node24/bin:$PATH`）。

## 8. 文档

- 状态只记在 `ITERATION_QUEUE.md`（条目状态 / 门禁基线）与 `PROGRESS.md`（快照）。**并行执行时由总负责人统一更新**，
  执行者不改。
- 行为或配置变化影响用户 / 部署时，更新 `README.md` 对应章节（spec 会写明）。
- 新想法写进 `IDEAS.md`，不在代码里留 TODO。

## 9. Git 与提交

- 分支：`qNN-work`（一个条目一个分支），基于 spec 写明的基线提交。
- 提交信息中文：`<type>(<scope>): <摘要>`，type ∈ `feat | fix | refactor | test | docs | chore`；正文写“为什么 + 关键决定”。
- 每条提交末尾附会话要求的署名行（以会话当前的 attribution 提醒为准）。
- 推送：执行者只推送自己的 `qNN-work` 分支（供总负责人拉取验收）；`main`、`claude/*` 等其他分支只由总负责人推送。
- 禁止：推送其他分支、强推、改写他人提交历史、`--no-verify`、提交 `.env` / `.data` / 密钥。
