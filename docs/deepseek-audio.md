# DeepSeek 语音接入

协议核对日期：2026-11-14。ASR 和 TTS 各自选择 `protocol=deepseek`，填写独立的根地址和密钥。官方根地址为 `https://api.deepseek.com/v1`，需保留 `/v1`；不填写完整工具路径，不自动复用文本模型密钥。两种语音工具均不需要模型名称，旧模型字段仍保存但不发送。

## 分段识别

`POST {base_url}/tools/asr` 使用 multipart，仅包含 `file` 字段。复用 Pipecat 分段 WAV：16 kHz、16 位单声道 PCM，读取响应 `text`。本机在上传前拒绝超过 8 MiB 或 120 秒的片段。语种由服务自动识别，不发送 `model/language`。

官方明确不支持流式识别，因此仍是用户停口后上传一段音频；空结果恢复倾听，不生成用户消息。不能因更换此 provider 就宣称已经接入实时 ASR 或达到总响应 1 秒。

## HTTP 合成

`POST {base_url}/tools/tts` 发送 `text`、`format=pcm`，填写音色时发送 `voice_id`。音色为 `stella` 或 `tide`，留空由服务选择。音频响应边合成边返回，为 24 kHz、16 位小端、单声道 PCM；其他采样率会被拒绝。

不发送模型名、语言、语速或 `instructions`，旧语气配置保留供切回其他协议使用。音频字节跨请求块对齐；空输出、截断、错误响应或末尾半个采样均按合成失败处理，不把未完成文本记为已播放。取消时释放请求，不尝试续传。

## 协议验证

自动化使用可控 HTTP 服务验证完整地址、鉴权、仅文件上传、自动语种、无模型字段、两个音色和默认音色、PCM 对齐与采样率、失败去敏和独立配置持久化。真实账户和设备效果单独验收，不以协议测试代替延迟验收。

官方资料：[TTS](https://api-docs.deepseek.com/zh-cn/guides/tts)、[ASR](https://api-docs.deepseek.com/zh-cn/guides/asr)。
