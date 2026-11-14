# Linux 服务器部署

使用 Python 3.12、uv 和 systemd。前端在本机构建后随代码发布，服务器不需要安装 Node.js，也不需要把 GitHub 登录凭据复制过去。应用由独立的 `llmautotel` 用户运行，配置和模型密钥保存在 `/var/lib/llmautotel/app.sqlite3`，代码放在 `/opt/llmautotel/releases`，`current` 链接选择当前发布。

默认服务仅监听 `127.0.0.1:8765`。没有域名时，可通过 SSH 隧道打开本机地址，或配置下述受信任的公网 IP HTTPS 入口；浏览器允许 localhost 和受信任 HTTPS 使用麦克风。应用尚无账号系统，公网入口必须增加鉴权，保护管理、配置和历史。电话 provider 的原有开关沿用配置，部署不自动启用线路。

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

也可使用域名配置 HTTPS。云电话网关与回调仅开放文档列出的认证路径，其他接口留在管理网，见 [云平台接入](telephony-cloud.md)。

## 公网 IP HTTPS 与入口鉴权

Let’s Encrypt 已支持受信任的公网 IPv4／IPv6 证书，须选择 `shortlived` profile，有效期 160 小时。此部署使用独立的 Certbot 5.4.0 环境和 Nginx，不改变语音应用依赖。证书工具的全部已验证版本记录在 [证书工具依赖](../deploy/certbot-requirements.txt)，安装时可复用该文件：

```sh
UV_TOOL_DIR=/opt/llmautotel/tools UV_TOOL_BIN_DIR=/opt/llmautotel/tools/bin \
  uv tool install --python /usr/bin/python3.12 --no-python-downloads \
  --with-requirements deploy/certbot-requirements.txt certbot==5.4.0
```

先检查 Nginx 的现有站点、公开端口和静态目录。IP 地址直接访问通常不发送域名 SNI，若 443 已有其他证书和站点，优先选择空闲的独立 HTTPS 端口，不改变已有 TLS 默认站点。证书验证使用 HTTP 80；若现有 IP 站点能静态提供 `/.well-known/acme-challenge/`，复用其 webroot，Certbot 只创建短暂验证文件。否则只在该站点增加专用验证 location，保留原业务路由。不要覆盖或停掉其他站点来腾出端口。

将 `APP_PUBLIC_IP` 和 `APP_ACME_WEBROOT` 设置为实际地址与静态目录，创建 root 持有的专属目录，再申请生产证书：

```sh
install -d -m 0700 /etc/llmautotel/acme /var/lib/llmautotel-certbot /var/log/llmautotel-certbot
/opt/llmautotel/tools/bin/certbot certonly --non-interactive --agree-tos \
  --required-profile shortlived --webroot --webroot-path "$APP_ACME_WEBROOT" \
  --ip-address "$APP_PUBLIC_IP" --cert-name llmautotel-ip \
  --config-dir /etc/llmautotel/acme --work-dir /var/lib/llmautotel-certbot \
  --logs-dir /var/log/llmautotel-certbot
```

