# 本机 API

所有接口以 `/api` 为前缀。当前服务面向本机单用户，默认仅监听 `127.0.0.1`。

| 方法 | 路径 | 行为 |
| --- | --- | --- |
| GET | `/health` | 返回 `{ "status": "ok" }` |
| GET | `/settings` | 返回咨询／销售、模型、语音与电话配置，凭据只返回是否设置 |
| PUT | `/settings` | 保存完整配置，返回脱敏后的配置 |
| GET | `/telephony/providers` | 电话 provider 元数据及字段类型，不连接供应商 |
| GET | `/telephony/inbound` | 本机接听状态；云 `awaiting_callback` 不是实际线路健康检查 |
| POST | `/telephony/{aliyun或tencent}/inbound` | 正式来电路由，鉴权后争用单会话槽并绑定已有远端通话 |
| POST | `/telephony/calls` | 显式创建电话，body 为 `provider` 与 `destination`，返回 `call` |
| POST | `/telephony/aliyun/llm` | 阿里平台流式模型网关，独立授权码和本通标识验证 |
| POST | `/telephony/tencent/llm/chat/completions` | 腾讯 OpenAI 兼容模型网关，Bearer 授权码和本通标识验证 |
| POST | `/telephony/{aliyun或tencent}/events` | 官方最终回执，认证并关联已有电话 |
| POST | `/calls` | 使用已保存配置开始一通会话，返回 `call` 和 SDK `connection` 参数 |
| GET | `/calls/active` | 当前会话或 `null` |
| GET | `/calls` | 文字历史摘要列表，按开始时间降序 |
| GET | `/calls/{id}` | 详细记录，包含脱敏配置快照与文字 |
| POST | `/calls/{id}/end` | 结束会话；重复结束幂等返回原记录 |
| DELETE | `/calls/{id}` | 删除已结束记录，返回 204；活动通话返回 409 |
| POST | `/offer?call_id={id}` | SmallWebRTC SDP offer/answer |
| PATCH | `/offer?call_id={id}` | SmallWebRTC trickle ICE 候选 |

配置包含 `conversation.mode`（`consultation` 默认或 `sales`）、`consultation`（`product_info`、`instructions`、`opening`）、旧 `sales`（`goal`、`product_info`、`instructions`、`opening`）、`asr`、`llm`、`tts`、`voice` 和 `telephony`。两组对话资料独立保留。旧无模式的保存配置升级为咨询，复制产品资料，销售原组不变；省略对话分组保存时保留当前值。模型公共字段为 `base_url`、`model`、`timeout_seconds`。ASR 增加 `language`；TTS 增加 `voice`、`sample_rate`。语音配置为 `vad_start_seconds`、`vad_stop_seconds`、`vad_confidence`。
ASR / TTS 还可设置 `protocol` 为 `openai`（默认）或 `mimo`；MiMo ASR 语言为 `auto/zh/en`，MiMo TTS 采样率须为 24000。LLM 的可选 `thinking` 为 `enabled/disabled/null`，`reasoning_effort` 为 `none/low/medium/high/max/null`；`null` 表示省略参数并使用服务默认。应用不设置 token 输出上限。

保存模型密钥时：省略 `api_key` 表示保持原值，字符串表示替换，`null` 表示清除。保存时不要携带只读字段 `api_key_set`。配置允许暂存空字段；开始通话前检查必要配置。

校验错误返回 HTTP 422，`detail` 列表仅包含字段位置、错误类型与说明，不回显输入值。
API 根地址必须使用 HTTP / HTTPS、有效端口，不包含 URL 凭据、查询参数或片段；ASR 语言使用 Pipecat 支持的语言代码。

开始原生／网页会话前要求当前模式的产品资料（销售额外要求目标）、三个服务地址与模型名，以及 TTS 音色。云托管模式只要求本机 LLM 及当前模式资料，平台负责 ASR／TTS。密钥允许留空以接入无鉴权的本机服务。网页、外呼与来电共用一通限制，竞争返回 HTTP 409；原生来电忙线拒接，云拒接格式见下文。新配置只影响下一通。POST `/calls` 返回的 `connection` 可交给 Pipecat SDK `connect()`；默认无外部 ICE 服务器。

结束接口可省略 body，默认原因为 `user_hangup`；也可发送 `{ "reason": "connection_lost" }`。已有结束原因不被后到请求覆盖。SDK 状态与已提交文字通过 RTVI 数据通道同步，见 [语音管线](voice.md)。

咨询模式只提供咨询结束工具，不执行销售挽留：仅在明确要求结束或明确表示答疑结束时告别。销售模式保留 `retain_once`、最多一次挽留及后续拒绝／直接结束时的 `hang_up`；次数仅属于本通，打断不恢复。原生语音告别播放确认后结束；网页发出 RTVI `{ "type": "call-ended", "reason": "ai_hangup" }`，客户端等待断连后释放麦克风并获取最终记录。`ai_hangup` 由服务端生成，后到断连不覆盖它。含糊时继续当前模式对话，不猜测或询问是否挂断；云托管工具和播放限制见云文档。

会话状态为 `connecting`、`active`、`ended` 或 `failed`。详细记录包含时间、原因、`state`、`channel`、`provider`、`direction`、`caller`、`destination`、`remote_id`、`settings`、`transcript`。`channel` 为 `browser`（旧记录默认）或 `telephone`；电话 `direction` 为 `inbound/outbound`，来电主叫保存在 `caller`、接听号码在 `destination`。旧电话无方向时视为外呼。摘要增加 `conversation_mode`，旧历史默认销售。云来电路由发生于振铃时，不代表已经接听。文字项包含 `role`、`text`、`timestamp`、`interrupted`；中断项可能无正文，被打断生成稿以未完整播放的内部背景保留，不冒充已说内容。内部标记在 TTS 前过滤。旧文字保持原样，不保存录音或密钥。

