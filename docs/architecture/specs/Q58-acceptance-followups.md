# Q58 · Q51 / Q53 / Q54 验收跟进项

- 基线：`claude/loving-maxwell-gfbcxo` 当前 HEAD（含 Q51、Q53、Q54 集成）；分支 `q58-work`
- 迁移：无
- 来源：总负责人 2026-10-23 验收（Q51 通过、Q54 通过、Q53 有条件通过）
- 在 Q57 之前完成，使端到端套件覆盖最终行为
- 只改下列文件清单涉及的模块；每个 R 一个提交

## 1. 背景（必填）

验收审查发现 Q53 两个应修复项（订阅数量上限被恢复路径绕过；过期备份暂存只在同账号再访问时清理），
以及若干小问题。本条目一次性收尾，不扩展新功能。

## 2. 功能要求（必填）

### 应修复（Q53）
- **R1 恢复订阅遵守上限**：`feeds/service.py` 的 `MAX_FEEDS_PER_ACCOUNT`（100）同样约束备份恢复。
  - 预览阶段：`现有订阅数 + 将新建数 > 100` 时，超出部分计入 `feeds.skip_limit`（新计数键），并加一条警告
    `{ "code": "feed_limit", "message": "订阅已达 100 个上限，备份中 N 个订阅源未导入" }`；不整体拒绝导入。
  - 应用阶段：`feeds/backup.py::restore_feed` 在新建前再次检查上限（防预览后账号又新增订阅），超限时不新建并计入同一计数。
  - 前端预览表显示该计数（契约 `skipLimit`，缺失 → 0）。
- **R2 过期暂存全局清理**：新增 `portability/importer.py::expire_all_previews(database, directory, *, now=None)`，
  跨账号把 `status='previewed' AND expires_at <= now` 的行置为 `expired` 并删除 payload 文件；
  启动时调用一次；另在新文件 `portability/scheduler.py` 里加一个独立轻量循环（与 `feeds/scheduler.py::FeedPoller` 同构，
  间隔 1 小时，只在非 test 环境启动，关闭时按 R7 的方式停止）。不得放进 `ingestion/`。
- **R3 每账号暂存上限**：创建预览时，若该账号 `previewed` 行已有 5 条，把最旧的置为 `expired` 并删除其文件。
- **R4 账号删除清理文件**：若仓库有账号删除路径，删除后移除 `portability-imports/<user_id>/` 目录；
  若没有账号删除功能，在 R2 的清理里顺带删除“目录名不对应任何现存用户”的子目录。报告写明采用哪种。

### 小问题
- **R5** `apps/web/lib/portability-client.ts` 错误映射加 `import_failed`（文案“导入未完成，数据未修改”），补客户端测试；
  同步更新 `specs/Q53-data-portability.md` §4.4 错误码表（总负责人已授权本条目改此一行）。
- **R6** `providers/service.py` 更新请求仅含 `stream_usage: null` 时不得递增 `version`：先剔除 `None` 的可选字段再判断“至少一个字段”，
  补测试（应返回现有的“无变更”行为或 422，与其他字段一致）。
- **R7** `main.py` 关闭顺序：每个 poller 的 `stop()` 各自 `try/finally`，保证一个失败不跳过其余与 `ingestion_worker.shutdown`。
- **R8** `provider-form.tsx` 复选框 label 的内联 `style` 移到 provider 样式文件中的类。
- **R9** `feeds/parser.py::_FirstImage` 改为取第一个通过 `safe_link` 的 `<img src>`（而非遇到第一个不安全的就停），补一例测试。
- **R10** `app/styles/feeds.css` 的 `@media (width < 600px)` 改为仓库通用写法 `@media (max-width: 599px)`。

## 3. 数据与迁移（必填）

无。`skip_limit` 只是预览 / 结果 JSON 的新键。

## 4. 接口契约（必填）

```jsonc
// 预览 counts.feeds
{ "create": 3, "skip_existing": 1, "skip_limit": 2 }
```
前端 `normalizeImportPreview`：`feeds.skipLimit` 缺失 → 0，非整数 → 报错。

## 5. 文件清单（必填）

后端：`feeds/backup.py`、`portability/importer.py`、`portability/schemas.py`、新 `portability/scheduler.py`（清理循环）、
`main.py`、`providers/service.py`、`feeds/parser.py`；测试 `tests/portability/test_import.py`（或新 `test_cleanup.py`）、
`tests/providers/test_stream_usage.py`、`tests/feeds/test_feed_images.py`（或现有图片测试文件）。
前端：`lib/portability-contract.ts`、`lib/portability-client.ts`、`components/settings/data-portability-workspace.tsx`、
`components/settings/provider-form.tsx`、对应样式文件、`app/styles/feeds.css`、`tests/portability-*.test.ts`。
文档：`specs/Q53-data-portability.md` §4.4 一行。

## 6. 测试清单（必填）

- T1 账号已有 98 个订阅，备份含 5 个新订阅 → 预览 `create=2, skip_limit=3` 且有警告；应用后共 100 个。
- T2 预览后账号又手动新增 2 个订阅 → 应用时只再新建 0 个，`skip_limit` 计数正确。
- T3 `expire_all_previews`：两个账号各一条过期、一条未过期 → 只处理过期的两条，文件被删除。
- T4 第 6 次预览使最旧的一条变为 `expired` 且文件删除。
- T5 R4 所选方案的清理测试。
- T6 客户端 `import_failed` 映射。
- T7 `stream_usage: null` 单独更新不递增 `version`。
- T8 第一个 poller `stop()` 抛错时其余仍被停止、worker 仍关闭（打桩）。
- T9 HTML 中第一个 `<img>` 为 `javascript:`、第二个为合法 https → 取第二个。
- T10 前端契约 `skipLimit` 缺省 / 合法 / 非法。

## 7. 验收要点

- 恢复路径与手动新增订阅共用同一上限常量，无第二处硬编码 100。
- 清理循环不在 `ingestion/`，测试不依赖真实计时。
