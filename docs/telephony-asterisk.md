# Asterisk 电话 provider

本 provider 用 Asterisk 的 ARI 接听 SIP 来电，也保留主动外呼，并用原生 `chan_websocket` 双向传输音频。既有 ASR、LLM、TTS、提示词、打断和文字历史继续由本应用管理。Asterisk 负责运营商线路、电话信令、编码转换和电话媒体发送。

默认 `enabled=false`、`inbound_enabled=false`。入呼接听须同时打开总开关及来电接听开关，服务启动或保存生效配置后才建立监听连接；仅打开总开关保留原外呼行为，明确开始一次外呼时才连接。任何保存操作都不会主动拨号。本 provider 仍受应用的单通会话限制。

## 版本和前置条件

- 使用 **Asterisk 22.8.0 或更新的 22.x**，或 **23.2.0 及以上**。仅有 22.6.0 的 `chan_websocket` 不够：本适配需要 JSON 控制消息及 `externalMedia.transport_data`。20.x 至少需 20.18.0；升级选择由 PBX 管理员确认。
- PBX 已配置可用的 PJSIP 中继，并已确定可用主叫、被叫号码格式和业务用途。SIP 运营商线路或 FXO／E1／VoLTE 网关均先接入 PBX；应用不直接管理网关厂商的私有协议。
- 已加载 ARI、HTTP WebSocket、`chan_websocket`、PJSIP 及所需音频转换模块。`/ari` 和 `/media` 需从应用服务器可达。
- 原生媒体侧固定使用 `slin16`：单声道、16 kHz、16 位有符号 PCM。本应用使用小端 PCM；请在常见小端 Linux PBX 上接入并验证。电话线路的 G.711 等编解码转换由 Asterisk 处理，应用侧 TTS 音频统一重采样。

