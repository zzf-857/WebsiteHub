# Q57 · 端到端自查与迭代修复

- 基线：总负责人合并 Q51、Q53、Q54 并串好迁移链之后的 `claude/loving-maxwell-gfbcxo` HEAD（开工前向总负责人确认哈希）；分支 `q57-work`
- 迁移：无（如修复必须改表结构，停下上报，不得自行加迁移）
- 新目录：`apps/web/e2e/`（Playwright 套件）；新依赖 `@playwright/test@1.56.1`（`apps/web` devDependency，`pnpm add -D`，提交锁文件）
- 本条目可以改任何文件**以修复缺陷**，但受 §5 “修复边界”约束

## 1. 背景（必填）

Q42–Q56 各自有单元 / 接口测试，但没有任何一条在真实浏览器里跑完整流程；Q52、Q55、Q56 合并时只跑了 `tsc` 与 `ruff`，
全量门禁未复核。本条目先把门禁补回全绿，再用一套可重复运行的端到端测试覆盖主要用户流程，发现的问题逐个修复并加回归测试。

## 2. 步骤 0：全量门禁复核（必须最先做）

步骤 0 在派发 Q51 / Q53 / Q54 **之前**单独执行一次（任务名 Q57-0，基线为当前 HEAD，分支 `q57-0-work`，
只做本节，按 `ACCEPTANCE.md` §5 交付）；Q57 正式开工时在新基线上再复核一次，只记录数字，红了按同样规则处理。

1. 在基线上原样运行 `CODING_STANDARDS.md` §7.3 的全量门禁（后端去掉 `-q` 看汇总行）。
2. 记录数字：后端 passed / skipped，前端 tests / pass / skip。
3. 若有红：逐个定位，修复为独立提交 `fix(gate): <摘要>`，每个修复配说明“哪个合并引入的”。
   不得修改断言来让测试变绿，除非能证明断言本身与 spec 矛盾（在报告里写明 spec 出处）。
4. 门禁全绿后才进入 §3。

## 3. 端到端套件（必填）

### 3.1 结构
```
apps/web/
  playwright.config.ts        # 唯一配置
  e2e/
    fixtures.ts               # 账号注册 / 登录、经 API 播种、清理；导出 test / expect
    seed.ts                   # 纯数据构造（无断言）
    auth.spec.ts
    capture.spec.ts           # 快速收藏
    discover.spec.ts
    detail.spec.ts
    library.spec.ts
    feeds.spec.ts
    agent-access.spec.ts      # 令牌 + 真实 MCP HTTP 调用
    portability.spec.ts       # 导出 → 新账号导入
    visual.spec.ts            # 主题与窄屏
```
- 文件名用 `*.spec.ts`（`node --test` 不会误收）；`tsc` / `eslint` 必须覆盖 `e2e/`，保持干净。
- 新脚本 `apps/web/package.json`：`"e2e": "playwright test"`。E2E **不并入**每次提交的全量门禁，但验收时必须通过。

### 3.2 运行环境
- `playwright.config.ts` 用 `webServer` 数组启动两个进程，端口避开开发端口：
  - API：`uv run --project ../../services/api webhub-db upgrade && uv run --project ../../services/api uvicorn webhub.main:app --host 127.0.0.1 --port 8190 --no-proxy-headers`
  - Web：先 `next build`（或复用已有构建），再 `node server.mjs --hostname 127.0.0.1 --port 3190`
  - 环境变量：`WEBHUB_DATA_DIR` 指向 `os.tmpdir()` 下每次运行新建的目录（**不得在仓库内**）、
    `WEBHUB_API_INTERNAL_URL=http://127.0.0.1:8190`、可信 Origin 包含 `http://127.0.0.1:3190`；
    运行环境设为非 production（使用开发主密钥自动生成），具体变量名以 `services/api/src/webhub/config.py` 为准。
- 浏览器：Chromium；云端沙箱 `PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers`，**不得**运行 `playwright install`；
  版本不匹配时在配置里用 `launchOptions.executablePath`（由环境变量 `WEBHUB_E2E_CHROMIUM` 覆盖，缺省不设）。
- `workers: 1`、`fullyParallel: false`（单 SQLite）；`retries: 0`（不靠重试掩盖不稳定）；失败保留 trace 与截图到
  `apps/web/test-results/`（加入 `.gitignore`）。
- **不依赖外网**：保存外部网址时元数据抓取可能失败或被 SSRF 拦截，断言只针对“收藏已创建、状态可见、页面不报错”；
  不配置 LLM Provider，AI 相关断言只到“未配置时的提示 / 受限状态”。需要外网的用例打 `@network` 标签并默认跳过。
- 每个 spec 自己注册独立账号（用户名带随机后缀），不依赖其他 spec 的数据；播种只经 `/api/backend/*` 并带
  `Origin: http://127.0.0.1:3190`。

### 3.3 场景清单（每条至少一个测试，编号即测试标题前缀）

