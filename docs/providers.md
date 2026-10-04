# 模型接入与协议边界

ASR、文本 LLM 和 TTS 使用各自独立的地址、模型、密钥和请求超时。
ASR / TTS 可以分别选择 `openai` 兼容协议或 `mimo` 协议；地址与模型仍由配置提供，不按地址或模型名猜测协议。旧配置默认继续使用 `openai`。
API 地址填写服务商兼容接口的根地址，例如服务部署在 `/v1` 下时保留该路径；
不要填写完整的 `/audio/speech` 或 `/chat/completions` 路径。
应用不预填服务商地址、模型或密钥。无鉴权的本机服务可留空密钥，
不会读取 `OPENAI_API_KEY` 环境变量来替代配置。

| 阶段 | HTTP 请求 | 首版协议 |
| --- | --- | --- |
| ASR | `POST {base_url}/audio/transcriptions` | multipart `file=audio.wav`、`model`、`language`；返回含 `text` 的 JSON |
| 文本 LLM | `POST {base_url}/chat/completions` | `model`、`messages`、`stream=true`；返回标准 SSE chat completion chunks |
| TTS | `POST {base_url}/audio/speech` | `model`、`voice`、`input`、`response_format=pcm`；返回流式原始音频 |

以上为 OpenAI 兼容音频协议。MiMo 的语音协议如下：

| 阶段 | HTTP 请求 | 格式 |
| --- | --- | --- |
| MiMo ASR | `POST {base_url}/chat/completions` | `user` 消息中的 `input_audio.data` 为 WAV Base64 Data URL，`asr_options.language` 为 `auto/zh/en`；读取 `choices[0].message.content` |
| MiMo TTS | `POST {base_url}/chat/completions` | 合成文字放 `assistant.content`，`audio={format:pcm16,voice:...}`、`stream=true`；解码 SSE `delta.audio.data` 的 Base64 |

MiMo 复用同一 Pipecat WAV 分段、FIFO 最终转写、音频排队、句级提交和打断清理逻辑。ASR 使用非流式单段识别，编码后的音频不能超过 10 MB；TTS 使用流式 PCM16LE 单声道，固定为 **24000 Hz**，配置中其他采样率会被拒绝。空 ASR 结果不会生成用户发言；空 TTS、非法 Base64 或不完整 PCM 采样会结束会话并提示合成失败。SDK 音频流在取消或生成器关闭时释放。

文本 LLM 的 `thinking` 与 `reasoning_effort` 可单独配置，留空时省略。思考模式通过 `extra_body.thinking.type` 传给兼容接口，推理强度通过 `reasoning_effort` 传入。服务商须支持这些可选参数。应用不发送 `max_tokens` 或 `max_completion_tokens`；`reasoning_content` 不朗读，只将最终回答 `content` 送入 TTS。

本机当前所选服务的配置说明见 [MiMo 与 DeepSeek 接入](model-setup.md)，其中不包含密钥。

ASR 复用 Pipecat `OpenAISTTService` 的分段识别，麦克风输入为 16 kHz 单声道，
由框架封装成 WAV。空识别文字会传给会话层，让界面恢复倾听，不生成虚构用户消息。
识别语言使用 Pipecat 支持的语言代码，例如 `zh`、`en`；首版默认 `zh`。

文本 LLM 复用 `OpenAILLMService`，使用其流式输出和上下文适配器。
SDK 自动重试和 Pipecat 的超时重试都关闭，每次模型请求失败由会话层结束当前会话。
请求超时是单次连接或读写等待的上限，不是整通会话的时限。

通过 Pipecat 1.12.0 的公开 `build_chat_completion_params` 钩子，在 SDK 编码前处理末尾连续两条以上的纯文本用户消息：保留之前发言作为背景，将最后发言标为当前请求。背景提示允许用户明确要求继续或同时回答。该处理仅创建请求副本，不改原始 `LLMContext`、文字历史或模型参数；不会跨过助手消息，也不合并多模态内容。系统提示同时要求插话后先回答当前问题，避免仍按顺序补答未完成的旧问题。