协议依据：[官方 WebSocket 通道文档](https://docs.asterisk.org/Configuration/Channel-Drivers/WebSocket/)、[22.8.0 Channels API 定义](https://github.com/asterisk/asterisk/blob/22.8.0/rest-api/api-docs/channels.json)、[22.8.0 Bridges API 定义](https://github.com/asterisk/asterisk/blob/22.8.0/rest-api/api-docs/bridges.json)。

## 填写配置

| 字段 | 含义 |
|---|---|
| `enabled` | provider 总开关，默认关闭 |
| `inbound_enabled` | 来电接听开关，默认关闭；与总开关同时启用才开始监听 |
| `inbound_numbers` | 业务被叫号码白名单；留空接受专属路由上的全部来电 |
| `ari_url` | PBX ARI 地址，以 `/ari` 结尾，例如 `https://<PBX 管理域名>/ari`；不能在 URL 中写账号、密码或查询参数 |
| `username` / `password` | `ari.conf` 的可写 ARI 用户；密码只保存在服务端，页面回读只显示是否已设置 |
| `app` | 本应用登记到 ARI 的 Stasis 应用名，默认 `llmautotel`；多个系统使用独立应用名 |
| `incoming_app` | 来电监听的 Stasis 应用名，默认 `llmautotel-inbound`，须与外呼 `app` 不同 |
| `inbound_marker` | Stasis 的唯一参数，默认 `llmautotel-inbound`；路由中必须填写一致的标识 |
| `inbound_reconnect_seconds` | 监听连接断开后的重连间隔，默认 5 秒 |
| `endpoint_template` | PBX 实际的 PJSIP 拨号模板，例如 `PJSIP/{number}@<中继名>`；仅允许一个 `{number}` 占位符 |
| `caller_id` | 线路方认可的主叫号码，最终显示由 PBX 和运营商规则决定 |
| `ring_timeout_seconds` | 等待被叫接听的最长振铃秒数；向 ARI 发送时向上取整为整数 |
| `media_timeout_seconds` | ARI 请求、WebSocket 握手和媒体初始化超时秒数 |
| `cleanup_timeout_seconds` | 两条 WebSocket 与三个远端资源删除共用的清理总预算，默认 5 秒，可配 1 至 30 秒 |

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

## 接听来电

入呼只要求 ARI 地址、用户名、密码、独立入呼应用名和路由标识，**不要求外呼拨号模板或主叫号码**。客户 SIP 线路可来自运营商或云通信服务商：将对方提供的 SIP 中继接到现有 Asterisk，再把业务号码的来电路由给本应用。云厂商必须实际提供标准 SIP 中继及号码路由；只有云平台 HTTP API 的账户不能当作 SIP 账号填写。

管理员将业务 DID 路由到下列 dialplan，替换示例中的业务号码、应用名和标识；不要将整个 PBX 的所有来电指向本应用。本应用不会自动编辑 PBX 配置。

```ini
[llmautotel-inbound-route]
exten => <业务号码>,1,NoOp(Product assistant incoming call)
 same => n,Stasis(<incoming_app>,<inbound_marker>)
 same => n,Hangup()
```

由应用在成功取得全局单通槽后回答来电，因此路由中不要预先 `Answer()`。`inbound_numbers` 匹配 `StasisStart.channel.dialplan.exten`，应填写实际进入这个路由时的被叫号码。SIP 线路的号码转换规则、DID 路由和防火墙由 PBX 管理员配置。[官方 Stasis 接管及应答说明](https://docs.asterisk.org/Configuration/Interfaces/Asterisk-REST-Interface-ARI/Getting-Started-with-ARI/)、[Asterisk 22 通道接口](https://docs.asterisk.org/Asterisk_22_Documentation/API_Documentation/Asterisk_REST_Interface/Channels_REST_API/)

监听器只接管指定 `incoming_app` 且参数恰为 `[inbound_marker]` 的 `StasisStart`。外呼使用独立应用，媒体通道没有来电参数，其他应用及标识的通话均不会被接听或挂断。专属路由中不在白名单内的来电，以及应用已有网页、外呼或来电会话时的新来电，会以 `reason=busy` 拒接，不占用会话槽或生成对话历史。

接纳后对**原来的 channel ID**执行 `POST /channels/{id}/answer`，不调用 originate；复用与外呼相同的媒体 bridge、PCM、播放确认、打断和告别机制。长驻监听器独占入呼应用的事件 WebSocket，单通 driver 使用隔离队列接收原通道与本通媒体通道事件，不建立第二条同应用 WebSocket。即使在单通后台任务尚未启动时挂断，来电对象也可以直接清理原通道。

监听连接失效会结束借用该连接的本应用来电；监听器按配置间隔重连，只等待新事件，不枚举并重接旧通道，同一 channel ID 不重复接听。关闭接听入口后，已有来电保留事件连接直到单通释放；延后停止任务有独立引用和异常处理，WebSocket 关闭失败仍释放 HTTP 客户端并报告脱敏错误。重新配置连接参数应由会话协调器在本通结束后应用。应用停止不影响未路由给本服务的其他 PBX 电话。

## 拨号、打断和挂断

1. 应用先登记 ARI 事件连接，再创建被叫 SIP 通道。振铃、忙线、拒接及未接听不会触发 AI 开场。
2. 被叫已接听且进入 Stasis 后，创建独立 mixing／proxy_media bridge 与 `externalMedia` 通道：`transport=websocket`、`encapsulation=none`、`connection_type=server`、`external_host=INCOMING`、`format=slin16`、`transport_data=f(json)`。
3. 读取 `MEDIA_WEBSOCKET_CONNECTION_ID`，连接临时媒体地址；核对 `MEDIA_START` 的通道 ID、音频格式和帧大小。继续等待媒体通道进入 Stasis，成功加入 bridge 后才向语音服务报告就绪并开场。
4. 入站音频使用 WebSocket BINARY 帧；控制使用 JSON TEXT 帧。出站音频按 Asterisk 最大消息大小分块，`START_MEDIA_BUFFERING`／`STOP_MEDIA_BUFFERING` 保留不满整帧的尾部，`MEDIA_XOFF`／`MEDIA_XON` 处理流控。
5. 用户插话时，语音服务取消旧生成和 TTS，driver 发送 `FLUSH_MEDIA` 清除 PBX 待播放队列；等待发送的旧音频及旧播放标记同时失效。迟到的 `MEDIA_MARK_PROCESSED` 不能完成新回合或触发旧挂断。
6. 每次等待播放完成使用独立 `MARK_MEDIA.correlation_id`，对应回执才认为此前媒体已由 PBX 处理；还发送 `REPORT_QUEUE_DRAINED`，但无关联 ID 的队列通知不单独完成播放等待。AI 告别须先等播放标记完成，再挂断；用户手动挂断立即停止通话。
7. 结束时取消后台准备任务。已发出的创建请求仍等待结果，单独受 `media_timeout_seconds` 约束；即使挂断先到，也在迟到创建完成后删除预先分配的 SIP 通道、媒体通道与 bridge，不重复发起外呼。之后两条 WebSocket 关闭与三个资源删除并行执行，共用 `cleanup_timeout_seconds`，404 表示资源已不存在。删除失败或超时会关闭本地客户端并报告清理失败，仍通知应用收尾并保存状态。

播放回执表示 **PBX 媒体队列进度**，不能证明用户手机扬声器已播放到某个字。实际线路、手机端延迟和回声需单独验收。失败不自动重拨，避免创建重复外呼。

挂断等待正在进行的创建请求时，最长可能先等待其独立请求期限，再使用清理总预算。PBX 或管理网络完全不可达、创建结果超时未知时，应用无法保证远端电话已经挂断；清理失败提示需要管理员在 PBX 确认本通通道状态，不能把本地资源释放当作远端挂断成功。

## 已验证和待验证

运行针对本 provider 的测试：

```bash
uv run pytest tests/test_telephony_asterisk.py -q
```

可控 HTTP 替身核对实际请求 URL、Basic 鉴权头、拨号参数、externalMedia、变量读取及 bridge 操作，并通过独立远端任务验证挂断先到、三个创建操作迟到完成后仍被清理；清理 HTTP 失败、共用超时、WebSocket 关闭异常、本地客户端释放与幂等收尾亦有覆盖。可控 WebSocket 替身验证振铃与 Stasis 时序、音频分块、流控、打断、旧结果迟到、关闭与错误信息。另有真实本机 WebSocket server 验证 Upgrade、子协议、TEXT／BINARY 帧和标记确认；全部测试只使用测试凭据，不发真实电话。

入呼测试验证接管原通道、共享 ARI 事件、实际 answer 与媒体参数、双向 PCM、标识隔离、忙线及白名单拒接、重复事件去重、监听断连重连、监听入口关闭保留已有通话，以及会话任务启动前的远端清理。

尚未验证实体 Asterisk、SIP 运营商／网关线路、真实号码来电或外呼、忙音与主叫显示、手机端音质、告别完整播放或 500 ms 内停音。甲方确定 PBX 和线路后，需在其测试线路上完成这些项目；不能把协议测试结果当作运营商联通验收。
