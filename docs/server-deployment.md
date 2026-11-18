# Linux 服务器部署

## 2026-11-18 启动失败定位与修正

北京时间 06:49 停止已空转约 3.5 小时的任务 937535 后取得真实日志：Qwen3 依赖安装在下载 `onnxruntime==1.32.0` 时触发 uv 默认 30 秒网络读取超时并退出，尚未加载模型；平台 RUNNING 不代表应用进程存活。该包被上游 Qwen tokenizer 导入，保留锁定依赖，不凭大小删除。生产 ASR、TTS 和实时业务依赖准备现复用候选测试已使用的下载策略：uv 读取期限 600 秒、5 次重试、4 路并发，pip 同样覆盖较慢镜像下载；显式环境变量优先，继续使用配置的阿里云镜像及原依赖锁。此次修正针对启动可靠性，不计作逐轮推理加速。新增生产 ASR/TTS/实时依赖安装策略回归，完整后端 560 passed、3 skipped、62 warnings，Ruff 通过；本机未下载模型或推理。修正版任务 938351 已于北京时间 21:48:45 开始 RUNNING；截至 22:18，官方模型入口仍无法连接，模型进程及加载是否完成尚未验证，状态与未完成验证见下文。官方公网入口此前的 TCP 超时与这次安装失败是两项独立证据。

## 2026-11-18 一次性部署接续

北京时间 21:38:04 在业务服务器启动一次性单元 `llmautotel-gpu-deploy-938351.service`，已复查 active，状态为 `waiting_gpu_health`。这不是新的 GPU 任务或定时任务：仅等待正式任务 938351 的官方健康接口，等待期限到 22:38:04；期限到达仍未 ready 时退出并记录，配置不变，正式 GPU 不由此脚本停止。任务已于 21:48:45 开始 RUNNING，但官方入口尚不通，语音尚未恢复。

接续过程要求配置与启动时一致、无活动通话，并复用已有部署脚本的 ready、模型目录、鉴权、有效 PCM 和安装源码核对；通过后备份并切换三组地址、重启。仅尝试一次会改变配置的部署，失败不会循环重启。切换后若无活动通话，运行三轮完整链路内存终端基准；它不等于浏览器或设备验收，也不代表 1 秒目标已通过。配置变化会退出，避免覆盖用户随后保存的设置。

受限状态文件为 `/var/lib/llmautotel/gpu-deploy-938351.json`，基准输出为同目录 `gpu-benchmark-938351.json`；后者只在实际执行后生成。服务器操作脚本位于 `/tmp/llmautotel-resume-gpu.py` 和 `/tmp/llmautotel-deploy-20261118.py`。可用 `systemctl status llmautotel-gpu-deploy-938351` 与该单元 journal 核对进度，停止此单元不会停止正式 GPU。

已通过六项可控安全检查：GPU 未 ready、配置变更、有活动通话、业务接口不可达均不调用部署；预检失败只尝试一次；成功分支才进入三轮基准。这些检查模拟网络和子进程，没有调用真实模型或重启服务，不能算作部署冒烟成功。 PCM 预检另补七项受控检查：零音频、幅值 299 的近静音、奇数字节、长度不足、阈值 300 边界、有效负幅值及自定义阈值；全部通过，拒绝分支均未触达通话检查、配置写入或重启。预检要求至少 480 个 16 位小端样本，RMS 默认不低于 300，可用 `--signal-threshold` 调整，避免把非零噪声当成有效语音。服务器与本机脚本 SHA-256 一致。21:42 的账号任务列表为 RUNNING / STARTING / RESTARTING 均为 0、只有 938351 等待；22:14 再查 RUNNING 为 1、WAITING 为 0，938351 的身份通过独立 brief 接口确认，未发现遗留实验占卡。

