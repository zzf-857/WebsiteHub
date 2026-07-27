# WebHub Agent Rules

本项目继承 `F:\AI\AgentMake\AGENTS.md` 的全部工作区规则，并补充以下硬性约束：

1. 项目仓库内不得创建 `temp/`、`tmp/`、`scratch/` 或其他临时资料目录。
2. 调试脚本、验证输出、截图、日志、旧计划、重复文档和废弃设计产物统一放到 `F:\AI\AgentMake\temp\WebHub\<任务名>`。
3. 清理出的资料若仍需复核，放到 `F:\AI\AgentMake\temp\WebHub\cleanup-YYYY-MM-DD`，同时维护该目录的 `MANIFEST.md`；不要直接永久删除。
4. Cookie jar、Session、临时密钥、真实书签和其他敏感数据不得提交 Git，也不得复制到项目文档。
5. `README.md` 只做项目入口，`PRD.md` 管产品范围，`PROGRESS.md` 管当前状态，`ITERATION_QUEUE.md` 管当前待办；不要再创建平行的 Plan、Task 或状态快照。
6. `Arts/handoff/untitled/project/WebHub 效果图.dc.html` 是当前设计真源。旧稿和重复打包文件只能进入统一 temp 隔离区。

