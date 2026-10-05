# 小米 MiMo 与 DeepSeek

本机模型参数按用户指定接入，密钥仅保存在权限受限的 SQLite 配置中，不在文档、历史或 Git 中保存。

| 角色 | 配置 |
| --- | --- |
| ASR | 协议 `mimo`，根地址 `https://api.xiaomimimo.com/v1`，模型 `mimo-v2.5-asr`，语言 `zh` |
| TTS | 协议 `mimo`，相同根地址，模型 `mimo-v2.5-tts`，音色 `mimo_default`，采样率 24000 Hz |
| 文本 LLM | 根地址 `https://api.deepseek.com`，模型 `deepseek-flash`，思考模式 `disabled`，保留推理强度配置 `high` |

ASR 和 TTS 各自保存 MiMo 密钥，LLM 保存 DeepSeek 密钥。所有代码默认值仍为空地址、空模型及无密钥，其他用户不会自动获得上述配置。

MiMo 的默认音色由服务集群决定，中国集群默认“冰糖”；可以在音色输入中指定官方支持的其他音色。首版使用预置音色模型，不接入音色克隆或设计。

DeepSeek 按用户最新要求关闭思考，发送 `thinking.type=disabled`；保留原推理强度配置 `high`，不设置 `max_tokens` 或 `max_completion_tokens`。应用仅将最终回答合成为语音。若之后重新开启思考，首个回答音频的等待时间可能增加。

现有销售配置保留用户此前填写的 ChatGPT Plus 测试资料与固定销售开场，代码不针对该产品适配。新默认模式为产品咨询，旧版配置首次升级只复制产品资料到独立咨询组，不沿用销售要求或开场；可在对话配置切回销售。两种模式共用模型，参数只影响下一通。插话后回答最新问题，生成背景仅供内部理解；咨询不推销或挽留，销售仍保留首次拒绝挽留一次、再次拒绝告别和直接结束不挽留。真实 API、设备和新来电线路的验收范围分别记录在 [验收记录](acceptance.md)。

## ChatGPT Plus 来电咨询示例

2026-10-05 已在本机保存 `conversation.mode=consultation`，以 ChatGPT Plus 填写独立的咨询产品资料和回复要求。欢迎语为：“您好，我是 AI 咨询助手，欢迎咨询 ChatGPT Plus。我可以为您介绍功能和订阅信息。”

资料包括问答、写作与学习资料整理、文件分析、语音、图片生成及 Codex；说明免费方案也具备部分基础能力，Plus 提供更高额度和更多能力，具体功能及限额以账户显示为准。官方月费为 20 美元，实际币种、税费及金额以结算页面为准，不预设人民币价格、具体模型版本或固定次数。常规 OpenAI API 费用单独计费，不把 Plus 订阅说成包含本应用三组模型的 API 用量。事实依据为 [Plus 官方帮助](https://help.openai.com/en/articles/6950777-what-is-chatgpt-plus)、[ChatGPT 套餐](https://chatgpt.com/pricing/)及 [OpenAI Docs 定价](https://learn.chatgpt.com/docs/pricing)，核对日期为 2026-10-05。

回复要求使用通用咨询规则：简洁中文、直接回答最新问题、不调查用途、不主动推销或挽留、不编造资料以外的信息、不冒充官方客服或承诺代办；用户明确结束时使用带祝福的告别与挂断工具。该示例只是本机配置内容，程序默认值和通用逻辑没有加入 Plus 特例。

保存后以新的 Store 实例回读确认持久化，同时比较原销售组、ASR／LLM／TTS（含密钥）、语音及电话配置保持原值。本机数据文件仍为 `0600`。本次没有调用真实模型、启动服务或接听电话；云来电欢迎语还需在云智能体的开场配置中同步，见 [云平台接入](telephony-cloud.md)。

## 保留的销售示例与历史验证

保留的销售测试目标和话术已去掉调查用途、频率和痛点的要求，采用“回答问题、说明价值、询问是否考虑订阅”的表达。程序使用配置驱动的通用提示词和工具说明，实际意向问句依据目标选择；更换为购买或预约演示等目标无需修改程序，也没有 Plus 专用分支。唯一一次挽留沿用直接介绍价值的方式。

2026-10-04 的真实接口验证已通过：MiMo 合成一句中文后，ASR 准确识别；DeepSeek 在启用思考、强度 high 的配置下返回文本流。该验证使用内存中的测试音频，没有保存录音；真实浏览器设备与打断延迟仍待验收。

随后关闭思考的真实 DeepSeek 对照验证中，连续问功能后改问价格，新的请求背景/当前发言标记让模型直接回答价格。此结果为单次接口样本，不能替代真实麦克风打断验收。

`deepseek-flash` 已通过实际适配器验证流式工具：最新规则下首次“不用了，谢谢”调用 `retain_once`，挽留后的新回合同句调用 `hang_up` / `purchase_refusal`；“请挂断，谢谢”直接调用 `hang_up` / `direct_exit`。嫌贵、犹豫及拒绝后继续提问时继续对话，不询问挂断。打断背景之后的工具证据仍为最新用户原文。该验证只观察模型工具决策及内存状态消费，没有通过真实设备执行通话。本机测试资料中原有“拒绝立即道别、通话由用户挂断”的话术同步替换为一次挽留和工具挂断规则。

依据：[MiMo ASR](https://mimo.mi.com/docs/zh-CN/quick-start/usage-guide/audio/Speech-Recognition)、[MiMo TTS](https://mimo.mi.com/docs/zh-CN/quick-start/usage-guide/audio/speech-synthesis-v2.5)、[DeepSeek 官方调用示例](https://api-docs.deepseek.com/zh-cn/)。
