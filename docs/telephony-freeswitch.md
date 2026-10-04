# FreeSWITCH provider：ESL 与原生双向 PCM

FreeSWITCH provider 默认关闭。配置并启用后才在显式发起电话时建立 ESL 和 UDP 连接；保存配置不会外呼。AI 使用现有 ASR、LLM、TTS、上下文和打断管线，电话音频由独立 transport 适配。

## 适用范围

支持客户已配置在 FreeSWITCH 中的 SIP gateway。运营商 SIP 中继、把模拟固话转换为 SIP 的 FXO 网关、E1/PRI 网关及支持 SIP 的移动语音网关，都配置为 FreeSWITCH gateway，使用同一个 provider。应用不代替运营商开通号码，也不远程配置物理网关。

接口按 **FreeSWITCH v1.10.12 官方源码**核对：`mod_event_socket` 用于鉴权、后台 originate、接听和挂断事件；`sendmsg / call-command: unicast` 用于双向原始 PCM。无需 `mod_audio_stream` 商业双向版本或额外音频插件。协议模拟测试覆盖实际 TCP/UDP 字节交换，尚未对真实 FreeSWITCH 实例、运营商线路或物理电话验收。

仅支持 **8 kHz、单声道、16 位 PCM，小端 FreeSWITCH 主机与应用主机**。外呼约束 SIP codec 为 PCMA/PCMU，并在接听后读取 `read_rate` 验证 8000；不满足时结束会话，不把其他采样率误标为 8 kHz。原生 unicast 的 `L16` 名字不能直接等同于 RTP 的网络字节序 L16：该接口直接收发 FreeSWITCH 核心解码后的原生样本，首版要求双方小端。

## 准备 FreeSWITCH

客户管理员应先完成运营商或网关的 SIP 接入，在 `sofia/gateway/<网关名>/<号码>` 下可以正常呼叫。应用只接受已存在的 gateway 名称，不创建或写入 SIP 注册配置。

FreeSWITCH 需要加载 `mod_event_socket`，在 `event_socket.conf.xml` 配置独立的强密码及应用主机 ACL。ESL 是明文控制通道，原生 unicast UDP 没有加密、认证、序号或重传；部署到同机、可信专网或 VPN 内，并由防火墙限定通信双方。不要把这些端口直接暴露到公网。应用不会回显 ESL 密码或原始命令错误响应。

管理员还需在 FreeSWITCH 主机上预留一个可用 UDP 端口。当前应用同时仅允许一通会话，因此一个端口足够。应用无法探测远程主机端口占用情况，也不会替远程 FreeSWITCH 随机选端口。

## 填写配置

| 字段 | 填写内容 |
|---|---|
| `enabled` | 默认 `false`，完成配置后主动启用 |
| `host`、`port` | 应用可达的 ESL 地址与端口；默认端口 8021 |
| `password` | ESL 密码，保存在服务端；回读只显示是否已设置 |
| `gateway` | 客户已在 FreeSWITCH 配置的 SIP gateway 名称 |
| `caller_id` | 线路方准许的主叫号码 |
| `ring_timeout_seconds` | 振铃等待时间，默认 45 秒 |
| `media_timeout_seconds` | 接听后等待首包音频时间，默认 10 秒 |
| `cleanup_timeout_seconds` | 停止后台外呼和确认挂断的总等待预算，默认 5 秒，范围 1–10 秒 |
| `fs_media_host` | FreeSWITCH 用于绑定 unicast 的实际 IPv4 地址，必须从应用可达 |
| `fs_media_port` | FreeSWITCH 主机上预留的 UDP 端口；默认 0 表示未配置，启用时必须填写 |
| `audio_bind_host` | 应用接收音频的本地 IPv4 绑定地址；同机默认 `127.0.0.1`，跨主机时选对应专网网卡或 `0.0.0.0` |
| `audio_bind_port` | 应用本地 UDP 端口；默认 0，由操作系统选空闲端口 |
| `audio_advertised_host` | FreeSWITCH 能访问的应用 IPv4 地址；不能填写 `0.0.0.0` |
| `playback_tail_seconds` | 末包发出后等待电话媒体传递的缓冲时间，默认 0.1 秒，可按实测调节 |

