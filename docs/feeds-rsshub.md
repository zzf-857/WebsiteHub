# 订阅流与自部署 RSSHub

WebHub 的 `/feeds`（导航“订阅”）只展示 RSS / Atom / 播客条目，不入库：条目保留 30 天，
只有点“收进收藏库”的才写进收藏（并保留与条目的关联）。后台每 60 分钟按订阅源轮询，
使用 ETag / Last-Modified 条件请求，失败按 2h、4h、8h… 指数退避（最长 24 小时），
单次最多读 2 MB、50 条，单源最多保留 200 条。

## 为什么需要 RSSHub

多数中文站点（B 站、知乎、微博、少数派专栏、公众号等）没有 RSS。[RSSHub](https://docs.rsshub.app/)
把它们转换成标准 RSS。建议与 WebHub 部署在同一台服务器，只在容器网络内可达，不对外开放。

## 部署（与 deploy/compose.yaml 同一个 Compose 项目）

在 `deploy/compose.yaml` 的 `services:` 下追加（镜像标签请固定到一个具体版本）：

```yaml
  rsshub:
    image: diygod/rsshub:<固定版本>
    restart: unless-stopped
    environment:
      NODE_ENV: production
      CACHE_TYPE: memory
    # 不发布任何端口：只有同一 Compose 网络里的 webhub 能访问 http://rsshub:1200
    security_opt:
      - no-new-privileges:true
```

然后在 `deploy/.env` 中放开这个源（默认所有订阅都只能访问公网地址，内网地址会被 SSRF 校验拒绝）：

```dotenv
WEBHUB_FEED_TRUSTED_ORIGINS=http://rsshub:1200
```

`docker compose -f deploy/compose.yaml up -d` 后，在“订阅 → 订阅管理”里添加 `http://rsshub:1200/<路由>`。
Windows 本机开发时 RSSHub 跑在 `http://127.0.0.1:1200`，相应写 `WEBHUB_FEED_TRUSTED_ORIGINS=http://127.0.0.1:1200`。

## 常见路由示例

| 来源 | 路由 |
| --- | --- |
| B 站 UP 主投稿 | `/bilibili/user/video/<uid>` |
| 知乎专栏 | `/zhihu/zhuanlan/<id>` |
| 知乎用户回答 | `/zhihu/people/answers/<id>` |
| 微博用户 | `/weibo/user/<uid>` |
| 少数派首页 | `/sspai/index` |
| 掘金分类 | `/juejin/category/<category>` |
| GitHub 仓库 Release | `/github/release/<owner>/<repo>` |
| 小宇宙播客 | `/xiaoyuzhou/podcast/<id>` |

路由以 RSSHub 文档为准；部分路由需要在 RSSHub 侧配置 Cookie，请只在 RSSHub 的环境变量里配置，
不要放进 WebHub。

## 外部 Agent

MCP 只读工具 `get_feed_digest(limit=10)` 返回“今日精选”（与收藏库高频标签 / 分类的匹配度 + 时间衰减，
同一来源最多 3 条），供 Hermes / grokbot 每日推送。
