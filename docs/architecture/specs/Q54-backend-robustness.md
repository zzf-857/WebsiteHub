# Q54 · 后端稳健性补齐

- 基线：`claude/loving-maxwell-gfbcxo` 当前 HEAD；分支 `q54-work`
- 迁移：`20261023_0026`，`down_revision = "20261022_0024"`（新站分析意图表）
- 前端区域：只改 Provider 表单（类名沿用现有 `provider-` 写法），不新增页面
- 并行关系：与 Q51、Q53 并行；**不得**改 `feeds/` 业务逻辑、`site-header.tsx`、`server.mjs`；
  `ingestion/worker.py` 已超 1200 行，本条目**不得让它变长**（新逻辑放新文件）
- 四个子项相互独立，建议按 R1 → R2 → R3 → R4 分四个提交

## 1. 背景（必填）

1. **新站 LLM 分析意图只在内存里**。`library/analysis_queue.py::schedule_site_enrichment` 把手动 / Agent / 订阅收藏的
   新站放进 `ingestion/worker.py` 的内存队列；进程重启或队列满时意图丢失，只能等某次列表请求触发
   `ensure_auto_backfill` 的扫尾才被捡起。首页批量补全已经是持久 run（`metadata_backfill`），新站这条路径还不是。
2. **删除与“加入 Space”有并发窗口**。`library/service/sites.py::delete_site` / `bulk_delete_sites` 先读
   `SpaceMember` 得到受影响的 Space，再删 Site；`spaces/service.py::add_member` 在这之间插入成员时，该 Space 的
   `version` 不会被删除方递增，前端持有的 Space 版本与实际成员不一致。
3. **自定义 OpenAI 兼容 Provider 无法开启流式用量**。`agent/provider_binding.py` 只对 `openai`、`deepseek`
   发 `stream_options.include_usage`；兼容端点（如自建网关）即使支持也拿不到用量。
4. **旧收藏正文快照未核实**。Q48 引入 `site_contents`；显式批量补全（首页 / 收藏库）是否为旧收藏写入快照没有测试保证。

## 2. 功能要求（必填）

### R1 新站分析意图持久化

- **R1.1 新表** `site_analysis_intents`（§3）：每 `(user_id, site_id)` 至多一行，表示“这条收藏还欠一次 LLM 分析”。
- **R1.2 写入**：`schedule_site_enrichment` 改为 `async def`，先在**独立短事务**里
  `INSERT … ON CONFLICT(user_id, site_id) DO UPDATE SET next_attempt_at = now`（不重置 `attempts`），提交后再调用现有内存调度。
  三个调用方（`library/routes.py`、`feeds/routes.py`、`agent_access/tools.py`）改为 `await`。
  写意图失败只记日志（不含 URL），不影响收藏本身的成功响应。
- **R1.3 新模块** `ingestion/intents.py`（≤300 行），只含：
  - `record_site_intent(database, *, user_id, site_id) -> None`（R1.2 的写入）。
  - `async def tick(database, *, now=None, limit=64) -> IntentTickResult`：取 `next_attempt_at <= now` 的行
    （按 `next_attempt_at` 升序，跨账号，`limit` 条），逐行判定：
    1. Site 不存在 → 删行（外键级联通常已删，这里兜底）。
    2. `site_metadata_preferences.llm_analyzed_at` 非空 → 删行（已完成）。
    3. 账号没有启用的 `model` Provider → 删行（无法分析；以后配置 Provider 时由现有扫尾处理）。
    4. `attempts >= MAX_INTENT_ATTEMPTS`（6）→ 删行并记一条 `warning` 日志（只含 site_id）。
    5. 否则调用 `ingestion_worker.schedule_analysis(…, intent=SITE_ENRICHMENT, priority=False, interactive=False, bulk=False)`；
       `attempts += 1`、`next_attempt_at = now + backoff(attempts)`；若被拒（队列满）则不加 `attempts`，
       只把 `next_attempt_at` 推后 60 秒。
    - 退避：`backoff(n) = min(60 × 2^(n-1), 6 × 3600)` 秒。
    - `IntentTickResult` 为 frozen dataclass：`scheduled`、`completed`、`dropped`、`deferred` 计数，供测试断言。
  - `class IntentPoller`：与 `feeds/scheduler.py::FeedPoller` 同构（`start()` / `stop()`，循环间隔 60 秒，
    循环体 `except Exception` 记日志后继续）。
- **R1.4 启动恢复**：`main.py` 生命周期在 `resume_metadata_backfills` 之后立即 `await intents.tick(database)` 一次；
  非 test 环境启动 `IntentPoller`，关闭时先停 poller 再 `ingestion_worker.shutdown`。
