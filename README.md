# MangaFinder

一个本地优先、可插拔数据源的漫画作者订阅与作品发现器。

当前 MVP 支持：

- 添加/删除作者订阅；
- 通过 MangaDex、WNACG 和 nHentai 发现作者相关作品；
- 保存封面、简介、状态、年份、标签和来源链接；
- 手动刷新作者，后台任务可追踪、可重试；
- 通过 MangaDex 公共 API 选择章节，后台打包 CBZ 并下载；
- WNACG 自动尝试主域和官方发布页镜像，并可将单册打包为 CBZ；
- nHentai 使用 gallery JSON 发现作者作品，支持来源级代理/Cookie 和单册 CBZ 下载；
- 数据源能力声明，为其他合法来源的章节下载适配器预留稳定接口；
- 响应式 Web 管理界面。
- 可选的聚合 Agent：对规则无法确定的候选做结构化证据复核，默认只读并保留人工确认。
- 可选的作者动态雷达：绑定人工确认的 X 账号，区分转推、参展、新刊、再版与取消，
  通过站内雷达和 QQ 官方 Bot 提醒。

> 只应连接你有权访问和下载的来源。适配器必须遵守来源的 API 条款、robots、限流和版权要求。本项目不会绕过登录、付费墙或反爬验证。

## 快速开始

### Docker（推荐）

```bash
docker compose up --build
```

打开 <http://localhost:8000>。

### 本地开发

后端：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e './apps/api[dev]'
uvicorn app.main:app --app-dir apps/api --reload
```

前端：

```bash
cd apps/web
npm install
npm run dev
```

开发服务器默认在 <http://localhost:5173>，并代理 `/api` 到后端。

## 常用命令

```bash
make test
make lint
make build
```

架构决策和后续路线见 [docs/PLAN.md](docs/PLAN.md)。

使用 Dockge 管理和更新请参阅 [docs/DOCKGE.md](docs/DOCKGE.md)。

聚合 Agent 的设计、安全边界和实施阶段见
[docs/AGENT_AGGREGATION_PLAN.md](docs/AGENT_AGGREGATION_PLAN.md)。

当前服务器可用 `./scripts/sync-dockge.sh --build-web` 将工作区安全同步到 `/opt/stacks/mangafinder`；脚本保留 Dockge 中的 `.env` 和持久数据。

作者动态雷达的设计边界见 [docs/SOCIAL_RADAR_PLAN.md](docs/SOCIAL_RADAR_PLAN.md)，Dockge、X 会话和
QQ Bot 配置见 [docs/SOCIAL_RADAR.md](docs/SOCIAL_RADAR.md)。