这是标准生产 CA 证书，不需要客户端导入私有根证书或绕过浏览器警告。Certbot 5.4.0 可不指定联系邮箱；需要通知或账号恢复联系时自行配置真实邮箱。参考 [Let’s Encrypt IP 证书与 Certbot 说明](https://letsencrypt.org/2026/03/11/shorter-certs-certbot)。

替换 [Nginx HTTPS 模板](../deploy/nginx-https.conf.example) 的监听地址、公开 Origin（须含非默认端口）、公开 IP、证书、私钥、密码文件和后端地址，放入 Nginx `http` 上下文中的独立配置。签发证书后才启用模板。后端继续只监听本机；所有网页、静态资源、REST、配置、历史与 SDP／ICE 接口均由 Nginx Basic Auth 保护。模板拒绝其他浏览器 Origin，允许命令行省略 Origin；登录 Authorization 在代理到后端前清除。无需修改前端：浏览器登录后为同源请求发送鉴权。

生成独立随机管理密码，不复用模型、云账号或 TURN 密钥。用 `openssl passwd -6 -stdin` 生成 crypt 哈希，按 `用户名:哈希` 保存为 `/etc/llmautotel/https.htpasswd`，由 root 持有、授予实际 Nginx worker 用户所在组读取、权限 `0640`。不要把密码作为命令参数、写入 Git 或输出到日志。供管理员取用的 `/etc/llmautotel/https-access.json` 应仅保存 URL、用户名和入口密码，root 持有、权限 `0600`；本机副本放在 Git 忽略的数据目录中并保持 `0600`。浏览器首次访问会显示登录框，使用该文件中的凭据。

配置和登录文件就绪后执行 `nginx -t` 并 reload，只重载 Nginx，不重启语音应用。不要用未经认证的健康接口作为公网特例。后续开通云电话时，另行配置其独立认证网关，不把管理密码交给云平台或解除整个入口的鉴权。

### 自动续期与验证

安装 [续期 service](../deploy/llmautotel-cert-renew.service)、[timer](../deploy/llmautotel-cert-renew.timer) 到 `/etc/systemd/system/`，将 [reload hook](../deploy/reload-nginx.sh) 安装为 `/etc/llmautotel/reload-nginx.sh`，root 持有、权限 `0700`。检查路径后启用 timer：

```sh
systemd-analyze verify /etc/systemd/system/llmautotel-cert-renew.service /etc/systemd/system/llmautotel-cert-renew.timer
systemctl daemon-reload
systemctl enable --now llmautotel-cert-renew.timer
systemctl start llmautotel-cert-renew.service
systemctl list-timers llmautotel-cert-renew.timer --no-pager
```

timer 每天检查两次，最多随机延后一小时，补执行错过的检查；Certbot 仅在证书需要续期时签发。成功后 hook 先验证 Nginx 配置，再 reload 加载新证书，不重启通话进程。续期日志使用独立目录并限制轮转保留数量。

```sh
/opt/llmautotel/tools/bin/certbot renew --dry-run --non-interactive \
  --no-random-sleep-on-renew --config-dir /etc/llmautotel/acme \
  --work-dir /var/lib/llmautotel-certbot --logs-dir /var/log/llmautotel-certbot \
  --deploy-hook /etc/llmautotel/reload-nginx.sh --run-deploy-hooks
journalctl -u llmautotel-cert-renew.service --no-pager
```

dry-run 使用 CA 的测试环境，只验证验证路径和部署 hook，不替换生产证书。HTTPS 自动化测试 `tests/test_https_deployment.py` 启动隔离的真实 Nginx、TLS 与受控后端，验证匿名／错误密码拒绝、静态及 API／信令全覆盖、登录凭据不转发、Origin 校验以及协议和端口保留。需要 Nginx／OpenSSL；缺少时跳过，可在已安装 Nginx 的服务器运行 `python3 tests/test_https_deployment.py -v`。公网部署还须用系统正常 CA 信任校验实际证书，并通过受保护的 offer／ICE 建立真实音频连接。

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

依赖不变而磁盘不足以保存第二份环境时，可让新发布的 `.venv` 链接到原运行环境，停服后执行上述 `uv sync` 并增加 `--reinstall-package llmautotel`，仅重装当前项目。此时多个发布共享已安装程序，单独切回 `current` 不会回滚 Python 包；回滚必须从旧发布重新安装该项目再启动。保留承载环境的原目录，勿在清理旧源码时删除它。磁盘充足时使用各自独立环境，切换和回滚更直接。

## 2026-11-14 新服务器部署

新入口为 `https://103.52.153.233`，应用代码版本 `888e4adf3fa2057bb5b5c14ce642c8fb4d47144f`，发布目录 `/opt/llmautotel/releases/20261114T135053Z-888e4ad`。Ubuntu 24.04 使用 Python 3.12、uv 0.13.8 和锁定的运行依赖，具有独立虚拟环境。应用、专属 TURN 和 Nginx 均已启动并设为自启动，核对时重启计数均为 0；后端只监听 `127.0.0.1:8765`。未修改旧服务器。

仅迁移当前配置及凭据，不复制本机历史；迁移前后配置摘要一致。数据库与服务用户权限沿用上文要求，咨询、销售、模型参数和电话开关均保留，四个电话 provider 仍禁用。管理登录沿用原有独立入口凭据；服务器副本位于 `/etc/llmautotel/https-access.json`，本机副本为 Git 忽略的 `data/https-access-103.52.153.233.json`，均为 `0600`。

已取得受系统 CA 信任的 Let’s Encrypt 公网 IP 证书；本次签发证书到期时间为 2026-11-21 13:01:54（Asia/Shanghai），后续以实际续期证书为准。续期 timer 已启用，实际 dry-run 与 Nginx reload hook 成功。公网正常 CA 校验下，匿名／错误密码返回 401，正确登录的页面与健康接口返回 200，异站 Origin 返回 403。TURN TCP 使用 3478、relay UDP 范围 55000–55100，并限制目标为应用服务器公网地址。

本次没有进行浏览器、麦克风、跨网络 WebRTC 音频或电话线路验收。服务启动、入口鉴权和中继监听不代表真实设备音频已连通。用户新提供的 DeepSeek Key 仅用于临时内存测试，未替换正式 MiMo／DeepSeek 配置，原账户的 402 未处理。成功短句样本与费用观察见 [响应速度测量](latency.md#新服务器与临时-deepseek-key-实测)。
