# 电话媒体与现有语音管线

电话接入通过 `PhoneTransport` 注入原有 `VoiceSession`。ASR、文本 LLM、TTS、Silero VAD、打断草稿背景、按模式选择的咨询／销售工具及句级记录共用原实现；provider 负责接管已有来电或显式外呼，以及原始音频、播放清理与挂断。来电工厂传入 `IncomingCall`，仅接听已有通道，接口见 [来电接入](telephony-inbound.md)。

```python
from llmautotel.telephony.freeswitch import FreeSwitchDriver
from llmautotel.telephony.transport import PhoneTransport
from llmautotel.voice import VoiceSession

transport = PhoneTransport(
    lambda media_callbacks: FreeSwitchDriver(settings.telephony.freeswitch, media_callbacks),
    number=number,
    call_id=call_id,
)
session = VoiceSession(None, settings, voice_callbacks, transport=transport)
```

示例中的配置、号码、UUID、持久化回调由会话管理器提供。创建对象和保存配置不联网；只有显式启动会话、收到 Pipecat `StartFrame`，且输入与输出 transport 都初始化后才调用 provider `start()`。

## 电话音频与开场

provider 输入是单声道 PCM16LE。`PhoneInputTransport` 使用 Pipecat 的官方流式重采样器转换为原管线的 16 kHz，选用 `QQ` 低延迟模式，避免默认高品质滤波在输入端额外积累上百毫秒。输出沿用官方 `BaseOutputTransport` 的重采样、分块、排队与尾部补齐，转换为 provider 所需采样率，以 20 ms 节奏发送。

电话没有浏览器的 RTVI `client-ready` 事件。provider 必须在被叫接听且双向媒体可用后调用 `on_ready()`；transport 的 `on_client_connected` 才触发一次开场。未接听、忙线或媒体尚未就绪时不播放开场。状态和文字仍通过原会话回调落库并供网页查看。

## 播放确认和打断

`TTSTextFrame`、`LLMFullResponseEndFrame`、`LLMAssistantPushAggregationFrame`、`HangupAfterPlaybackFrame` 四类检查点在官方音频队列中排在前面的音频之后；公共 transport 在放行检查点前等待 provider `wait_played()`。因此只完成合成或发送，不能提前将整句标记为已播放，也不能提前执行告别后的挂断。

HTTP TTS 的句子文字可能早于 `TTSStoppedFrame` 到达。为了不把不足 20 ms 的最后一块或滤波器尾部留到下一句，本实现复用 **Pipecat 1.12.0** 的 `MediaSender._enqueue_flushed_audio_buffer()`，先把尾部补齐、排入官方队列，再排入检查点。这是对已锁版本的薄适配，升级 Pipecat 时须运行媒体与上下文回归。

收到 `InterruptionFrame` 后增加回合代次，复用官方打断处理清空旧音频与重采样尾部，并调用 provider `flush()`。即使音频不足一块、尚未触发 `BotStartedSpeaking`，也丢弃缓存。新的音频在 flush 完成前等待；检查点入队时标注代次，旧代次的迟到写入和播放确认不能提交文字或触发挂断。官方队列包含必须完成的工具控制帧时会保留媒体任务：取消旧播放确认只丢弃旧检查点，保留该任务继续处理工具控制帧与新回复；真实任务取消仍按官方取消流程退出。取消 ASR/LLM/TTS 与下一轮上下文处理由原管线负责。

provider 的确认能力依协议不同：Asterisk 以媒体标记确认电话服务器播放队列；FreeSWITCH 原生 unicast 使用末包发送时长加配置的媒体尾部等待，没有手机侧播放 ACK。两者均不能承诺逐字与用户实际听到的内容对齐。真实线路、耳机／外放、停音延迟及告别完整度仍需设备验收。

## 清理与验证

用户挂断、连接中取消、provider 断连、模型故障和媒体故障都会取消管线与旧播放等待，并对 driver 执行幂等 flush、hangup、close。终止原因以第一项确定的原因保留，忙线、无人接听与媒体故障不会误归为模型请求失败。

挂断或清理未获确认时仍尝试 driver `close()` 释放本机资源，并显示安全的清理失败说明。内部取消流程保留首先确定的挂断意图；清理未确认时，最终记录采用安全的失败说明，不能据此宣称远端电话已经结束。上游原始异常或凭据不会显示到页面。

`uv run pytest tests/test_telephony_transport.py` 使用真实 Pipecat 管线、官方重采样和可控 PCM driver，验证实际输入／输出音频、20 ms 节奏、不足一块的尾部、播放检查点、迟到确认、连接中挂断、接听开场及真实 SDK 挂断工具的播放顺序。它不拨打真实号码，不调用外部模型。

参考：[Pipecat 自定义 transport 文档与源码](https://github.com/pipecat-ai/pipecat/tree/v1.12.0/src/pipecat/transports)、[官方 BaseOutputTransport](https://reference-server.pipecat.ai/en/latest/api/pipecat.transports.base_output.html)。本项目以已安装并锁定的 1.12.0 源码为接口依据。
