# Asterisk 电话 provider

本 provider 用 Asterisk 的 ARI 发起 SIP 外呼，并用原生 `chan_websocket` 双向传输音频。既有 ASR、LLM、TTS、提示词、打断和文字历史继续由本应用管理。Asterisk 负责运营商线路、电话信令、编码转换和电话媒体发送。

默认 `enabled=false`。保存配置不会连接 PBX、注册线路或拨号；只有启用后明确开始一次外呼才建立连接。本 provider 仍受应用的单通会话限制。

## 版本和前置条件

- 使用 **Asterisk 22.8.0 或更新的 22.x**，或 **23.2.0 及以上**。仅有 22.6.0 的 `chan_websocket` 不够：本适配需要 JSON 控制消息及 `externalMedia.transport_data`。20.x 至少需 20.18.0；升级选择由 PBX 管理员确认。
- PBX 已配置可用的 PJSIP 中继，并已确定可用主叫、被叫号码格式和业务用途。SIP 运营商线路或 FXO／E1／VoLTE 网关均先接入 PBX；应用不直接管理网关厂商的私有协议。
- 已加载 ARI、HTTP WebSocket、`chan_websocket`、PJSIP 及所需音频转换模块。`/ari` 和 `/media` 需从应用服务器可达。
- 原生媒体侧固定使用 `slin16`：单声道、16 kHz、16 位有符号 PCM。本应用使用小端 PCM；请在常见小端 Linux PBX 上接入并验证。电话线路的 G.711 等编解码转换由 Asterisk 处理，应用侧 TTS 音频统一重采样。

协议依据：[官方 WebSocket 通道文档](https://docs.asterisk.org/Configuration/Channel-Drivers/WebSocket/)、[22.8.0 Channels API 定义](https://github.com/asterisk/asterisk/blob/22.8.0/rest-api/api-docs/channels.json)、[22.8.0 Bridges API 定义](https://github.com/asterisk/asterisk/blob/22.8.0/rest-api/api-docs/bridges.json)。

## 填写配置

| 字段 | 含义 |
|---|---|
| `enabled` | 默认关闭；准备好线路后显式启用 |
| `ari_url` | PBX ARI 地址，以 `/ari` 结尾，例如 `https://<PBX 管理域名>/ari`；不能在 URL 中写账号、密码或查询参数 |
| `username` / `password` | `ari.conf` 的可写 ARI 用户；密码只保存在服务端，页面回读只显示是否已设置 |
| `app` | 本应用登记到 ARI 的 Stasis 应用名，默认 `llmautotel`；多个系统使用独立应用名 |
| `endpoint_template` | PBX 实际的 PJSIP 拨号模板，例如 `PJSIP/{number}@<中继名>`；仅允许一个 `{number}` 占位符 |
| `caller_id` | 线路方认可的主叫号码，最终显示由 PBX 和运营商规则决定 |
| `ring_timeout_seconds` | 等待被叫接听的最长振铃秒数；向 ARI 发送时向上取整为整数 |
| `media_timeout_seconds` | ARI 请求、WebSocket 握手和媒体初始化超时秒数 |

密码通过 HTTP／WebSocket `Authorization` 头发送，不加入事件 URL 查询参数。事件连接是 `ari_url/events?app=...`；媒体连接从同一地址去除最后的 `/ari` 后拼接 `/media/<临时连接 ID>`。例如 `/pbx/ari` 对应 `/pbx/media/...`，反向代理须同时转发这两个路径的 WebSocket Upgrade。应用直接访问 PBX，不继承环境代理。

PBX 端至少要开启 HTTP 和 ARI。以下是字段示意，管理员须换成实际地址和密码，并按现有 PBX 的 TLS 与访问控制配置合并；本应用不会自动修改 PBX 文件：

```ini
; http.conf：同机部署可使用回环地址；跨机部署使用 PBX 管理网地址。
[general]
enabled = yes
bindaddr = <PBX 管理网地址>
bindport = <管理端口>

; ari.conf
[general]
enabled = yes

[llmautotel-api]
type = user
read_only = no
password = <专用随机密码>
```

需要加密传输时配置 Asterisk HTTPS 或已有管理网反向代理，然后填写 `https://.../ari`。本模式由应用主动连接 PBX 的媒体 WebSocket，不需要 `websocket_client.conf` 回连客户端条目，也不需要 PBX 访问本机网页应用的公网回调地址。[官方 ARI 配置说明](https://docs.asterisk.org/Configuration/Interfaces/Asterisk-REST-Interface-ARI/Asterisk-Configuration-for-ARI/)

## 拨号、打断和挂断

1. 应用先登记 ARI 事件连接，再创建被叫 SIP 通道。振铃、忙线、拒接及未接听不会触发 AI 开场。
2. 被叫已接听且进入 Stasis 后，创建独立 mixing／proxy_media bridge 与 `externalMedia` 通道：`transport=websocket`、`encapsulation=none`、`connection_type=server`、`external_host=INCOMING`、`format=slin16`、`transport_data=f(json)`。
3. 读取 `MEDIA_WEBSOCKET_CONNECTION_ID`，连接临时媒体地址；核对 `MEDIA_START` 的通道 ID、音频格式和帧大小。继续等待媒体通道进入 Stasis，成功加入 bridge 后才向语音服务报告就绪并开场。
4. 入站音频使用 WebSocket BINARY 帧；控制使用 JSON TEXT 帧。出站音频按 Asterisk 最大消息大小分块，`START_MEDIA_BUFFERING`／`STOP_MEDIA_BUFFERING` 保留不满整帧的尾部，`MEDIA_XOFF`／`MEDIA_XON` 处理流控。
5. 用户插话时，语音服务取消旧生成和 TTS，driver 发送 `FLUSH_MEDIA` 清除 PBX 待播放队列；等待发送的旧音频及旧播放标记同时失效。迟到的 `MEDIA_MARK_PROCESSED` 不能完成新回合或触发旧挂断。
6. 每次等待播放完成使用独立 `MARK_MEDIA.correlation_id`，对应回执才认为此前媒体已由 PBX 处理；还发送 `REPORT_QUEUE_DRAINED`，但无关联 ID 的队列通知不单独完成播放等待。AI 告别须先等播放标记完成，再挂断；用户手动挂断立即停止通话。
7. 结束时关闭两条 WebSocket、取消后台任务，删除 SIP 通道、媒体通道与 bridge。忙线、未接听、拒接、断连和接入错误作为结束原因交给应用保存。

播放回执表示 **PBX 媒体队列进度**，不能证明用户手机扬声器已播放到某个字。实际线路、手机端延迟和回声需单独验收。失败不自动重拨，避免创建重复外呼。

## 已验证和待验证

运行针对本 provider 的测试：

```bash
uv run pytest tests/test_telephony_asterisk.py -q
```

可控 HTTP 替身核对实际请求 URL、Basic 鉴权头、拨号参数、externalMedia、变量读取及 bridge 操作；可控 WebSocket 替身验证振铃与 Stasis 时序、音频分块、流控、打断、旧结果迟到、关闭与错误信息。另有真实本机 WebSocket server 验证 Upgrade、子协议、TEXT／BINARY 帧和标记确认；全部测试只使用测试凭据，不发真实电话。

尚未验证实体 Asterisk、SIP 运营商／网关线路、真实号码呼叫、忙音与主叫显示、手机端音质、告别完整播放或 500 ms 内停音。甲方确定 PBX 和线路后，需在其测试线路上完成这些项目；不能把协议测试结果当作运营商联通验收。
