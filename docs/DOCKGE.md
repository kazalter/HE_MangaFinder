# 使用 Dockge 管理 MangaFinder

MangaFinder 的根目录已经是一个完整的 Dockge Stack：`compose.yaml`、`Dockerfile.dockge`、后端源码和 `apps/web/dist` 必须放在同一目录，因为当前使用本地镜像构建。

Dockge 使用 `Dockerfile.dockge`，直接复制已生成的 Web 静态文件，不需要服务器在每次构建时下载 Node 镜像。开发环境的完整双阶段构建仍保留在根目录 `Dockerfile`。

## 推荐目录

Dockge 默认扫描 `/opt/stacks/<stack-name>/compose.yaml`，因此推荐结构为：

```text
/opt/stacks/
└── mangafinder/
    ├── compose.yaml
    ├── Dockerfile.dockge
    ├── .env
    ├── apps/
    └── data/
```

不要只把 `compose.yaml` 粘贴到一个空 Stack 中，否则 Dockge 找不到构建镜像所需的 Dockerfile、API 源码和 Web 构建产物。

## 首次加入 Dockge

1. 将整个项目复制或克隆到 Dockge 的 stacks 目录，例如 `/opt/stacks/mangafinder`。
2. 在项目根目录把 `.env.example` 复制为 `.env`。
3. 根据需要修改 `.env`：

```dotenv
MANGAFINDER_PORT=8000
MANGAFINDER_DATA_DIR=./data
MANGAFINDER_CHAPTER_LANGUAGES=zh-hans,zh-hant,en,ja
MANGAFINDER_USE_DATA_SAVER=true
MANGAFINDER_WNACG_BASE_URLS=https://www.wnacg.com,https://www.wn08.cfd,https://www.wn07.cfd
MANGAFINDER_WNACG_COOKIE=
MANGAFINDER_WNACG_MAX_SEARCH_PAGES=5
MANGAFINDER_NHENTAI_ENABLED=true
MANGAFINDER_NHENTAI_BASE_URL=https://nhentai.net
MANGAFINDER_NHENTAI_PROXY_URL=http://172.19.0.1:7897
MANGAFINDER_NHENTAI_COOKIE=
MANGAFINDER_NHENTAI_MAX_SEARCH_PAGES=3
MANGAFINDER_PROXY_NETWORK=he-manager_default
TZ=Asia/Shanghai
```

### 可选：启用聚合 Agent

第一阶段的 Agent 只审核现有聚合候选，不会自动合并，也不会把封面图片发送给模型。
推荐先在宿主机运行 Ollama，并在 Dockge 的 `.env` 中配置：

```dotenv
MANGAFINDER_AGENT_ENABLED=true
MANGAFINDER_AGENT_PROVIDER=openai_compatible
MANGAFINDER_AGENT_BASE_URL=http://host.docker.internal:11434/v1
MANGAFINDER_AGENT_MODEL=你的本地模型名
MANGAFINDER_AGENT_API_KEY=
MANGAFINDER_AGENT_TEMPERATURE=0
MANGAFINDER_AGENT_TIMEOUT_SECONDS=60
MANGAFINDER_AGENT_MAX_REVIEWS_PER_RUN=20
MANGAFINDER_AGENT_AUTO_APPLY=false
MANGAFINDER_AGENT_ALLOW_CLOUD_IMAGES=false
MANGAFINDER_AGENT_PROMPT_VERSION=v5
```

也可以填写兼容 `/v1/chat/completions` 和 JSON Schema structured output 的其他接口。
如果使用云端接口，填写对应的 HTTPS `BASE_URL` 和 `API_KEY`；当前版本仍只发送标题、
作者、编号、页数、年份、标签、来源身份和封面感知哈希，不发送封面 URL 或图片。

保存 `.env` 后在 Dockge 点击 **Update**。打开“聚合候选”窗口后，只有模型配置完整时
“细查全部候选”按钮才会启用。模型结果仍需点击“确认合并”或“保持分开”。

DeepSeek API 使用独立适配模式：

```dotenv
MANGAFINDER_AGENT_PROVIDER=deepseek
MANGAFINDER_AGENT_BASE_URL=https://api.deepseek.com
MANGAFINDER_AGENT_MODEL=deepseek-v4-flash
MANGAFINDER_AGENT_API_KEY=你的密钥
```

