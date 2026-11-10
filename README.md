# MangaFinder

一个本地优先、可插拔数据源的漫画作者订阅与作品发现器。

当前 MVP 支持：

- 添加/删除作者订阅；
- 通过 WNACG 和 nHentai 发现作者相关作品；
- 保存封面、简介、状态、年份、标签和来源链接；
- 手动或定时刷新作者，后台任务可追踪、可重试，并对失败来源独立指数退避，不重复扫描健康来源；
- 行动型资料库总览、新发现收件箱、发现趋势、来源健康度与待发布日历；
- 待办工作台集中处理新发现、聚合候选、待发布命中和未恢复的下载失败；支持按选择批量已读或忽略；
- 独立待发布作品生命周期，可汇总多条社交信号并在来源命中时建议关联；
- Series 系列层、前后作/番外关系、保守卷号候选与缺卷提示；
- 作者标准名、别名、社团和来源查询名配置，并对身份冲突要求人工确认；
- 作品字段来源、手工固定和首选版本；SQLite 下支持查看并安全撤销升级后的合并、拆分和排除；
- 本地以图搜库，使用裁切感知 dHash、pHash 和颜色指纹返回可解释匹配；
- 下载前检查 HE Manager 馆藏：来源一致和标题相似分别提示；无法检查时需明确确认；
- CBZ 自动写入 ComicInfo.xml，并提供 SHA-256/CRC 校验、空间统计和确认式清理；
- JSON 导出、可验证且默认排除凭证的备份、OPDS 目录及 Komga/Kavita 扫描触发；
- WNACG 自动尝试主域和官方发布页镜像，只接受标题前部的精确作者署名，并可将单册打包为 CBZ；
- nHentai 使用官方 JSON API 按 artist 标签发现作者作品，自动遵守接口配额，支持可选 API Key、来源级代理和单册 CBZ 下载；
- 可将误收录版本或整部作品从指定作者名下排除，后续刷新不会重复导入同一来源；
- 数据源能力声明，为其他合法来源的章节下载适配器预留稳定接口；
- 响应式 Web 管理界面。
- 可选的聚合 Agent：对规则无法确定的候选做结构化证据复核，默认只读并保留人工确认。
- 可选的作者动态雷达：绑定人工确认的 X 账号，区分转推、参展、新刊、再版与取消，
  通过站内雷达、QQ 官方 Bot 或签名 Webhook 提醒。
- DeepSeek AI 助手：基于已采集的作者动态与作品资料回答、提供来源链接；支持的资料修改先预览，需管理员再次确认。

> 只应连接你有权访问和下载的来源。适配器必须遵守来源的 API 条款、robots、限流和版权要求。本项目不会绕过登录、付费墙或反爬验证。

## 快速开始

### Docker（推荐）

```bash
docker compose up --build
```

打开 <http://localhost:8000>。

左侧“系统设置”首次打开时会要求创建至少 12 位的管理员密码。修改作品、订阅和任务等操作需要管理员登录；会话过期后页面会引导重新登录。模型 API Key、QQ Secret
和 X 登录会话可随后在设置中心安全导入；Compose profile、数据卷和容器网络仍在 `.env`
或 Dockge 中配置。加密密钥自动保存在数据目录的 `system-secret.key`，请与数据库一起备份，
不要公开或删除。

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

架构决策、已完成的日常发现闭环和后续功能路线见 [docs/PLAN.md](docs/PLAN.md)。

使用 Dockge 管理和更新请参阅 [docs/DOCKGE.md](docs/DOCKGE.md)。

聚合 Agent 的设计、安全边界和实施阶段见
[docs/AGENT_AGGREGATION_PLAN.md](docs/AGENT_AGGREGATION_PLAN.md)。

当前服务器可用 `./scripts/sync-dockge.sh --build-web` 将工作区安全同步到 `/opt/stacks/mangafinder`；脚本保留 Dockge 中的 `.env` 和持久数据。

AI 助手的数据范围、修改确认流程和安全边界见 [docs/AI_ASSISTANT.md](docs/AI_ASSISTANT.md)。

作者动态雷达的设计边界见 [docs/SOCIAL_RADAR_PLAN.md](docs/SOCIAL_RADAR_PLAN.md)，Dockge、X 会话和
QQ Bot 配置见 [docs/SOCIAL_RADAR.md](docs/SOCIAL_RADAR.md)，Webhook 请求格式与验签方式见
[docs/WEBHOOK.md](docs/WEBHOOK.md)。

## HE Manager 馆藏检查

在系统设置 → 部署状态中使用 HE Manager 账号连接，密码只用于登录，不保存；登录会话加密保存，过期后需重新连接。下载前会检查 HE Manager 的漫画馆藏：相同来源或相似标题需人工确认，未找到也可能因馆藏元数据不完整而遗漏。服务不可用时需明确确认才继续。Compose 默认访问同一 Docker 网络的 `http://he-manager:8010`，其他部署可设置 `MANGAFINDER_HE_MANAGER_BASE_URL`。