| 编号 | 场景 | 关键断言 |
| --- | --- | --- |
| E1 | 注册 → 退出 → 登录；未登录访问 `/` 跳到登录页 | URL、账号菜单可见 |
| E2 | 快速收藏：输入网址保存 → 出现在发现页与收藏库；重复保存提示已存在 | 卡片出现、提示文案 |
| E3 | 发现页：星标、已看、推迟、不再推荐各一次，每个都点“撤销”恢复；“换一批”后撤销仍有效 | 乐观更新 + 撤销后状态与服务器一致（刷新后再断言） |
| E4 | 详情页就地编辑名称 / 摘要 / 标签 / 分类，刷新后保留；并发冲突（另开上下文先改）出现冲突提示 | 409 文案、数据不丢 |
| E5 | 键盘快捷键：打开快速收藏、搜索聚焦、列表上下移动与打开；输入框内单键不触发 | 焦点位置 |
| E6 | 收藏库：类型 / 分类 / 标签筛选组合、搜索、批量选择 → 批量删除（二次确认）→ 撤销或确认 | 计数、URL 参数 |
| E7 | Space：新建、加入成员、调整顺序、移除 | 顺序刷新后保持 |
| E8 | 订阅：添加订阅源（用 API 播种已拉取的条目，避免外网）、标记已读、收进收藏库、取消订阅（二次确认） | 条目状态、收藏出现 |
| E9 | Agent 接入：创建只读令牌与读写令牌，明文只显示一次；用 Playwright `request` 以 `Authorization: Bearer` 对页面展示的 MCP 地址发 JSON-RPC：`initialize`、`tools/list`、`tools/call search_resources`；只读令牌调用写工具被拒；吊销后 401 | 返回结构、审计列表出现记录 |
| E10 | 数据导出与恢复：账号 A 下载备份 → 账号 B 上传 → 预览数量 → 确认导入 → 收藏库出现同样的收藏、Space 顺序一致 | 预览数字、结果页 |
| E11 | 主题：浅 / 深各访问发现、详情、收藏库、订阅、设置五页，无控制台错误，截图存档 | `console.error` 为 0 |
| E12 | 窄屏 390×844：上述五页 `document.documentElement.scrollWidth <= 390`；触控目标 ≥40px（抽查主按钮） | 数值断言 |
| E13 | 图片代理：页面中所有外部图片 `src` 以 `/api/backend/media/proxy` 开头 | DOM 断言 |

截图（E11、E12）写到 `apps/web/test-results/screenshots/`，交付时在报告列路径；不提交截图。

## 4. 迭代修复流程（必填）

1. 跑全套 → 收集失败。
2. 每个失败先判断：**测试写错**（选择器、时序）还是**产品缺陷**。测试问题修测试；等待一律用 Playwright 的
   自动等待与 `expect.poll`，**禁止** `waitForTimeout`。
3. 产品缺陷：
   - 在报告“缺陷表”登记：编号 Bn、场景、现象、根因、修复提交、回归测试。
   - 先写能复现的最低层测试（后端 pytest / 前端 node:test；只有纯浏览器行为才只靠 E2E），再修复。
   - 一个缺陷一个提交：`fix(<scope>): <摘要>（Bn）`。
4. 修完重跑全套 + 全量门禁，直到全部通过；最后一次完整运行的输出摘要贴进报告。

## 5. 修复边界（必填）

- 可直接修：单一模块内的缺陷、文案、样式、可访问性、竞态，改动 ≤5 个文件且不改接口契约。
- **必须停下上报总负责人**（在报告里给出建议方案，不实施）：需要改表结构、改公开接口契约、改 `ARCHITECTURE.md` 不变量、
  改设计令牌或跨区域视觉语言、或修复会让某个 spec 的功能要求不再成立。
- 不得：跳过 / 删除 / 标记 `skip` 已有测试；为过 E2E 加只在测试环境生效的分支；放宽安全检查（Origin、SSRF、令牌作用域）。

## 6. 文件清单（必填）

新：`apps/web/playwright.config.ts`、`apps/web/e2e/*.ts`；改：`apps/web/package.json`、锁文件、`.gitignore`
（`apps/web/test-results/`、`apps/web/playwright-report/`）、`README.md`（新增“端到端测试”一节：前置条件、命令、
沙箱浏览器路径说明）。其余文件只因缺陷修复而改，逐个列在缺陷表中。

## 7. 测试清单（必填）

- T1 §2 门禁复核结果与修复提交（若有）。
- T2 §3.3 的 E1–E13 全部通过（无 `@network` 用例时也要通过）。
- T3 每个产品缺陷有对应回归测试（最低可行层）。
- T4 连续两次完整运行结果一致（证明无不稳定用例）。

## 8. 验收要点

- 报告包含：步骤 0 数字、缺陷表、两次完整运行摘要、截图路径、上报项（若有）。
- 总负责人在合并结果上亲自运行一次 `pnpm --filter @webhub/web e2e` 与全量门禁。
- 抽查 E9：令牌明文只出现一次、审计日志记录写操作、只读令牌无法写。
