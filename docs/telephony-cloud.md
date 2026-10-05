# 阿里云与腾讯云托管语音接入

这两种 provider 默认关闭，来电接入另有独立开关，默认同样关闭。它们由云平台处理电话音频、ASR、TTS 和语音打断，本机保留产品咨询／销售配置、文本模型、模型密钥及文字历史。需要继续使用当前 MiMo ASR/TTS 和本机 Pipecat 音频管线时，选择 Asterisk 或 FreeSWITCH 接入；云托管模式的语音模型由电话平台配置。

```text
用户电话 ⇄ 云平台 ASR / TTS / VAD
                    ↓ HTTP 流式请求
              本机模型网关
                    ↓ 已配置文本 LLM
               流式文字 / 原生挂断指令
```

应用只把本机网关的鉴权码提供给云平台，真实文本模型 API 密钥仍用于本机发起的模型请求。每通使用开始时的配置快照，平台请求里的 model、system、thinking、effort 和 max_tokens 不能覆盖本机设置。当前 DeepSeek 关闭思考、reasoning effort 为 high、不设置 max tokens 的规则也适用于云模式。

## 公共配置

先选择对话模式并填写对应产品资料、回复要求及文本模型；默认是产品咨询，旧销售目标和话术仍可通过销售模式启用。云模式无需填写本机 ASR/TTS 接口。电话配置中的公共访问地址必须能让平台访问本机模型网关、来电路由及回执接口。开发环境的 `localhost` 仅能被本机访问，不是云平台可达的地址。

| 配置 | 用途 |
| --- | --- |
| 启用 | 总开关，默认关闭；外呼仍须点击发起电话 |
| 启用来电接入 | 独立开关，默认关闭；与总开关一起启用并配置云路由后，用户拨打号码会自动接入 |
| 来电接入号码 | 本机允许接待的被叫号码列表；需与平台号码绑定一致，兼容大陆本地和 `0086` / `+86` 格式 |
| API 地址、地域 | 官方云控制 API 的根地址和账户使用的地域，不带查询参数或路径 |
| 主叫号码 | 云平台已经授权的号码，按供应商要求填写号码格式 |
| 模型网关鉴权码 | 独立高熵随机值；与真实 LLM 密钥分开 |
| 回执鉴权码 | 独立高熵随机值；用于回执和来电路由 URL 的能力认证，腾讯 CDR 另可配置原生 BasicAuth |
| 平台音色 | 平台支持的音色名称；不使用本机 TTS 的音色参数 |
| API 超时 | 云控制请求的等待时间，不是通话时长 |
| 通话与回执等待上限 | 防止遗漏最终回执后长期占用通话槽位；达到上限请求立即挂断并释放资源 |

控制 SDK 已锁定为 `alibabacloud-aiccs20191015==5.4.3`、`tencentcloud-sdk-python-ccc==3.1.177`、`tencentcloud-sdk-python-common[async]==3.1.185`，具体依赖由 `uv.lock` 固定。签名和请求序列化复用官方 SDK；应用不打印 SDK 请求体、密钥或上游错误正文。

## 来电接待与线路关系

来电接入复用这两家平台的正式语音智能体，用户拨打平台已绑定的号码，平台回调本机路由。本机接纳后绑定供应商提供的 `callId` / `SessionId`，使用接纳时的配置快照回答问题，不再调用 `LlmSmartCall` 或 `CreateAICall` 拨号。来电、旧外呼及浏览器通话共用单通槽位。

