# 本机 API

所有接口以 `/api` 为前缀。当前服务面向本机单用户，默认仅监听 `127.0.0.1`。

| 方法 | 路径 | 行为 |
| --- | --- | --- |
| GET | `/health` | 返回 `{ "status": "ok" }` |
| GET | `/settings` | 返回销售、模型与语音配置，模型含 `api_key_set`，不返回密钥 |
| PUT | `/settings` | 保存完整配置，返回脱敏后的配置 |
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

AI 基于明确用户意愿调用服务端 `hang_up` 工具后，告别输出完成才发出 RTVI `{ "type": "call-ended", "reason": "ai_hangup" }` 并优雅断开。客户端先标记正常结束，等待断连后释放麦克风并用现有结束接口取最终记录。`ai_hangup` 由服务端生成，不增加客户端可提交的结束原因；断连收尾不会覆盖它。意思不明确时保持会话、继续销售对话，不询问用户是否要挂断。

会话状态为 `connecting`、`active`、`ended` 或 `failed`。历史详细记录包含 `id`、`started_at`、`ended_at`、`status`、`end_reason`、`settings`、`transcript`。文字项包含 `role`、`text`、`timestamp`、`interrupted`。中断项可能没有正文，表示正在播放的半句未计入已说内容；生成稿会以“未完整播放”的内部系统背景保留在本通模型上下文，不写为完整播放的历史正文。内部背景标签在 TTS 前过滤，不作为新的语音或文字输出。过去已经保存的记录保持原样。记录不含录音或密钥。

未完成连接的预留槽位默认 30 秒后释放，可通过 `LLMAUTOTEL_CONNECTION_TIMEOUT_SECONDS` 配置。正常退出会结束活动会话；意外终止后再次启动会将未结束的历史标为 `failed` / `server_restarted`。
