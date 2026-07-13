# MangaFinder 技术选型与实施计划

更新时间：2026-07-13

## 1. 产品目标

用户订阅作者名后，系统定期从已启用来源发现该作者的作品，归一化并保存封面与元数据，突出显示新发现内容；对明确允许下载且适配器实现了下载能力的来源，提供可恢复的下载任务。

首版的成功标准不是“支持尽可能多的网站”，而是把新增来源的成本压缩到实现一个适配器，并让失败、限流、重试和来源差异不会污染核心业务。

## 2. 调研结论

- [Suwayomi Server](https://github.com/Suwayomi/Suwayomi-Server) 验证了来源扩展、后台更新、下载队列适合漫画聚合场景；我们借鉴来源能力隔离，但不在 MVP 引入 JVM 扩展运行时。
- [Komf](https://github.com/Snd-R/komf) 的提供方优先级、字段级聚合和来源配置值得后续采用；MVP 先保存原始来源标识，避免过早自动合并同名作品。
- [Komga](https://github.com/gotson/komga) 与 [Kavita](https://github.com/Kareadita/Kavita) 证明媒体文件、元数据和阅读服务应该分离。MangaFinder 聚焦“发现与获取”，未来通过 ComicInfo.xml、OPDS 或 API 对接阅读器。
- MangaDex 提供可访问的公共 API，可按作者查询并返回封面关系，因此作为第一个真实适配器。不会使用 Google/Twitter 页面爬虫作为首版来源：结果不稳定、认证与条款边界更复杂。
- WNACG 没有稳定公开 API，使用站内搜索 HTML 适配器；规范链接保留 `wnacg.com`，请求失败时按配置回退到官方发布页列出的镜像。适配器不自动绕过 Cloudflare，仅允许用户自行配置 Cookie。

## 3. 技术选型

| 层 | 选择 | 原因 |
|---|---|---|
| Web API | Python 3.12 + FastAPI | 异步 I/O、类型提示、OpenAPI 和爬取生态成熟 |
| 数据访问 | SQLAlchemy 2.x | Repository 边界清晰；SQLite/PostgreSQL 可切换 |
| 数据库 | SQLite（默认） | 个人本地部署零配置；并发或 NAS 部署时切 PostgreSQL |
| HTTP | httpx | 异步、超时与连接池支持良好，测试易替换 |
| 后台任务 | 数据库任务表 + 应用内 worker | MVP 无 Redis 依赖，同时保留状态、重试和错误记录 |
| 前端 | React 19 + TypeScript + Vite | 组件化、类型安全、构建轻量 |
| 测试 | pytest + Vitest | 覆盖适配器解析、服务编排与 UI 逻辑 |
| 部署 | 单 Docker 镜像 + Compose | 前端构建产物由 API 服务，个人部署只需一个容器 |

不选择微服务：当前只有一个部署单元，拆服务会增加队列、鉴权、可观测性和版本协调成本。代码仍按领域模块拆分，未来可从稳定边界提取服务。

## 4. 模块边界

```text
Web UI -> REST API -> Authors/Catalog services -> Provider ports
                          |                  -> MangaDex adapter
                          v
                     Repositories -> SQLAlchemy -> SQLite/PostgreSQL

REST API -> Job repository -> Worker -> discovery service
                                -> future download service -> filesystem
```

- `providers`：只负责外部来源协议、请求、解析和能力声明；不写数据库。
- `authors`：订阅生命周期与刷新入口。
- `catalog`：作品归一化和查询。
- `jobs`：持久任务状态、领取、重试和 worker 生命周期。
- `db`：ORM、会话和事务；业务模块不直接组装外部请求。
- `web`：只依赖 REST 契约。

## 5. 核心数据模型

- `authors`：用户订阅的作者名、最后检查时间。
- `works`：归一化展示字段。
- `work_sources`：来源名、外部 ID、原始 URL、来源更新时间；唯一键 `(provider, external_id)`。
- `author_works`：作者与作品多对多关系。
- `jobs`：类型、载荷、状态、尝试次数、错误和时间戳。

跨来源去重暂不根据标题自动合并。标题相同不代表同一作品；后续以 ISBN、来源交叉链接或人工确认建立 `work_matches`。

## 6. 来源适配器契约

每个适配器实现：

- `capabilities`：作者搜索、作品发现、章节列表、下载等能力；
- `discover_by_author(name)`：返回统一 DTO；
- 稳定的 `provider + external_id`；
- 明确超时、限流、可重试错误和 User-Agent；
- 下载能力只能用于来源明确允许访问的内容，不能绕过认证、付费墙或反爬。

新增网站时先确认官方 API/条款，再写契约测试与响应 fixture；HTML 选择器不得散落在业务服务。

## 7. 分阶段实施

### 阶段 A：MVP（本次构建）

- [x] 项目骨架、配置、数据库模型；
- [x] Provider 抽象与 MangaDex 适配器；
- [x] 作者增删查、手动刷新、作品列表；
- [x] 持久后台发现/下载任务与失败重试；
- [x] Web 管理界面、Docker、基础测试；
- [x] MangaDex 章节选择与 CBZ 下载；校验章节归属并仅使用公开 API。
- [x] WNACG 关键词发现、官方镜像回退、单册图片清单解析与 CBZ 下载。

### 阶段 B：可日常使用

- 定时刷新策略、只对失败来源退避、通知（Webhook/邮件）；
- 作者别名与人工匹配，跨来源作品匹配审核；
- 封面本地缓存、ETag/Last-Modified、数据导入导出；
- 接入更多用户拥有访问权且允许下载的来源，并为 CBZ 写入 ComicInfo.xml；
- PostgreSQL 与独立 worker 部署模式。

### 阶段 C：生态集成

- OPDS/ComicInfo.xml、Komga/Kavita 扫描触发；
- RSS、出版社官方 API、作者主页等合规来源；
- 下载限速、断点续传、校验、磁盘配额与清理策略；
- 多用户鉴权、审计和通知偏好。

## 8. 质量门槛

- Provider 必须有 fixture 测试，外部网络不进入单元测试；
- Service 只依赖 Provider protocol 和 Repository；
- 数据库唯一约束保证任务重复执行幂等；
- 所有外部请求必须设置超时、限流标识和可识别 User-Agent；
- 日志不得记录 Cookie、Token 或下载签名 URL；
- 变更通过后端测试、前端类型检查和生产构建。

## 9. 主要风险

- 站点结构与 API 会变化：通过适配器契约和 fixture 把影响限制在单模块。
- 同名作者/笔名：MVP 返回来源匹配结果，阶段 B 增加候选确认和别名。
- 错误合并：默认不跨来源自动合并。
- 版权与条款：优先官方 API，只允许适配器显式声明下载能力。
- SQLite 并发：个人部署足够；多 worker 前切 PostgreSQL，并使用 `SKIP LOCKED` 领取任务。