阿里云 AICCS 与腾讯云 TCCC 都支持平台号码和自有号码方案。企业 SIP 中继接入平台时，语音仍由平台托管，不能仅凭“SIP”将云回调当成 PCM 音频接口。若线路商提供标准 SIP 中继给自建 PBX，可通过本项目的 Asterisk／FreeSWITCH 接入保留独立 ASR、TTS、LLM。腾讯的自有号码方案见[SIP Trunk 对接](https://cloud.tencent.com/document/product/679/73527)，阿里的自带线路需要[内部邀约加白](https://help.aliyun.com/zh/aiccs/enterprise-own-line)。

平台号码开通、套餐、智能体创建、号码绑定和互联网可达地址必须先完成。保存本机配置不会购买线路、修改云账户或启用生产号码。

## 阿里云 AICCS

### 来电配置（优先）

在[AICCS 大模型应用管理](https://help.aliyun.com/zh/aiccs/user-guide/big-model-application-management)中创建应用并绑定本机大模型网关，将支持呼入的阿里云号码或已加白的自带线路号码绑定至该应用。一个呼入号码只能绑定一个应用。本机填写同一应用编码、阿里云账号 UID、来电号码以及 AccessKey 与公共配置，开启总开关和来电接入。

应用的呼入回调配置填写 `<公共访问地址>/api/telephony/aliyun/inbound?token=<回执鉴权码>`。官方来电请求为：

```json
{"caller":"主叫号码","callee":"被叫号码","callId":"平台通话ID","applicationCode":"应用编码"}
```

请求头为 `timestamp`（毫秒）和 `auth`，后者按官方协议计算 `MD5(caller + uid + timestamp)` 的小写十六进制。除能力 token 外，本机还校验该签名、五分钟时间窗口、应用编码和被叫号码；账号 UID 不等于一个强秘密，不能只靠这个 MD5 公式保护公开接口。

接纳时返回 `{"code":"OK","bizParam":"{\"call_id\":\"本机通话ID\"}"}`；平台把 `bizParam` 作为后续网关的 `biz_params`，并携带实际 `session_id`。本机核对两个标识后才调用文本模型。可不配置任何开场变量，官方仍允许用该回调传递业务参数。接听时先播放简短欢迎语，应用的开场配置需与本机当前咨询／销售模式相符。

相同来电回调重试复用已接纳的通话，不会创建重复会话或重新拨号。已启用且鉴权通过的忙线来电使用正式 `HangupOperate` 请求挂断；本机不发明阿里没有公开的“拒接 JSON”。总开关或来电开关关闭时，本机拒绝回调且不调用云 SDK。阿里回调超时或失败会使用云应用的变量默认值，因此关闭本机后也必须在云控制台取消号码绑定或关闭来电路由，避免平台兜底话术自行接待。

### 保留的外呼配置

在阿里云先完成号码、应用和大模型网关配置。应用编码对应 `ApplicationCode`；`LlmSmartCall` 接受该应用编码，不能通过单次外呼参数覆盖网关地址。配置步骤见[大模型网关配置](https://help.aliyun.com/zh/aiccs/user-guide/large-model-gateway-configuration/)与[创建智能通话](https://help.aliyun.com/zh/aiccs/developer-reference/api-aiccs-2019-10-15-llmsmartcall)。

本机配置填写 AccessKey ID、AccessKey Secret、应用编码、主叫号码以及公共配置。选填的智能通话超时按阿里接口限制为 600–3600 秒。

平台大模型网关配置填写：

- URL：`<公共访问地址>/api/telephony/aliyun/llm`。
- 授权码：与本机的模型网关鉴权码完全一致。阿里网关将该值原样放在 `Authorization`，本机按原样验证，不自动添加 `Bearer`。
- 模型名：与本机配置一致。本机实际请求仍使用通话快照里的模型配置。
- 开启接通触发模型调用，让模型先说话；固定开场白还需在应用的开场配置中使用相同文本。单次 API 的开场变量依赖应用模板，不猜测平台私有变量名。

网关按官方 `stream`、`session_id`、`out_id`、`biz_params` 和 `messages` 接收请求，外呼时 `OutId` 与 `BizParam.call_id` 使用本机通话 ID。标识与当前通话不匹配时拒绝模型调用。格式参见[网关对接协议](https://help.aliyun.com/zh/aiccs/user-guide/large-model-gateway-docking-parameter-protocol)。

模型被打断后，平台历史中的 `<user-interrupt/>` 被转为内部未完整播放背景，下一轮依据最新用户发言回答。内部背景标签在发给平台的模型输出中再次过滤，不能被朗读。该保守转换把带标记的整条助手消息看作未完整播放背景，不宣称逐字播放对齐。

AI 正常结束时在带祝福的告别后输出官方 MSML `<hangup/>`。平台播放前面的文本后挂断，用户继续说话时可取消待执行标记；应用不根据估算音频时长延迟调用挂断。用户手动挂断则调用 `HangupOperate(ImmediateHangup=true)`，不等告别。正式标记及行为见 [MSML](https://help.aliyun.com/zh/aiccs/user-guide/msml)与[电话挂断接口](https://help.aliyun.com/zh/aiccs/developer-reference/api-aiccs-2019-10-15-hangupoperate)。

在阿里应用回执配置中选择 HTTP 批量 `LlmSmartCall`，地址为 `<公共访问地址>/api/telephony/aliyun/events?token=<回执鉴权码>`。本机返回 `{"code":0,"msg":"成功"}`；按 `call_id`/`out_id` 匹配已有记录并保存 `conversation_record`，不保存录音链接。未接通时 `end_time` 可以为空，忙线、无人接听、拒接等最终回执同样释放本通槽位。接口及状态字段见[最终回执](https://help.aliyun.com/zh/aiccs/developer-reference/llmsmartcall-1)与[智能状态码](https://help.aliyun.com/zh/aiccs/developer-reference/smart-status-code)。

该回执鉴权是配置完整 URL 所实现的能力认证，不是阿里供应商签名。公开的实时对话加密回调、MNS 验签与这个 HTTP 最终报告是不同协议，本接入不会假装用其中一种验签另一种。部署时保密完整回执 URL，并避免将 token 查询参数写入代理访问日志。

## 腾讯云 TCCC

### 来电配置（优先）

先创建可接待来电的智能体并配置 OpenAI 兼容自定义模型，模型 API URL 为 `<公共访问地址>/api/telephony/tencent/llm/`，API Key 使用本机模型网关鉴权码。号码绑定及动态路由见[绑定智能体呼入](https://cloud.tencent.com/document/product/679/119343)；自携模型设置见[接入 DeepSeek](https://cloud.tencent.com/document/product/679/116187)，实际模型仍以本机快照为准。

本机填写 SdkAppId、来电智能体 ID、来电号码、SecretId／SecretKey 和公共配置，并开启总开关和来电接入。号码编辑页的“呼入 API 调用”填写 `<公共访问地址>/api/telephony/tencent/inbound?token=<回执鉴权码>`。平台振铃时 POST：

```json
{"Event":"callInBound","SessionId":"平台通话ID","SdkAppId":1400000000,"CallInBound":{"AIAgentId":0,"IvrId":0,"Caller":"0086主叫号码","Callee":"0086被叫号码"}}
```

接纳响应为：

```json
{"CallInBound":{"OverrideAIAgentId":388,"Variables":[{"Key":"llmautotel_call_id","Value":"本机通话ID"}]}}
```

`Variables` 的正式用途是注入智能体提示词，不能假定它可以修改未公开的模型请求参数。请在云智能体的自定义模型系统提示词里加入一行 `[llmautotel_call_id:${llmautotel_call_id}]`，平台会将变量替换为本机 ID。本机网关只从平台 `system` 消息读取标记，不从用户文字推断会话；平台 system 随后被本机提示词替换，标记不会进入实际模型输入或语音正文。缺少标记、出现冲突或标记不匹配时拒绝调用。平台若另提供 `SessionId` / `session_id`，本机也必须核对；标准 OpenAI 请求没有正式电话会话字段时，关联依靠已接纳 ID、网关鉴权码和单通槽位。该路径需以真实账户验证提示词变量透传，不声称 `LLMExtraBody` 在呼入时可配置或自动含会话 ID。

接听欢迎语由云智能体配置为简短问候，例如“您好，欢迎咨询，请问有什么可以帮您？”。忙线时返回官方 `{"CallInBound":{}}`，平台会在接听前挂断。相同远端 SessionId 回调重试复用原会话 ID。路由的正式五秒超时和两次重试失败会降级到号码静态绑定的智能体／IVR；停用本机时也要停用云号码路由或调整静态兜底，避免云端独立继续接待。来电路由的 token 是完整 URL 能力认证，不是未经文档证实的腾讯 HMAC 或 CDR BasicAuth 协议。

### 保留的外呼配置

需要 TCCC 应用、可用于此接口的自有号码和语音智能体通话套餐；该条件由腾讯云账户开通。[CreateAICall 官方说明](https://cloud.tencent.com/document/product/679/111211)列出号码与套餐要求。

本机填写 SecretId、SecretKey、SdkAppId、主叫号码及公共配置。`CreateAICall` 使用 OpenAI 兼容协议，`APIUrl` 自动指向 `<公共访问地址>/api/telephony/tencent/llm/`，平台发送请求到其 `/chat/completions`；`APIKey` 只填本机网关鉴权码。`LLMExtraBody.call_id` 绑定本机通话 ID。

固定开场白使用 `WelcomeType=0` 与 `WelcomeMessage`；未配置固定开场白时使用 `WelcomeType=1` 生成开场。开场和回复允许被打断，`InterruptMode=0`；VAD 静默时间可配置为 240–2000 ms。应用不主动关闭腾讯默认的 AI 合规提示音。

AI 结束使用 `EndFunctionEnable` 启用的原生 `call_end`。网关只透传平台实际提供的工具定义与模型返回的调用参数，不编造固定 schema，不开放转人工等额外工具。销售模式要求首次明确拒绝目标行动时温和挽留一次，直接要求结束或挽留后再次明确拒绝时带祝福告别；含糊、嫌贵、犹豫或继续提问时继续销售对话。咨询模式围绕当前产品问题回答，不推销或销售挽留；仅当用户明确结束咨询时告别并使用平台结束能力。云托管模式目前由提示词和平台原生工具执行这些规则，没有本机 Pipecat 的逐句播放屏障及 `retain_once` 工具状态校验。

用户手动挂断调用 `HangUpCall`。`ControlAIConversation.ServerPushText.StopAfterPlay` 文档仅说明关闭对话任务，不能据此声称 PSTN 电话已经挂断；实现没有用它或估算延迟替代播放确认。原生 `call_end` 的实际告别播放和插话取消行为仍需账户与真实线路验收，尚未证明与浏览器/Pipecat 模式完全一致。相关接口见[原生话务功能](https://cloud.tencent.com/document/product/679/116191)、[立即挂断](https://cloud.tencent.com/document/product/679/84273)与[控制 AI 对话](https://cloud.tencent.com/document/api/679/120723)。

在腾讯控制台启用电话 CDR 数据推送，可选下列认证方式：

1. 推荐使用平台公开支持的 BasicAuth。地址为 `<公共访问地址>/api/telephony/tencent/events`，用户名填 SdkAppId 的数字字符串，密码填本机回执鉴权码。
2. 配置完整 URL `<公共访问地址>/api/telephony/tencent/events?token=<回执鉴权码>` 作为能力认证地址；仍需保密地址并剥离代理日志里的 token。

平台追加 `action=cdr&version=1`，本机核对 `SdkAppId` 与 `SessionId`，响应 `{"ErrCode":0,"ErrMsg":""}`。BasicAuth、重试和推送标识由[电话数据推送前置说明](https://cloud.tencent.com/document/product/679/67256)规定，数据结构见[电话 CDR 推送](https://cloud.tencent.com/document/product/679/67257)。这是电话协议；不套用在线 IM 的 `Desk.SessionEventNotify`、TRTC 回调或其他产品的签名算法。

CDR 本身不带对话文字。本机在最终 CDR 到达后，通过 `DescribeAICallInteractionRecords` 查询平台的 `UserReply.ASRTranscript` 与 `AISpeak.SpokenText`，不下载录音或录音转写 URL。文字暂未就绪时，本通仍按平台最终结果释放槽位，但回执返回安全的 503 让平台重试，随后补充已有历史并保持真实结束原因。`CanBeInterrupted` 仅代表允许打断，不能推断已经发生打断，所以腾讯模式当前不保证历史里的逐句打断标记。字段依据[官方会话交互流](https://cloud.tencent.com/document/product/679/131442)。

## 记录、失败与验收范围

模型网关输出是生成稿，只交给平台；不直接存作用户已经听到的助手消息。云模式的文字记录以平台最终报告和交互流为准，因此通话期间历史文字可能暂未出现，页面状态表示网关正在处理文本，不代表远端音频的精确播放进度。

模型失败时显示失败阶段并请求结束电话。已接纳的来电发生本地状态或记录回调错误时，同样按绑定的远端 ID 挂断并释放控制连接。手动挂断或服务关闭发生在创建请求期间时，仍等待请求返回远端会话 ID，再请求挂断该电话，防止迟到的成功创建遗留外呼。不自动重拨。

新一轮网关请求通过校验时就使上一轮失效，无需等待新响应开始读取。旧模型结果在状态回调暂停后迟到，或旧响应直到新一轮启动后才开始读取，都不能继续发送旧文字、结束标记或取消当前请求。

手动挂断后迟到的最终报告仍可更新已有电话记录；保持本机已记录的结束时间与用户挂断原因。重复报告替换平台文字记录，未知会话不会创建历史。通话 ID 的远端关联不包含模型密钥。禁用 provider 后仍可验证已保存的回执鉴权码并接收已有电话的迟到报告，这不会启用外呼或产生云查询；腾讯禁用后不再主动查询交互文字。迟到回执使用当前 provider 的鉴权配置，在回执送达前轮换鉴权码会拒绝旧回执。

自动化测试使用受控 HTTP、真正官方 SDK 签名及真实 OpenAI 兼容 SDK，覆盖云控制参数、鉴权、模型快照、思考与 token 参数、跨 chunk 文本净化、旧流取消迟到、状态回调暂停及旧响应延迟读取、创建中结束、最终报告去重、未接通终态、超时和资源清理。来电新增正式请求／应答字段、阿里签名与时间窗口、号码／应用匹配、腾讯系统变量绑定、防用户伪造标记、禁用时无控制请求、既有远端会话接纳、运行前立即挂断及最终回执结束测试。所有号码和凭据都是测试占位符，没有真实拨号。

真实云账户开通、应用模板／网关配置、呼入欢迎语、腾讯呼入提示词变量透传、线路接听、托管 ASR/TTS 效果、腾讯原生告别播放后挂断、回声及打断延迟均未做真实验收。500 ms 停音目标属于真实设备验收项，受平台 VAD、线路和网络影响，模拟测试不代表已达到该指标。
