# 本机 API

所有接口以 `/api` 为前缀。当前服务面向本机单用户，默认仅监听 `127.0.0.1`。

| 方法 | 路径 | 行为 |
| --- | --- | --- |
| GET | `/health` | 返回 `{ "status": "ok" }` |
| GET | `/settings` | 返回销售、模型与语音配置，模型含 `api_key_set`，不返回密钥 |
| PUT | `/settings` | 保存完整配置，返回脱敏后的配置 |
| GET | `/telephony/providers` | 外呼 provider 元数据及字段类型，不连接供应商 |
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

配置包含 `sales`（`goal`、`product_info`、`instructions`、`opening`）、`asr`、`llm`、`tts` 和 `voice`。模型公共字段为 `base_url`、`model`、`timeout_seconds`。ASR 增加 `language`；TTS 增加 `voice`、`sample_rate`。语音配置为 `vad_start_seconds`、`vad_stop_seconds`、`vad_confidence`。
ASR / TTS 还可设置 `protocol` 为 `openai`（默认）或 `mimo`；MiMo ASR 语言为 `auto/zh/en`，MiMo TTS 采样率须为 24000。LLM 的可选 `thinking` 为 `enabled/disabled/null`，`reasoning_effort` 为 `none/low/medium/high/max/null`；`null` 表示省略参数并使用服务默认。应用不设置 token 输出上限。

保存模型密钥时：省略 `api_key` 表示保持原值，字符串表示替换，`null` 表示清除。保存时不要携带只读字段 `api_key_set`。配置允许暂存空字段；开始通话前检查必要配置。

校验错误返回 HTTP 422，`detail` 列表仅包含字段位置、错误类型与说明，不回显输入值。
API 根地址必须使用 HTTP / HTTPS、有效端口，不包含 URL 凭据、查询参数或片段；ASR 语言使用 Pipecat 支持的语言代码。

开始会话前要求销售目标、产品资料、三个服务的地址与模型名，以及 TTS 音色。密钥允许留空以接入无鉴权的本机服务。同时只有一通会话，重复开始返回 HTTP 409。新配置只影响下一通。POST `/calls` 返回的 `connection` 可直接交给 Pipecat SDK `connect()`；音视频仅连接本机后端，默认无外部 ICE 服务器。

结束接口可省略 body，默认原因为 `user_hangup`；也可发送 `{ "reason": "connection_lost" }`。已有结束原因不被后到请求覆盖。SDK 状态与已提交文字通过 RTVI 数据通道同步，见 [语音管线](voice.md)。

首次明确拒绝所配置目标行动，通过服务端 `retain_once` 工具温和挽留一次，保持会话；挽留后的新回合再次拒绝，或直接要求结束时，才允许 `hang_up`。工具内部沿用 `purchase_refusal` 意图名称，表示拒绝购买、订阅或其他销售目标，REST 字段不变。次数仅属于本通，打断不恢复。告别输出完成才发出 RTVI `{ "type": "call-ended", "reason": "ai_hangup" }` 并优雅断开。客户端先标记正常结束，等待断连后释放麦克风并用现有结束接口取最终记录。`ai_hangup` 由服务端生成，不增加客户端可提交的结束原因；断连收尾不会覆盖它。意思不明确时保持会话、继续销售对话，不询问用户是否要挂断。

会话状态为 `connecting`、`active`、`ended` 或 `failed`。历史详细记录包含 `id`、`started_at`、`ended_at`、`status`、`state`、`end_reason`、`channel`、`provider`、`destination`、`remote_id`、`settings`、`transcript`。`channel` 为 `browser`（旧记录默认）或 `telephone`；电话记录保存线路 provider、被叫号码及供应商通话标识，网页模式这些字段为 `null`。`state` 是界面反馈，电话拨号与振铃仍属 `status=connecting`。文字项包含 `role`、`text`、`timestamp`、`interrupted`。中断项可能没有正文，表示正在播放的半句未计入已说内容；生成稿会以“未完整播放”的内部系统背景保留在本通模型上下文，不写为完整播放的历史正文。内部背景标签在 TTS 前过滤，不作为新的语音或文字输出。过去已经保存的记录保持原样。记录不含录音或密钥。

网页语音未完成连接的预留槽位默认 30 秒后释放，可通过 `LLMAUTOTEL_CONNECTION_TIMEOUT_SECONDS` 配置。电话使用各 provider 的拨号、媒体或回执超时，该环境变量不控制电话。正常退出会结束活动会话；意外终止后再次启动会将未结束的历史标为 `failed` / `server_restarted`。

## 电话 provider 配置

`settings.telephony` 包含 `public_base_url` 和 `asterisk`、`freeswitch`、`aliyun`、`tencent` 四组独立配置。全部默认 `enabled=false`，旧数据库读取亦如此。读取或保存配置不会连接电话平台；只有启用并显式开始外呼才连接。密码、云凭据、模型网关 token、事件回调 token 回读均只提供 `字段名_set`。保存前排除这些只读标记；省略凭据保留原值，`null` 清除，省略整个 telephony 或 provider 保留已存配置。

媒体模式沿用本机的 ASR／LLM／TTS；云模式使用平台托管 ASR／TTS 和本机 LLM 网关。字段由 `/telephony/providers` 返回的元数据描述，填写凭据和号码时不会把它们放入浏览器持久存储。Asterisk endpoint 模板仅允许 `PJSIP/` 前缀和一个 `{number}`，不接受格式说明、额外变量或换行。FreeSWITCH unicast 的远端地址要求 IPv4，并预留端口；云服务需要可访问的模型网关与事件回调。详细前置配置见 [电话 provider](telephony.md)。

电话开始请求例如 `{"provider":"asterisk","destination":"<被叫号码>"}`。provider 只接受 `asterisk/freeswitch/aliyun/tencent`；被叫号码只接受 7–15 位数字及可选开头加号。启用但配置不完整或未启用返回 422；已有网页或电话通话返回 409。返回 201 表示本机预留槽位并调度拨号，不代表供应商已接受或被叫已接听。之后通过现有 `/calls/{id}` 查询 `state/status/end_reason`，使用现有结束接口手动挂断。电话不会返回 SmallWebRTC 参数，也不允许使用 `/offer`。

云网关仅在对应活动电话中工作。认证与本通 `out_id/BizParam.call_id` 或 `LLMExtraBody.call_id` 校验在 HTTP SSE 响应开始前完成；外部请求不能覆盖销售快照、模型地址、模型、密钥、思考参数或输出限制。新的请求取消旧生成流，客户端断开只取消本轮；真正模型失败请求挂断当前电话。模型生成文字不直接存为已播放历史。

云最终回执使用独立鉴权码，腾讯支持官方电话 BasicAuth；具体配置和原始请求格式见 [云电话文档](telephony-cloud.md)。未知会话不会新建记录，供应商通话标识必须匹配。批量阿里回执逐条处理，当前和历史通话可在同批上报；重复回执替换正式文字而不追加重复项。手动挂断后的回执仍可更新已有记录，保留已确定结束原因和结束时间。回执可在 provider 禁用后认证接收，但不启用连接或云查询；回执送达前轮换鉴权码会使旧回执失效。腾讯 CDR 的应用标识按记录快照核对，公开凭据不会写入历史。

本机管理接口没有账号系统，云部署只将上述认证模型网关与回执入口交给公网 HTTPS 反向代理，配置、外呼控制、历史和 WebRTC 入口留在本机或管理网。不要将整个 `/api` 公开转发。默认监听地址仍为 `127.0.0.1`。