同机时 `fs_media_host`、`audio_advertised_host` 都可以填回环地址；跨主机时填写双方实际专网地址，保证 UDP 双向可达。`fs_media_host:fs_media_port` 同时是应用唯一可信的收包来源；源地址、端口不匹配的包和无效 PCM 会被丢弃。

FS 的 `local-ip/local-port` 指 **FreeSWITCH** 地址；`remote-ip/remote-port` 指 **应用** 地址，不能填反。应用将真实绑定的 `audio_bind_port` 写入 `remote-port`，所以本地端口为 0 时也能正确协商。

## 呼叫、打断与挂断

1. 先绑定应用媒体 UDP 端口，再认证 ESL、订阅电话事件。
2. 用应用分配的 UUID 发起 `bgapi originate ... &park()`，屏蔽早期媒体，并限定 PCMA/PCMU。
3. 仅处理对应 UUID 的接听、挂断和 originate 后台结果，忽略其他通话事件。
4. 收到接听事件后验证 `read_rate`、启用 unicast。只有收到首个可信 PCM 包才触发媒体就绪与 AI 开场。
5. 公共 Pipecat transport 负责 ASR 输入重采样、TTS 输出重采样和实时发送节奏；provider 直接发送 PCM，不保留长播放队列。打断取消公共 transport 的旧音频任务，provider `flush()` 撤销播放等待，迟到的旧音频由公共 transport 的回合隔离阻挡。
6. 告别完成后等待末包音频时长及配置的媒体尾部时间，再 `uuid_kill ... NORMAL_CLEARING`。对方挂断、忙线、无人接听、媒体超时和 ESL 断连均结束会话并释放连接。关闭活动 driver 和启动中取消也会尝试挂断；若原 ESL 已断开，则新建短时控制连接，只挂断本会话 UUID，避免 `park()` 的电话失去控制后继续占线。

`bgapi` 的 `+OK Job-UUID` 只说明后台任务已接受，通道可能稍后才创建。挂断时第一次 `No such channel` 不能视为成功：应用保留事件读取，跟踪同 UUID 的 `CHANNEL_CREATE` 与 `BACKGROUND_JOB`，在清理预算内继续尝试挂断同一通话，不重新拨号。已观察到该通道创建、后台任务完成或远端结束后，才能确认不存在遗留通道。若 FreeSWITCH 完全不可达或后台作业超出预算，界面显示挂断未获确认；本机仍释放 UDP、ESL 和后台任务，管理员应检查遗留通道。一次清理失败不会在随后 `close()` 中重新累计整个等待预算。

**播放进度的边界：**原生 unicast 没有用户手机侧的播放确认。`wait_played()` 根据末包发送时间和 `playback_tail_seconds` 等待，只能确认应用侧的发送节奏与尾部等待；不能保证用户逐字听完。UDP 无法撤回已发出的一包音频，打断后停止的是后续音频。实际手机停音、告别完整度以及 500 ms 停音目标仍需线路与设备实测，不能用模拟测试冒充。

## 协议测试与来源

`uv run pytest tests/test_telephony_freeswitch.py` 启动可控本地 TCP ESL 与 UDP PCM 对端，验证认证、实际 originate 网关和号码、事件与响应交错、双向 PCM、来源过滤、采样率校验、打断取消播放等待、挂断、媒体超时、后台外呼失败和资源释放。后台外呼模拟任务独立于 ESL 连接，覆盖确认接受后立即挂断、50 ms 后才创建通道的竞态及清理超时。不会拨打真实号码或使用真实模型。

- [FreeSWITCH 官方 Event Socket 说明](https://developer.signalwire.com/freeswitch/integration/event-socket/)
- [v1.10.12：原生 unicast、UDP 收发、`sendmsg` 解析](https://github.com/signalwire/freeswitch/blob/v1.10.12/src/switch_ivr.c)
- [v1.10.12：核心 PCM、L16、G.711 样本转换](https://github.com/signalwire/freeswitch/blob/v1.10.12/src/switch_pcm.c)
- [v1.10.12：`read_rate` 的设置](https://github.com/signalwire/freeswitch/blob/v1.10.12/src/switch_core_codec.c)
- [FreeSWITCH 官方 originate 示例](https://developer.signalwire.com/freeswitch/recipes/click-to-call/)
- [mod_audio_stream 维护者的社区版／商业双向版说明](https://github.com/amigniter/mod_audio_stream)