> 2026-11-18 当前部署：业务后端 `4023485` 的独立环境已核对全部 29 个安装源码，认证 HTTPS 已验证。替代正式任务 [938351 / voice-qwen3-20261118r2](https://openi.pcl.ac.cn/cloudbrains/detail/938351) 使用 GPU 私库 `9468f82`、A10040GB 与 Qwen3-TTS graph4，于北京时间 21:48:45 开始 RUNNING；22:17 复查仍在运行，排队已结束。官方入口为 `https://f8cb3488e32a490a.mlunotebook.openi.org.cn`，但业务服务器对三个入口 IP 的 TCP 443 均 3 秒超时，同机 OpenI 主站 TCP 443 为 42.1ms；本机绕过 TUN 的物理直连对照也失败。平台日志和日志下载均只返回“运行结束后再查看”，官方调试入口同样不可达，因此尚未确认模型进程存活或加载完成，不能把连接超时计入模型推理延迟。三组模型地址仍指向已停止的旧任务，语音服务未恢复，配置切换与新端到端验收尚未完成。正式 GPU 保持运行，实验任务均已结束；服务器一次性接续仍在等待 ready，先通过健康、鉴权、有效 PCM 等预检后才切换。 详见[部署与网络证据](server-deployment.md#2026-11-18-正式任务运行后入口复查)；历史 ready 和延迟数据不能证明当前可用。

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

## GPU 整条业务管线（新版）

`voice.pipeline=gpu` 把原 Pipecat 业务管线移到常驻 GPU 进程：16kHz 单声道 PCM 经一条鉴权 WebSocket 进入，VAD、ASR、上下文、LLM、分句与 TTS 在 GPU 端衔接。三模型直接调用内存适配层，不再经业务服务器分别发起 HTTP 请求。业务服务器保留网页/电话入口、输出队列与历史落库。`local` 保留原独立 API 编排，不自动故障回退。

配置页的“处理链路”可切换；GPU 模式使用文本模型根地址推导 `/realtime`，使用文本模型密钥鉴权。远端 `serve_realtime=true`，三模型名、24kHz PCM 和参考音色必须与服务匹配。新增 `requirements-realtime.lock` 固定 Pipecat 运行依赖，复用镜像模型栈，不下载新的 Torch/CUDA。快照去除全部模型密钥及电话线路凭据；GPU 不保存音频和对话。

GPU 按生成代次标注音频；插话后发出 flush，服务器清播放队列并丢弃过期音频。句子提交和挂断必须等服务器输出队列的 played 确认，过期确认不能恢复旧回复或执行挂断。保留现有咨询、销售、一次挽留和告别规则。单个 GPU 服务只允许一通实时会话，重复占用明确失败，断连释放任务和占用。

停口确认现在支持100ms，较短阈值可能拆开自然停顿，需要真实麦克风/电话验证。响应目标仍按停口到终端有效声音计时；不能将独立原型数据作为新版正式链路数据。业务服务器与 OpenI 模型服务器分别更新；当前账号只允许一个 GPU 在线任务，更新 GPU 时需安排模型加载中断窗口。常驻服务不自动到期关闭。


2026-11-15 新版业务发布使用 `/opt/llmautotel/releases/20261115T140000Z-ce1c289`。复用依赖时，`.venv` 必须指向旧发布环境的实际绝对路径，不能指向 `current/.venv`，否则切换后会形成循环；systemd 通过 `PYTHONPATH=/opt/llmautotel/current/src` 加载当前源码。新发布目录及父目录须允许服务用户读取和进入。发布前备份 SQLite 和原链接，确认无活动通话，重启后验证健康接口与新配置字段。

同日首音优化发布切到 `/opt/llmautotel/releases/20261115T152000Z-4428b0d`，复用上述实际虚拟环境并重启 `llmautotel`。SQLite和原发布链接备份在 `/var/backups/llmautotel/20261115T152000Z-first-audio`；重启后健康ok、NRestarts=0、源码散列一致。OpenI任务更新为935318，模型配置同步为新分配入口，详见[首音优化记录](openi-model-service.md#2026-11-15-首音优化与重启)。

### 2026-11-16 播放语速

业务服务器在本地／GPU 两条链路的播放出口使用 PyAV `atempo` 流式保音调变速，`voice.playback_speed` 默认 1.0，范围 0.8～1.5；本次线上配置为 1.15。配置页“语音节奏与响应 → 播放语速”可调整，下一通生效。GPU 快照不传此字段，避免重复加速并兼容旧服务；无需重载 GPU。句子播放确认前排空滤镜尾音，打断时丢弃旧缓冲。变速可能增加少量滤镜缓冲，不代表首音延迟进一步下降。

### 2026-11-16 恢复原速与阶段统计

用户反馈1.15倍播放出现断续，线上已将 `voice.playback_speed` 恢复为1.0；该值直接旁路变速滤镜，下一通生效。原可配能力保留，GPU服务不重启。恢复前数据库备份在 `/var/backups/llmautotel/20261116-reset-speed`。WebUI增加默认展开的最新阶段明细、各轮比较及毫秒/占比；统计口径及0.78秒原始记录见 `openi-model-service.md`。

## 2026-11-17 管线优化上线

业务代码 `6dd1920` 已 GPG 签名验签并推送到 `codex/openi-latency-spike`，未合并 main。新发布目录为 `/opt/llmautotel/releases/20261117-pipeline-6dd1920`，使用独立 `.venv`；`current` 已切换并重启服务。旧链接指向 `/opt/llmautotel/releases/20261116-latency-ui-326fbab`，原共享环境 `/opt/llmautotel/releases/20261114T135053Z-888e4ad/.venv` 保留。SQLite 与原发布信息备份在 `/var/backups/llmautotel/20261117T134807Z-pipeline-6dd1920`，目录 0700、数据库及 manifest 文件 0600；部署元数据保存于 `/var/lib/llmautotel-deploy-pipeline.json`。

业务服务 active、NRestarts=0，健康接口 ok，无活动通话，主要源码散列与本机一致。公网 `https://103.52.153.233` 经系统 CA 校验通过；匿名访问 401，鉴权页面和 JS/CSS 均 200。未修改 Nginx、TURN、证书、入口凭据或电话开关。

GPU 私库 `1b440a8` 已签名推送，包含与业务版本一致的全部 29 个业务源码文件。当前常驻任务为 937289，健康 ready；服务根地址为 `https://dd06e868b51941ab.mlunotebook.openi.org.cn`。在无通话时仅切换三组模型的 `base_url` 至其 `/v1`，再完整核对其余配置及凭据与备份一致。模型不变、语速 1.0、100ms 停口确认保持。两次开场及重连成功；正式问答单轮内存终端有效首音 1049.2ms，GPU 统计 927.1ms，尚不能宣称一秒验收通过。

三次插话均有真实识别及新回复，但模型未调用挂断工具，自动挂断未通过，暂需手动结束。等待后的 WebSocket clean close 1000 原因未确定；本次没有进行浏览器或电话设备验收。详细成功与失败结果见[GPU 上线记录](openi-model-service.md#2026-11-17-管线优化上线)和[脱敏报告](../experiments/openi_latency/results/2026-11-17-pipeline-deployment.json)。

回滚前先检查活动通话、可用 GPU 地址及匹配的业务包，再恢复相应发布链接和配置。旧 GPU 任务已经停止，直接恢复旧地址不会恢复服务；若重建任务，须重新核验平台分配地址及实际 ready。数据库备份包含历史时点的数据，不能直接覆盖上线后新增的历史。旧共享虚拟环境仍承载此前发布，清理时不要删除它。


## 2026-11-18 运行环境核对与官方入口

发布不能只核对 `current/src`：非 editable 安装实际从 `.venv/lib/python*/site-packages/llmautotel` 读取。此前共享环境仅 `latency.py` 与新版源码不一致，其余 28 个文件相同；本次按锁创建独立环境，29 个安装文件逐一 SHA-256 匹配，并核对 CLI shebang 指向新环境。`current` 已原子切换至 `/opt/llmautotel/releases/20261118-qwen3-4023485`，重启与认证 HTTPS 验证成功。配置数据库及旧指针备份在服务器受限目录，发布过程未改变模型、提示词、历史或 API 地址。

OpenI 创建页面明确禁止穿透，因此撤销可选反向 SSH 方案，仅使用平台官方在线推理入口；相关辅助程序、配置、受限账号和授权文件均已移除，sshd 配置检查及 reload 成功。GPU 私库提交 `d01df2d` 只使用官方入口。新任务仍须通过实际健康、鉴权与有效 PCM 检查后才切换三组地址；平台 RUNNING、端点分配及旧任务启动日志均不能代替该验收。

## 2026-11-18 正式任务运行后入口复查

21:48:45 任务 938351 开始 RUNNING，22:17 平台仍为 RUNNING / `-`；端点分配接口返回 code=0。业务服务器 22:17 的三个公网入口 IP `120.237.18.54`、`210.22.22.132`、`218.17.115.247` 的 TCP 443 分别 3009.4 / 3008.7 / 3004.4ms 超时，同机主站 `117.163.55.142:443` 为 42.1ms。此前 22:10 对 80 和 443 的检查也全部超时。本机 22:11 使用 `SO_BINDTODEVICE=enp7s0` 绕过 TUN 的六项直连对照均 3 秒超时，没有修改代理、路由或 DNS。Google 与 Cloudflare DoH 返回相同的三个 A、无 AAAA，无法归因于本机假 DNS。

浏览器访问模型健康和官方调试 URL 均 `ERR_CONNECTION_CLOSED`；OpenI 主站接口正常。22:18 官方日志下载 HTTP 200 但内容仍只有 70 字符的“任务运行中，结束后查看”提示，日志 API 同样没有实际日志；node_info 只有空日志文件名，资源占用不支持，结果目录无文件。因此模型启动成功、下载修正实际通过、监听 8000 和 ready 均未验证。连接失败在 TCP 层，没有产生新模型推理延迟样本。为读取日志而停止或重建会丢失当前运行服务与资源位置，本轮未执行。

22:16 服务器一次性接续单元仍 active、PID=184132、phase=`waiting_gpu_health`，截止仍为 22:38:04。状态文件的 `checked_at` 是阶段切换时间，不是探测心跳；应结合 fresh systemd 状态判断运行情况。新三轮基准文件尚未生成，配置尚未切换。完整脱敏证据见[部署报告](../experiments/openi_latency/results/2026-11-18-tts-deployment.json)。
