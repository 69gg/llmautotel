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

当前测试目标和话术已去掉调查用途、频率和痛点的要求，采用“回答问题、说明价值、询问是否考虑订阅”的表达。程序使用配置驱动的通用提示词和工具说明，实际意向问句依据目标选择；更换为购买或预约演示等目标无需修改程序，也没有 Plus 专用分支。唯一一次挽留沿用直接介绍价值的方式。

2026-10-04 的真实接口验证已通过：MiMo 合成一句中文后，ASR 准确识别；DeepSeek 在启用思考、强度 high 的配置下返回文本流。该验证使用内存中的测试音频，没有保存录音；真实浏览器设备与打断延迟仍待验收。

随后关闭思考的真实 DeepSeek 对照验证中，连续问功能后改问价格，新的请求背景/当前发言标记让模型直接回答价格。此结果为单次接口样本，不能替代真实麦克风打断验收。

`deepseek-flash` 已通过实际适配器验证流式工具：最新规则下首次“不用了，谢谢”调用 `retain_once`，挽留后的新回合同句调用 `hang_up` / `purchase_refusal`；“请挂断，谢谢”直接调用 `hang_up` / `direct_exit`。嫌贵、犹豫及拒绝后继续提问时继续对话，不询问挂断。打断背景之后的工具证据仍为最新用户原文。该验证只观察模型工具决策及内存状态消费，没有通过真实设备执行通话。本机测试资料中原有“拒绝立即道别、通话由用户挂断”的话术同步替换为一次挽留和工具挂断规则。

依据：[MiMo ASR](https://mimo.mi.com/docs/zh-CN/quick-start/usage-guide/audio/Speech-Recognition)、[MiMo TTS](https://mimo.mi.com/docs/zh-CN/quick-start/usage-guide/audio/speech-synthesis-v2.5)、[DeepSeek 官方调用示例](https://api-docs.deepseek.com/zh-cn/)。
