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

本机已按用户要求填入 ChatGPT Plus 测试资料及直接介绍产品的固定开场；其他使用者需填写自己的销售目标和产品资料。修改参数只影响下一通，不改变已开始会话。插话后优先回答当前问题，不自动续讲旧话题；被打断的 AI 生成稿以未完整播放标签保留为内部系统背景，管理标记在 TTS 前过滤，不作为对话朗读。首次明确不需要购买时温和挽留一次，再次拒绝后告别挂断；直接要求结束或别再打扰则跳过挽留。含糊时继续介绍，不问是否要挂断。真实 API 连通性与耳机/外放体验分别记录在 [验收记录](acceptance.md)。

2026-10-04 的真实接口验证已通过：MiMo 合成一句中文后，ASR 准确识别；DeepSeek 在启用思考、强度 high 的配置下返回文本流。该验证使用内存中的测试音频，没有保存录音；真实浏览器设备与打断延迟仍待验收。

随后关闭思考的真实 DeepSeek 对照验证中，连续问功能后改问价格，新的请求背景/当前发言标记让模型直接回答价格。此结果为单次接口样本，不能替代真实麦克风打断验收。

`deepseek-flash` 已通过实际适配器验证流式工具：最新规则下首次“不用了，谢谢”调用 `retain_once`，挽留后的新回合同句调用 `hang_up` / `purchase_refusal`；“请挂断，谢谢”直接调用 `hang_up` / `direct_exit`。嫌贵、犹豫及拒绝后继续提问时继续对话，不询问挂断。打断背景之后的工具证据仍为最新用户原文。该验证只观察模型工具决策及内存状态消费，没有通过真实设备执行通话。本机测试资料中原有“拒绝立即道别、通话由用户挂断”的话术同步替换为一次挽留和工具挂断规则。

依据：[MiMo ASR](https://mimo.mi.com/docs/zh-CN/quick-start/usage-guide/audio/Speech-Recognition)、[MiMo TTS](https://mimo.mi.com/docs/zh-CN/quick-start/usage-guide/audio/speech-synthesis-v2.5)、[DeepSeek 官方调用示例](https://api-docs.deepseek.com/zh-cn/)。
