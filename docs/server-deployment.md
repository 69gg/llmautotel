# Linux 服务器部署

使用 Python 3.12、uv 和 systemd。前端在本机构建后随代码发布，服务器不需要安装 Node.js，也不需要把 GitHub 登录凭据复制过去。应用由独立的 `llmautotel` 用户运行，配置和模型密钥保存在 `/var/lib/llmautotel/app.sqlite3`，代码放在 `/opt/llmautotel/releases`，`current` 链接选择当前发布。

默认服务仅监听 `127.0.0.1:8765`。没有域名时，通过 SSH 隧道打开本机地址；浏览器允许 localhost 使用麦克风。应用尚无账号系统，不要把管理、配置和历史接口直接开放到公网。电话 provider 的原有开关沿用配置，部署不自动启用线路。

## 准备发布

先在本机检查提交和构建：

```sh
git status --short
git verify-commit HEAD
pnpm --dir frontend build
git rev-parse HEAD
```

将代码归档与 `frontend/dist/` 传到服务器新的发布目录，不上传 `.venv`、`node_modules`、GitHub token、SSH 私钥或本机历史。下面以 `APP_RELEASE` 为已创建的发布目录，在服务器安装运行依赖：

```sh
cd "$APP_RELEASE"
uv sync --locked --no-dev --no-editable --no-python-downloads --python /usr/bin/python3.12 --no-cache
```

`--locked` 核对锁文件，`--no-dev` 排除开发依赖，`--no-editable` 使运行包不依赖可写源码。磁盘空间不足时先评估安装余量；可将安装临时目录放到有足够空间的 tmpfs，并在安装后清理本次临时目录。不要为了安装而删除其他应用的数据、缓存或发布。

## 配置与凭据

创建独立系统用户和目录，使用新发布目录后，将 `deploy/llmautotel.service` 安装到 `/etc/systemd/system/`，将 `deploy/service.env.example` 安装为 `/etc/llmautotel/service.env`。按实际安装位置修改 unit 中的工作目录、启动路径，以及环境文件中的数据和前端路径。环境文件应由 root 持有并设为 `0600`。

模型和线路参数可在网页配置。迁移已有本机凭据时，用 SQLite 一致性读取，仅复制 `settings` 表的当前行到新的应用数据库，创建空 `calls` 表；通过 SSH 加密传输，避免把密钥放入命令行、终端输出、日志或 Git。数据库由服务用户持有，文件 `0600`、数据目录 `0700`。已有远端数据库应先备份，并在停服后迁移；更新发布时沿用原数据库，不重复覆盖配置或历史。

启用服务：

```sh
systemd-analyze verify /etc/systemd/system/llmautotel.service
systemctl daemon-reload
systemctl enable --now llmautotel.service
systemctl status llmautotel.service --no-pager
curl --fail http://127.0.0.1:8765/api/health
```

确认网页构建能加载、当前模式及欢迎语正确、三组密钥的公开状态为已设置，电话接听状态与预期一致。不要输出整个私有数据库或密钥来证明迁移成功。

## 无域名访问

在使用浏览器的电脑上建立隧道，替换服务器账号和地址：

```sh
ssh -N -L 127.0.0.1:8767:127.0.0.1:8765 root@SERVER
```

保持 SSH 连接，打开 `http://127.0.0.1:8767`，进入语音通话并允许麦克风。`8767` 是本机入口，可换成其他空闲端口；服务器端仍是环境文件指定的端口。SSH 隧道传输网页和信令，WebRTC 音频另行建立连接。默认直接使用 UDP；网络受限时，可配置下述 TURN TCP 中继。页面加载成功不代表音频已连通，必须另外验证协商与音频。

有域名后可配置 HTTPS 和管理访问控制。云电话网关与回调仅开放文档列出的认证路径，其他接口留在管理网，见 [云平台接入](telephony-cloud.md)。

## 可选 STUN／TURN

`LLMAUTOTEL_ICE_SERVERS` 是标准 `RTCIceServer` 数组 JSON，默认 `[]`。服务启动时复制同一份参数给 Pipecat SmallWebRTC 与浏览器 SDK，无需修改前端。支持 `stun:`、`turn:` UDP／TCP 及 `turns:` TCP；TURN 使用用户名和密码。锁定的 aiortc 只采用首个可用 STUN 和首个可用 TURN，配置多个地址不表示后端逐个故障切换。此配置只控制网页 WebRTC，不影响电话 provider。

在权限为 `0600` 的 systemd 环境文件中填写，替换为自己的中继地址与凭据：

```sh
LLMAUTOTEL_ICE_SERVERS='[{"urls":"turn:RELAY_HOST:3478?transport=tcp","username":"RELAY_USER","credential":"RELAY_PASSWORD"}]'
```

配置无效时进程拒绝启动，错误不回显原始值。TURN 凭据是浏览器连接中继所必需的参数，因此会通过 `POST /api/calls` 的连接配置发送给当前客户端；它们不会出现在配置回读、模型请求或 SQLite 历史中。保护管理入口和信令访问，使用该应用专属中继账号，不复用模型密钥。

可在同一公网服务器安装发行版 `coturn`，使用 [专属 unit](../deploy/llmautotel-turn.service) 和 [配置模板](../deploy/turnserver.conf.example)。模板按 Ubuntu 24.04 的 coturn 4.6.1 核对，其他版本需先检查支持的选项。模板要求替换公开 IPv4、用户名与随机密码；安装为 `/etc/llmautotel/turnserver.conf`，所有者 `root:turnserver`、权限 `0640`，父目录允许服务用户遍历。确认没有其他 TURN 服务或端口冲突后启用专属 unit，避免同时启动发行版默认服务。

模板使用 TCP `3478` 接收客户端连接，只允许 UDP relay 到指定应用服务器的公开 IPv4，禁止任意目标及内部网络，并限制分配数量和带宽。`no-tcp-relay` 禁止 RFC 6062 TCP relay 分配，不会关闭客户端至中继的 TCP 连接。此模板要求服务器直接持有公开 IPv4；NAT 后的服务器需额外配置 coturn `external-ip` 与端口映射。选择的 relay UDP 范围为 `55000–55100`，须按实际监听与防火墙调整，不能占用其他应用端口。

```sh
systemd-analyze verify /etc/systemd/system/llmautotel-turn.service
systemctl daemon-reload
systemctl enable --now llmautotel-turn.service
systemctl restart llmautotel.service
```

网页继续通过 SSH 访问，语音使用 WebRTC 自身的 DTLS／SRTP 加密。需要 TURN TLS 时，应配置有效证书并改用 `turns:` URL。中继运行状态、实际 ICE 候选和收到的非静音音频应分别检查。参考 [Pipecat 传输说明](https://docs.pipecat.ai/client/concepts/choosing-a-transport) 与 [coturn 4.6.1 官方配置说明](https://github.com/coturn/coturn/blob/4.6.1/examples/etc/turnserver.conf)。

## 日常操作与更新

```sh
systemctl status llmautotel.service --no-pager
journalctl -u llmautotel.service -n 50 --no-pager
systemctl restart llmautotel.service
systemctl stop llmautotel.service
```

更新时创建新发布目录、构建和安装依赖、核对签名后再切换 `current` 并重启；数据库保持在独立的数据目录。切换前结束活动通话。部署结果应分别报告服务健康、配置与凭据迁移、模型连通、真实 WebRTC 音频，以及手机线路验收，不能仅凭 systemd 显示 active 宣称全部可用。
