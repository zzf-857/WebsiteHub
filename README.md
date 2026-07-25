# WebsiteHub

WebsiteHub 是一个 Agent First 的个人网站知识中枢。用户可以通过自然语言收录、检索和管理网站，并使用分类、标签与 Space 组织自己的网址资料库。

## 当前状态

项目处于需求设计阶段，优先交付桌面 Web MVP：

- 局域网账号注册、登录和账号级数据隔离
- 多会话 Agent、跨设备历史同步和 Slash Command 扩展入口
- 网站自动分析、可编辑预览和人工确认
- 收藏库优先检索，以及 Tavily、Jina、Exa 联网补充
- 唯一分类、多标签、多 Space 和批量打开
- OpenAI、DeepSeek、通义千问、Kimi、Ollama 与自定义模型配置

完整产品定义见 [PRD.md](./PRD.md)。

## 交付顺序

1. 完成并确认 PRD。
2. 根据 PRD 形成正式架构与分阶段实施计划。
3. 在当前 Windows 电脑完成首版开发，并开放家庭局域网访问。
4. 后续补充移动端响应式布局、Docker Compose 和 NAS/Linux 部署。

## 规划中的技术职责

- Next.js、React、Vercel AI SDK：Web 界面、流式聊天和生成式 UI
- LangChain、LangGraph：Agent 工具编排和 Human-in-the-Loop
- LlamaIndex：网站资料索引、混合召回和 RAG
- ReactBits：克制使用的高价值交互动效

具体版本、目录结构和服务边界将在正式实施计划中确定。