网页语音未完成连接的预留槽位默认 30 秒后释放，可通过 `LLMAUTOTEL_CONNECTION_TIMEOUT_SECONDS` 配置。电话使用各 provider 的拨号、媒体或回执超时，该环境变量不控制电话。正常退出会结束活动会话；意外终止后再次启动会将未结束的历史标为 `failed` / `server_restarted`。

## 电话 provider 配置

`settings.telephony` 包含 `public_base_url` 和四组独立 provider。全部默认 `enabled=false`、`inbound_enabled=false`。总开关开启且启用接听后，原生后台开始监听；云接收认证路由回调，不自动外呼。读取配置不发起连接。`inbound_numbers` 是接听号码字符串数组，原生允许空、云必须填写。密码、云凭据及各 token 回读均只提供 `字段名_set`；省略凭据保留，`null` 清除，省略整个 telephony 或 provider 保留已存配置。

媒体模式沿用本机的 ASR／LLM／TTS；云模式使用平台托管 ASR／TTS 和本机 LLM 网关。字段由 `/telephony/providers` 返回的元数据描述，填写凭据和号码时不会把它们放入浏览器持久存储。Asterisk endpoint 模板仅允许 `PJSIP/` 前缀和一个 `{number}`，不接受格式说明、额外变量或换行。FreeSWITCH unicast 的远端地址要求 IPv4，并预留端口；云服务需要可访问的模型网关与事件回调。详细前置配置见 [电话 provider](telephony.md)。

电话开始请求例如 `{"provider":"asterisk","destination":"<被叫号码>"}`。provider 只接受 `asterisk/freeswitch/aliyun/tencent`；被叫号码只接受 7–15 位数字及可选开头加号。启用但配置不完整或未启用返回 422；已有网页或电话通话返回 409。返回 201 表示本机预留槽位并调度拨号，不代表供应商已接受或被叫已接听。之后通过现有 `/calls/{id}` 查询 `state/status/end_reason`，使用现有结束接口手动挂断。电话不会返回 SmallWebRTC 参数，也不允许使用 `/offer`。

云通话网关验证当前已接纳会话。阿里使用 `out_id/BizParam.call_id`，腾讯外呼使用 `LLMExtraBody.call_id`，腾讯来电使用正式提示词变量注入的 system 标记；平台 system 在本机转为实际咨询／销售提示词，绑定标记不进入回复。认证及本通标识检查先于 SSE。外部请求不能覆盖快照、模型、密钥或思考设置。新请求取消旧流，HTTP 断开仅取消本轮；真实模型失败请求挂断本通。模型生成稿不直接写为已播放历史。

无任何活动通话时，已开启接听且正确认证的无绑定模型请求可作控制台协议探测：阿里按官方调测格式返回 SSE；腾讯兼容标准 SSE 或非流式 `chat.completion` JSON。响应是固定的网关连通性文字，不调用 LLM、不建通话，也不代表电话或模型验收通过。只要带任意会话标识／绑定标记就拒绝探测；有活动通话时缺标识仍拒绝，不能绕过当前会话校验。腾讯控制台实际校验请求尚未真实捕获，完整创建流程需云账户验收。

## 云来电路由

`POST /telephony/{provider}/inbound?token=<回调鉴权码>` 只允许 `aliyun/tencent`，两开关开启、号码白名单与应用标识匹配后才接纳。阿里额外校验正式 `timestamp` 与 `auth` 头，腾讯使用能力 token。鉴权失败／关闭为 401，不调用云 SDK。会话按 provider 与远端 ID 幂等，已结束回调重试不会新建通话；配置在鉴权和接纳间变化会拒绝，避免混用两份配置。

阿里 body 为 `caller/callee/callId/applicationCode`，成功回 `{"code":"OK","bizParam":"<包含call_id的JSON>"}`。已启用并认证的忙线／配置缺失来电用 `HangupOperate` 请求挂断，返回 409；控制请求失败返回 503。腾讯 body 为 `Event=callInBound`、`SessionId`、`SdkAppId`、`CallInBound`，成功返回智能体号及本通提示词变量；忙线／缺配置返回正式空 `{"CallInBound":{}}`。云平台的默认变量／静态路由兜底仍需在控制台配置，不能据本机拒绝假定所有平台兜底均已关闭。完整协议和系统提示词模板见 [云电话](telephony-cloud.md)。

接听状态为 `disabled/incomplete/connecting/listening/failed/pending/awaiting_callback`；仅原生 `listening` 代表本机事件连接已建立，`awaiting_callback` 不探测云线路。详情见 [来电接入](telephony-inbound.md)。

云最终回执使用独立鉴权码，腾讯支持官方电话 BasicAuth；具体配置和原始请求格式见 [云电话文档](telephony-cloud.md)。未知会话不会新建记录，供应商通话标识必须匹配。批量阿里回执逐条处理，当前和历史通话可在同批上报；重复回执替换正式文字而不追加重复项。手动挂断后的回执仍可更新已有记录，保留已确定结束原因和结束时间。回执可在 provider 禁用后认证接收，但不启用连接或云查询；回执送达前轮换鉴权码会使旧回执失效。腾讯 CDR 的应用标识按记录快照核对，公开凭据不会写入历史。

本机管理接口没有账号系统，云部署只将上述认证模型网关、入呼路由及回执入口交给公网 HTTPS 反向代理，配置、控制、接听状态、历史和 WebRTC 留在本机或管理网。不要将整个 `/api` 公开转发。默认仍监听 `127.0.0.1`。