- **R1.5 不改变现有语义**：即时调度路径（R1.2 后的内存调度）、并发上限、熔断与 `interactive` 溢出槽保持不变；
  `tick` 重复调度已在内存队列中的站点是安全的（`schedule_analysis` 已去重），测试需覆盖。
- **R1.6 删除级联**：`site_id` 外键 `ON DELETE CASCADE`，`user_id` 外键 `ON DELETE CASCADE`（账号删除即清空）。

### R2 删除与加入 Space 的串行化

- **R2.1** `delete_site` 与 `bulk_delete_sites` 在读取 `SpaceMember` **之前**调用新的锁函数
  `db/locking.py::reserve_sites(session, user_id, site_ids) -> int`：
  `UPDATE sites SET version = version WHERE user_id = :u AND id IN (:ids)`（`synchronize_session=False`），返回命中行数。
  这条语句让 SQLite 立即取得写锁；之后的读取与删除在同一写事务内，`add_member` 必须等待。
  - 已核实：`sites` 的 FTS 触发器都是列限定的 `AFTER UPDATE OF name, original_url, …`，不含 `version`，
    该语句不会触发索引重建。**不得**改变 `version` / `updated_at` 的值。
- **R2.2** 命中行数与请求数不符 → 按现有冲突路径（`version_conflict` / `bulk_delete_conflict`）回滚返回 409。
- **R2.3** `spaces/service.py::add_member` 与 `add_members_batch` 在 `_owned_site` 之前调用同一锁函数锁目标 Site，
  使两条路径都以“先锁 Site”开始，杜绝交叉等待。被锁 Site 已被删除时返回现有 404。
- **R2.4** 锁函数放 `db/locking.py`（与 `reserve_account_taxonomy` 同处），带 docstring 说明原因；`__all__` 更新。

### R3 OpenAI 兼容 Provider 的“支持流式用量”开关

- **R3.1 存储**：不加列。用 `provider_configs.config_json` 的键 `"stream_usage"`（布尔），缺省 `false`。
- **R3.2 接口**：`ProviderCreateRequest` / `ProviderUpdateRequest` 增加 `stream_usage: bool | None = None`
  （更新时 `None` 表示不改）。只有 `kind == "model"` 且 `provider == "openai_compatible"` 时允许非 `None`，
  否则 422（`code = "stream_usage_not_applicable"`，中文文案“该服务商不支持此选项”）。
  修改它与其他可编辑字段一样递增 `version`。
- **R3.3 响应**：Provider 响应增加 `stream_usage: bool`：`openai` / `deepseek` 恒为 `true`（只读含义），
  `openai_compatible` 为存储值，其余为 `false`。
- **R3.4 绑定**：`ProviderBinding` 增加 `stream_usage: bool`，由 `resolve_binding` 按 R3.3 同一规则计算；
  `build_chat_model` 改为 `stream_usage=binding.stream_usage`。规则写成 `providers/` 内的纯函数
  `stream_usage_enabled(provider: str, config: Mapping[str, object]) -> bool`，路由与绑定共用。
- **R3.5 前端**：`provider-form.tsx` 在服务商为“OpenAI 兼容”时显示复选框
  “支持流式用量统计（stream_options.include_usage）”，下方一行辅助说明“端点不支持时开启会导致对话失败”；
  契约 `normalizeProvider` 增加 `streamUsage`（缺失 → `false`）；客户端创建 / 更新请求体带 `stream_usage`。

### R4 旧收藏正文快照核实

- **R4.1** 核实显式批量补全（`metadata_backfill` run 的条目分析）是否经 `ingestion/content.py` 写入
  `site_contents`。若已写入：只补测试。若未写入：在条目分析成功路径调用现有写入函数（不新建写入逻辑），
  失败不影响条目结果。
- **R4.2** 不触发正文快照写入的路径（例如 metadata-only 的自动扫尾）保持现状，在报告中列明。
- **R4.3** `README.md` “AI 整理”或同等章节加一句：旧收藏的正文快照在显式批量补全时生成。

## 3. 数据与迁移（必填）

`20261023_0026_site_analysis_intents.py` 新表 `site_analysis_intents`：

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `user_id` | String(36) FK `users.id` ON DELETE CASCADE | 复合主键之一 |
| `site_id` | String(36) FK `sites.id` ON DELETE CASCADE | 复合主键之一 |
| `intent` | String(32) NOT NULL `server_default 'site_enrichment'` | CHECK `intent IN ('site_enrichment')`，为以后扩展留位 |
| `attempts` | Integer NOT NULL `server_default '0'` | |
| `next_attempt_at` | DateTime(timezone) NOT NULL | |
| `created_at` / `updated_at` | DateTime(timezone) NOT NULL | |

