# WebHub 浏览器扩展

原生 JavaScript 的 MV3 扩展（Chrome / Edge 102+），两项能力互不相干：

| 能力 | 文件 | 状态 |
| --- | --- | --- |
| 收藏当前页（Q47） | `save-core.js`（纯逻辑）、`save-worker.js`（工具栏 / 右键 / 通知接线）、`options.*` | 新增 |
| Space 标签组桥接（Q25） | `protocol.js`、`content-script.js`、`service-worker.js`、`pending.*` | 冻结（D2），行为不变 |

`service-worker.js` 只在第一行多 `importScripts` 了 `save-core.js` 与 `save-worker.js`；收藏功能不使用
标签组的消息通道（`runtime.onMessage`）和存储键，选项页也不给 Service Worker 发消息。

## 收藏是怎么走的

1. 入口：工具栏按钮、快捷键 `Alt+Shift+S`（`_execute_action`）、页面 / 链接右键「收藏到 WebHub」。
   `activeTab` 让这三个入口在用户操作时读到当前页网址和标题，扩展平时读不到任何标签页。
2. 读取 `chrome.storage.local` 里的 `webhub.save.settings.v1`（地址 + 令牌，不用 `storage.sync`，不上云）。
3. 确认已获得该地址的主机权限（`optional_host_permissions`，在选项页点「保存 / 测试连接」时申请）。
4. `POST <地址>/api/backend/mcp`，JSON-RPC `tools/call` → `save_resource {url, title}`，
   `Authorization: Bearer <读写令牌>`；`credentials: "omit"`、`redirect: "manual"`（令牌不跟随跳转）。
5. 反馈：徽标 `…` → `✓` 已收藏 / `=` 之前收藏过 / `!` 失败，悬停提示写明结果；系统通知带「打开详情」
   （`<地址>/library/<id>`）或「打开设置」（需要改设置才能修好的错误）。

## 侧载

1. 打开 `chrome://extensions`（Edge：`edge://extensions`），开启「开发者模式」。
2. 「加载已解压的扩展程序」→ 选择仓库里的 `apps/browser-extension`。
3. 首次安装会自动打开选项页：填 WebHub 地址（如 `https://<机器>.<tailnet>.ts.net`，本机开发用
   `http://localhost:3100`）和在 WebHub「设置 → Agent 接入」创建的**读写**令牌，点「保存」并同意授权。
4. 快捷键冲突时到 `chrome://extensions/shortcuts` 改。

## 测试

纯逻辑与接线测试放在 web 包里，随前端门禁的 `node --test` 一起跑：

```bash
cd apps/web && node --test tests/browser-extension-save.test.mjs tests/browser-extension-worker.test.mjs
```

- `browser-extension-save.test.mjs`：地址 / 令牌规范化、授权匹配模式、JSON-RPC 消息构造、HTTP / RPC / 工具错误解析、
  `performSave` 全流程、通知去向、徽标、清单约束。
- `browser-extension-worker.test.mjs`：在假的 `chrome.*` 里加载真实 `service-worker.js`，验证三个入口、通知按钮
  与标签组监听器仍然注册。

## 手动验收清单（Chrome 与 Edge 各走一遍）

- [ ] 侧载后扩展名为「WebHub」，首次安装自动打开选项页；工具栏图标为深色底青柠书签。
- [ ] 选项页：只填地址不填令牌 → 提示粘贴令牌；乱填令牌 → 提示格式不对；填明文远程 HTTP 地址 → 出现明文警告。
- [ ] 点「保存」弹出该主机的授权确认；拒绝 → 提示没有授权；同意 → 显示「已保存」，令牌框显示 `已保存 whk_xxxx…`。
- [ ] 「测试连接」：读写令牌显示「连接成功：N 个分类、M 个标签」；吊销后的令牌显示令牌无效；错误地址显示连不上 / 找不到接口。
- [ ] 普通网页点工具栏按钮：徽标 `…` → `✓`，通知「已收藏到 WebHub」；点「打开详情」进入该收藏的详情页。
- [ ] 同一页再收藏一次：徽标 `=`，通知「之前已经收藏过」。
- [ ] `Alt+Shift+S` 与页面右键「收藏到 WebHub」效果同上；链接右键「收藏这个链接到 WebHub」收藏的是链接地址。
- [ ] `chrome://`、`edge://`、新标签页上点按钮：徽标 `!`，通知「只能收藏 http / https 网页」。
- [ ] 只读令牌收藏：徽标 `!`，通知说明需要读写令牌，「打开设置」回到选项页。
- [ ] 停掉 WebHub 后收藏：徽标 `!`，通知「连不上 WebHub」或「后端暂时不可用」，几秒后徽标自动清除。
- [ ] WebHub「设置 → Agent 接入」的活动列表出现「收藏 https://…」审计记录。
- [ ] 标签组回归：在 `http://localhost:3100` 的 Space「一键全部打开」对话框里的「在浏览器分组打开」仍能创建命名标签组。
- [ ] 「清除设置」后再收藏：自动打开选项页，不发任何请求。