密钥只应保存在 Dockge Stack 的 `.env` 中；同步脚本不会覆盖或复制该文件。

WNACG 主域可能返回 Cloudflare 403。系统会顺序尝试 `MANGAFINDER_WNACG_BASE_URLS` 中的官方镜像；通常不需要 Cookie。如果所有镜像均要求浏览器验证，可以把自己有权使用的会话 Cookie 填入 `MANGAFINDER_WNACG_COOKIE`。系统不会自动破解或绕过验证码。

WNACG 可能包含成人内容。当前应用没有用户鉴权，启用该来源时不要直接暴露到未受保护的公网或未成年人可访问的网络。

nHentai 可能返回 Cloudflare 403。代理只配置给该来源，不要在容器里填写 `127.0.0.1:7897`，因为它指向 MangaFinder 容器自身。Compose 会通过 `MANGAFINDER_PROXY_NETWORK` 加入代理桥网络，并可访问 `http://172.19.0.1:7897`；若代理监听 Docker 宿主网关，也可使用 `http://host.docker.internal:端口`。代理不足以通过来源验证时，可在自己有权使用的浏览器会话中取得 Cookie，填入 `MANGAFINDER_NHENTAI_COOKIE`。系统不会破解验证码或自动绕过 Cloudflare。

已存在的 Hanime1 作品不会被删除，界面中会显示为“历史来源（已停用）”，但不再刷新或下载。

4. 确保数据目录可写：`mkdir -p /opt/stacks/mangafinder/data`。
5. 进入 Dockge，点击右上角菜单中的 **Scan Stacks Folder**。
6. 打开 `mangafinder`，点击 **Start**。第一次会构建本地镜像。
7. 容器显示 `healthy` 后访问 `http://服务器地址:8000`。

如果你的 Dockge 使用自定义 stacks 目录，就把项目放到该目录下。Dockge 容器中的 stacks 路径必须与宿主机路径保持一致，例如宿主和容器都使用 `/opt/stacks`。

## 在 Dockge 中更新

代码更新后，在 MangaFinder Stack 的 Terminal 中执行：

```bash
# 如果改过前端，先在源码工作区执行：cd apps/web && npm ci && npm run build
docker compose build --pull
docker compose up -d
```

如果只修改 `.env` 或 `compose.yaml`，直接点击 Dockge 的 **Update** / **Start** 即可重新创建容器。

从开发工作区同步到 Dockge Stack：

```bash
# 后端、Compose 或文档改动
./scripts/sync-dockge.sh

# 包含前端改动：先重新生成 Web 产物再同步
./scripts/sync-dockge.sh --build-web
```

脚本不会覆盖 Stack 中的 `.env` 和 `data`，也不会自行重启容器；最后的更新动作仍由 Dockge 管理。

## 数据与备份

数据库和下载文件都位于：

```text
${MANGAFINDER_DATA_DIR:-./data}
```

备份时停止 Stack，然后复制整个数据目录。更新或重新构建镜像不会删除这里的数据。

## 反向代理

如果通过 Nginx、Caddy 或 Traefik 暴露服务，代理目标为容器宿主机的 `MANGAFINDER_PORT`。应用前后端同源，不需要额外配置 WebSocket。

建议只在可信网络中使用；当前 MVP 还没有用户登录系统。如果需要开放到公网，应先在反向代理层增加 HTTPS 和身份认证。

## 常见问题

### Dockge 找不到 Stack

- 文件名必须是 `compose.yaml`；
- Stack 必须位于 Dockge 配置的 stacks 目录直属子目录中；
- 点击 **Scan Stacks Folder** 重新扫描；
- 检查 Dockge 的 stacks volume 是否使用相同的宿主机和容器路径。

### 构建时无法拉取基础镜像

检查 Docker daemon 的镜像源和代理配置。当前开发机曾配置 `127.0.0.1:7890` 代理，但该代理未运行；这会导致 Dockge 和命令行构建同时失败，与 Compose 文件无关。

### 显示 unhealthy

在 Dockge 中打开日志，然后检查：

- `data` 目录是否可写；
- 端口是否已被占用；
- `/api/health` 是否返回 `{"status":"ok"}`；
- 数据源请求是否受到网络或 DNS 限制。