- 主键 `(user_id, site_id)`；索引 `ix_site_analysis_intents_next_attempt_at`。
- 新表不涉及 `sites` 重建。降级：删索引、删表。
- 模型放 `db/models/analysis_intents.py`，在 `db/models/__init__.py` 导出。
- `DATABASE_SCHEMA_HEADS = {"20261023_0026"}`（合并时总负责人按 §6.4 串链）。

## 4. 接口契约（必填）

```jsonc
// POST /api/providers（kind=model, provider=openai_compatible）
{ "kind": "model", "provider": "openai_compatible", "display_name": "自建网关",
  "base_url": "https://llm.example.com/v1", "model_name": "qwen3", "stream_usage": true,
  "secret": { "action": "write", "value": "…" }, "enabled": true }
// 任意 Provider 响应新增
{ "…": "…", "stream_usage": true }
// 422（provider ≠ openai_compatible 时传 stream_usage）
{ "detail": { "code": "stream_usage_not_applicable", "message": "该服务商不支持此选项" } }
```
R1、R2、R4 不改任何 HTTP 契约。

## 5. 文件清单（必填）

后端：`library/analysis_queue.py`、`library/routes.py`、`feeds/routes.py`（仅 `await` 一处）、`agent_access/tools.py`
（仅 `await` 一处）、新 `ingestion/intents.py`、`main.py`、`db/locking.py`、`library/service/sites.py`、
`spaces/service.py`、`providers/schemas.py`、`providers/service.py`（或实际处理 create/update 的文件）、新纯函数所在文件、
`agent/provider_binding.py`、`db/models/analysis_intents.py`、`db/models/__init__.py`、`db/database.py`、迁移、
R4 若需修改则 `ingestion/` 内对应文件（报告写明）。
测试：`tests/ingestion/test_site_intents.py`、`tests/library/test_delete_space_race.py`、
`tests/providers/test_stream_usage.py`、`tests/agent/test_provider_binding.py`（追加）、`tests/ingestion/test_backfill_content.py`、迁移测试。
前端：`components/settings/provider-form.tsx`、`lib/provider-contract.ts`、`lib/provider-client.ts`、
`lib/provider-form.ts`（表单状态与请求体构造）、`tests/provider-*.test.ts`。
文档：`README.md` 一句（R4.3）。

## 6. 测试清单（必填）

- T1 保存收藏后 `site_analysis_intents` 有一行；重复保存同一站点仍一行，`attempts` 不被重置。
- T2 模拟“重启”：写入意图行、不进内存队列 → 新建 app（或直接 `tick`）→ 站点被调度一次（注入假 enricher，断言调用）。
- T3 `tick` 删除规则四种：站点已删、`llm_analyzed_at` 已有、无启用 Provider、超过最大尝试次数；各一例。
- T4 退避：连续 `tick`（注入 `now`）时 `next_attempt_at` 按 60、120、240… 递增，6 小时封顶；队列满时只推后 60 秒且不加次数。
- T5 账号隔离与级联：删除收藏 / 删除账号后意图行消失；`tick` 只按行内 `user_id` 调度。
- T6 写意图失败（打桩抛错）时收藏创建仍返回 201。
- T7 并发：用事件 / 屏障确定性复现“删除读取成员后、提交前，`add_member` 尝试插入”——修复后 `add_member` 等待并在删除提交后
  返回 404，且被删 Site 原所在 Space 的 `version` 已递增；批量删除同理一例。**不得**用 `sleep` 控制顺序。
- T8 `reserve_sites` 不改变 `version`、`updated_at`，不改变 FTS 检索结果。
- T9 `stream_usage`：`openai_compatible` 创建 / 更新 / 读回；其他服务商传入 422；更新递增 `version`；响应规则（R3.3）。
- T10 绑定：`openai_compatible` 开启时 `build_chat_model` 收到 `stream_usage=True`，关闭时 `False`；`openai` / `deepseek` 恒 `True`。
- T11 R4：显式批量补全成功后对应旧收藏有 `site_contents` 行（假抓取返回正文）；失败不影响条目结果。
- T12 迁移升级 → 降级 → 再升级。
- T13 前端契约 `streamUsage` 缺省 / 合法 / 非布尔；表单只在“OpenAI 兼容”时渲染复选框（源码或渲染断言）。

## 7. 验收要点

- `ingestion/worker.py` 行数不增；新模块职责单一。
- 所有删除 / 加成员路径都以“先锁 Site”开始；锁语句不改任何值。
- 日志中无 URL、无 Provider 密钥；422 文案为中文固定文案。
- 截图：Provider 表单“OpenAI 兼容”与“OpenAI”两种状态（浅 / 深 1440）。