TTS 是继承 Pipecat `TTSService` 的薄适配器，沿用框架的分句、打断和文本同步。
`voice` 是服务商的任意音色字符串，不限制为 OpenAI 的预置音色。
音频必须是**单声道、16 位小端 PCM**；`sample_rate` 说明服务实际输出采样率，
默认 24000 Hz。标准 `/audio/speech` 请求不发送非标准 `sample_rate` 字段，
也不要求服务为该字段切换输出。应根据服务文档设置实际采样率，否则播放速度会错误。
HTTP 分块切开采样时会缓存单字节并与下一块拼接；流末尾不完整的采样会报错。
OpenAI 兼容 TTS 不接收 WAV/MP3/Opus 或多声道音频。仅允许 PCM、octet-stream 或未声明的内容类型，
明确声明为 MP3、WAV 或 JSON 的响应会被拒绝。裸 PCM 不含格式元数据，
无法自动推断采样率或声道数，服务商必须与配置一致。

错误仅输出 `asr:`、`llm:`、`tts:` 阶段和必要的 HTTP 状态或超时概括，
不回传原始异常和服务商响应正文，避免错误页面或日志暴露密钥。
取消 TTS 请求会退出 HTTP 流上下文并关闭响应；下一轮使用新的音频上下文。
每句 HTTP 音频返回完毕后，在框架排入对应文字帧之后立即结束该句音频上下文，
让下一句继续播放，不等待框架 3 秒的空闲检测超时。
MiMo TTS 使用锁定 SDK 的 SSE 解析与音频 Base64 解码。会话结束时框架 `cleanup()` 关闭服务客户端，连接准备失败时可调用
`ProviderServices.aclose()` 清理尚未进入管线的客户端。

## 锁定接口版本与验证

实现依据 Pipecat 1.12.0、OpenAI Python SDK 3.24.0 的实际源码，
并使用 Context7 查验服务构造、流式音频和 HTTP 取消方法。
该 SDK 使用 `httpx2` 客户端，TTS 的直接 HTTP 适配器使用 `httpx`；
两套测试运输层分别匹配各自的客户端。

执行 `uv run pytest tests/test_providers.py` 验证真实 SDK 请求编码：
WAV multipart、独立接口路径和鉴权、SSE 文本、任意音色、分块 PCM、
错误信息去敏感化、零重试、无密钥服务及取消后没有旧流音频进入下一轮。
测试也覆盖 LLM 在管线中的 SSE 取消、流中途超时后的响应关闭，以及非 PCM 类型拒绝。
真实 Pipecat 管线测试检查连续两句的音频、文字、停止帧顺序，并要求在 1 秒内完成。
这些是可控 HTTP 协议测试，不代表已经连接真实服务商或完成真实设备语音验收。
`tests/test_mimo.py` 验证 MiMo 的 JSON/WAV/Base64/SSE 请求与取消；`tests/test_llm_options.py` 验证思考参数、实际省略 token 上限，以及思考内容不进入 TTS。`tests/test_interruption_context.py` 检查打断后真实 SDK 序列化的当前问题、背景保留、上下文不变与重复请求，以及三阶段旧流取消和下一轮音频输出。真实服务结果单独记录在 [验收记录](acceptance.md)。

参考：

- [Pipecat 1.12.0 ASR 实现](https://github.com/pipecat-ai/pipecat/blob/v1.12.0/src/pipecat/services/openai/stt.py)
- [Pipecat 1.12.0 文本 LLM 实现](https://github.com/pipecat-ai/pipecat/blob/v1.12.0/src/pipecat/services/openai/base_llm.py)
- [Pipecat 1.12.0 TTSService](https://github.com/pipecat-ai/pipecat/blob/v1.12.0/src/pipecat/services/tts_service.py)
- [HTTPX 异步流文档](https://www.python-httpx.org/async/)
- [MiMo ASR](https://mimo.mi.com/docs/zh-CN/quick-start/usage-guide/audio/Speech-Recognition)
- [MiMo TTS](https://mimo.mi.com/docs/zh-CN/quick-start/usage-guide/audio/speech-synthesis-v2.5)
- [DeepSeek 首次调用](https://api-docs.deepseek.com/zh-cn/)
