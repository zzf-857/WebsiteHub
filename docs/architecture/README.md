# WebHub 架构与执行规范（总入口）

本目录是 **首席架构师（总负责人）** 交给执行 Agent 的全部“施工图”。执行 Agent 只按这里的文档落地；
总负责人按 `ACCEPTANCE.md` 验收。任何文档没写清楚的地方，**先停下来在交付报告里提问，不要自行发明**。

## 文档层级（冲突时上位优先）

1. `ITERATION_QUEUE.md` 文末“不可动摇的约束” —— 安全与产品红线，永远最高。
2. `docs/architecture/ARCHITECTURE.md` —— 系统边界、模块职责、数据所有权、全局不变量。
3. `docs/architecture/CODING_STANDARDS.md` —— 代码风格、目录结构、命名、测试、迁移、提交规范（强制）。
4. `docs/architecture/specs/Qxx-*.md` —— 单个条目的可执行规格（接口、数据、步骤、测试清单、验收点）。
5. `ITERATION_QUEUE.md` 条目正文 —— 条目范围与状态；与 spec 冲突时以 spec 为准（spec 更新更晚、更细）。
6. `PRD.md` —— 产品背景；第 0 节（2026-10 定位修订）优先于正文。

`ACCEPTANCE.md` 定义“做完”的标准与验收流程，执行前必须读。
新开一个执行 Agent 时，把 `HANDOFF_PROMPT.md` 里的提示词正文发给它（替换条目编号）即可。

## 执行 Agent 的标准流程

1. 读：本文件 → `ARCHITECTURE.md` → `CODING_STANDARDS.md` → `ACCEPTANCE.md` → 自己负责的 `specs/Qxx-*.md`。
2. 在独立分支 / worktree 上实现（分支名 `qNN-work`，基于 spec 写明的基线提交）。
3. 严格按 spec 的“文件清单”和“接口契约”实现；需要偏离时，在报告“偏差”一节写明原因。
4. 按 spec 的“测试清单”逐条写测试；跑全量门禁到全绿。
5. 自查一遍（`ACCEPTANCE.md` 的执行者自查表），修掉真问题并补回归测试。
6. 提交（规范见 `CODING_STANDARDS.md` §9）；**只推送自己的 `qNN-work` 分支**（`git push -u origin qNN-work`），
   不推送任何其他分支，**不改** `PROGRESS.md` / `ITERATION_QUEUE.md`。
7. 按 `ACCEPTANCE.md` §5 的报告模板交付。

## 当前待执行条目（2026-10-23）

| 顺序 | 条目 | Spec | 依赖 | 可并行 |
| --- | --- | --- | --- | --- |
| 0 | Q57-0 合并后全量门禁复核 | `specs/Q57-e2e-self-check.md` §2 | 无 | 必须最先，单独派发 |
| 1 | Q51 订阅流补齐 | `specs/Q51-feeds-completion.md` | 步骤 0 | 与 Q53、Q54 并行 |
| 1 | Q53 数据导出与备份导入 | `specs/Q53-data-portability.md` | 步骤 0 | 与 Q51、Q54 并行 |
| 1 | Q54 后端稳健性 | `specs/Q54-backend-robustness.md` | 步骤 0 | 与 Q51、Q53 并行 |
| 2 | Q57 端到端自查与迭代 | `specs/Q57-e2e-self-check.md` | Q51、Q53、Q54 全部合并 | 否 |

迁移编号：Q51 `20261023_0025`、Q54 `20261023_0026`、Q53 `20261023_0027`（以 spec 为准，取代队列里早先的预留；
Q52 / Q55 / Q56 实际未加迁移）。并行条目各自接在基线 head（`20261022_0024`）之后；合并时由总负责人
按 `CODING_STANDARDS.md` §6.4 串成单链。

## 新条目怎么写

复制 `specs/_TEMPLATE.md`，所有标“必填”的小节都必须写满；写不满说明需求没想清楚，不能派发。
