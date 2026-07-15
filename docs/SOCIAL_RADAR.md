# 作者动态雷达部署指南

## 风险说明

X 的服务条款禁止未经许可通过非公开接口自动抓取。浏览器采集可能遇到登录验证、页面改版
或账号限制。本实现不会绕过验证码：检测到登录失效或访问限制后会停止该账号同步并在站内
显示错误。正式长期运行更建议替换为 X 官方 API 适配器。

## Dockge 配置

在 Dockge 的 MangaFinder `.env` 中至少设置：

```dotenv
COMPOSE_PROFILES=social
MANGAFINDER_SOCIAL_ENABLED=true
MANGAFINDER_SOCIAL_COLLECTOR_TOKEN=一段随机的至少32位字符串
MANGAFINDER_X_SESSION_HOST_DIR=/mnt/hdd/mangafinder/x-session
MANGAFINDER_SOCIAL_MEDIA_HOST_DIR=/mnt/hdd/mangafinder/social-media
MANGAFINDER_PUBLIC_BASE_URL=http://你的Linux局域网IP:8000
```

令牌可以在 Linux 上用 `openssl rand -hex 32` 生成。需要代理时设置
`MANGAFINDER_X_PROXY_URL=http://代理容器名:端口`。如果代理监听在某个 Docker 网桥地址，必须填写
采集器容器实际可达的地址（例如 `http://172.19.0.1:7897`），不要默认使用
`host.docker.internal`。可先从容器内测试该地址的 TCP 连通性。Cookie 所属浏览器的 User-Agent
建议原样填入 `MANGAFINDER_X_USER_AGENT`。

## 导入 X 会话

1. 在浏览器登录 `x.com`，打开开发者工具的 Network，刷新页面。
2. 打开任意发往 `x.com` 的请求，复制完整 `Cookie` request header。
3. 在仓库或 Dockge stack 目录执行：

```bash
python3 scripts/create-x-storage-state.py --output /mnt/hdd/mangafinder/x-session/storage-state.json
```

Cookie 输入不会回显，生成文件权限为 `0600`。不要把文件或 Cookie 提交到 Git。更新 Dockge
stack 后，在“作者动态雷达”中选择作者，先“自动查找候选”或手动输入 handle，再点击确认绑定。

## QQ 私聊

在 QQ 开放平台创建官方机器人，取得当前应用被授予的个人私聊能力后配置：

```dotenv
MANGAFINDER_QQ_BOT_ENABLED=true
MANGAFINDER_QQ_BOT_APP_ID=
MANGAFINDER_QQ_BOT_CLIENT_SECRET=
MANGAFINDER_QQ_BOT_USER_OPENID=
```

QQ 只接收作者、标题、展会、置信度、证据、原帖和站内链接，不发送成人封面。如果应用没有
主动私聊权限或投递失败，消息会留在 outbox 中退避重试，站内雷达不受影响。

## 故障排查

- Dockge 构建提示 `proxyconnect ... 127.0.0.1:7890 refused`：这是 Docker daemon 的镜像拉取
  代理，不是 `MANGAFINDER_X_PROXY_URL`。检查 `/etc/docker/daemon.json` 的 `proxies` 是否仍指向
  已停用端口；修改为实际端口后需要在合适的维护窗口重启 Docker。
- `X 登录会话已失效`：重新生成 `storage-state.json`，删除同目录的 `runtime-state.json` 后重启采集器。
- `X 页面加载失败或触发访问限制`：暂停扫描并降低频率；系统不会自动绕过验证。
- `连接 X 超时`：检查 `MANGAFINDER_X_PROXY_URL`。代理在宿主机监听时，确认它绑定的 Docker
  网桥地址与采集器所连接的网络一致，并从 `social-collector` 容器测试该地址和端口。
- `QQ 尚未配置`：情报仍会正常显示，outbox 每六小时重试一次。
- Agent 未配置：系统使用规则生成候选，但不会以“Agent 高置信”名义自动放行。
