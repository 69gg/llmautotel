# 实时语音管线

每次通话建立独立 Pipecat 管线：浏览器 SmallWebRTC 输入 → ASR → 用户上下文 → 文本 LLM → TTS → SmallWebRTC 输出 → 助手上下文。三种服务可分别配置地址、模型和密钥。输入使用 16 kHz 单声道音频；TTS 返回的裸 PCM 须为 16 位、小端、单声道，采样率与配置一致，默认 24 kHz。

网页与服务端完成 RTVI ready 握手后，AI 自动生成简短开场；填写固定开场时直接朗读该文本。ready 重复事件不会重复开场。用户若已先开口，服务端直接处理用户发言，不随后补播开场。

Silero VAD 在本机 CPU 检测开口，默认连续语音 0.1 秒确认开始、静音 0.6 秒断句、置信度 0.7。检测开始立即由 Pipecat 打断 LLM、TTS 和输出队列，不等待识别文本；检测停止后，ASR 将该段音频包装为 WAV，调用兼容接口。停顿阈值之后还需要等待 ASR 和模型响应。背景声、外放回声和麦克风噪声会影响检测，建议首次调试使用耳机。

分段 ASR 按录入顺序返回，每个停口片段都有一个最终转写，包括空结果。用户在识别期间继续补充发言时，管线把这些片段合为同一用户回合，等全部最终结果返回且用户停止说话后才请求 LLM。ASR 的 P99 安全计时器到期不会提前结束仍在识别的回合；回合 watchdog 使用已配置 ASR 超时加 VAD 停顿时长，避免框架默认 5 秒抢先收尾。如果 watchdog 仍等不到全部结果，服务端丢弃这一未完整识别回合，发送中文识别超时提示并取消会话，不据此调用 LLM / TTS。

此处通过 `FinalTranscriptUserTurnStopStrategy` 薄子类复用官方 `SpeechTimeoutUserTurnStopStrategy` 的计时和 VAD 行为，仅补充分段最终结果屏障和空最终结果出口。它使用 Pipecat **1.12.0** 的 `_maybe_trigger_user_turn_stopped` 及对应私有状态；watchdog 使用用户聚合器 `_user_turn_controller` 的同步超时事件，保证在框架兜底提交前清理未完成回合，公开聚合器同名事件会异步排队，不能提供此顺序保证。依赖已锁定该版本；升级 Pipecat 时需核对这些钩子并运行 `tests/test_voice.py` 和 `tests/test_segmented_turns.py`，其中包括慢 HTTP 转写、续说、空结果及超时后不再调用模型的真实管线回归。

界面状态经 RTVI server-message 发送，包含 `listening`、`recognizing`、`thinking`、`speaking`；用户停止说话后进入识别状态，最终有效转写后才等待模型回复。空转写回到聆听，不触发新回复。标准 RTVI 事件另提供正在说话和转写信息。持久化后的转写提交事件是 `{type: "transcript", entry: {role, text, timestamp, interrupted}}`。LLM 流式生成文字仅用于临时显示，不当作用户已经听到的内容。

浏览器收到远端音轨后，将该音轨挂到通话音频元素的 `srcObject` 并调用 `play()`。锁定的 SmallWebRTC JavaScript 传输层 **1.10.8** 在远端音轨解除静音时调用 `onTrackStarted(track)`，没有传入可选的 participant；因此播放判断允许 participant 缺省，同时排除 `local: true` 和当前客户端的本地音轨。本机麦克风适配器的音轨回调明确带有 `local: true`，本地收音不会接入扬声器播放。此前要求 participant 必须存在会漏掉真实远端回调，使开场音频无法挂载、播放或进入音量分析。

浏览器拒绝自动播放时，通话页显示“点击播放声音”；用户点击后重新调用 `play()` 并恢复音量分析。音量反馈来自浏览器音轨的 RMS 分析，代表音轨含有信号，不保证扬声器实际出声。挂断时先暂停播放、清空音频元素 `srcObject`、停止音轨并关闭音量分析，再释放连接；旧会话的迟到音轨回调会被丢弃并停止对应音轨，不能恢复播放。前端回归覆盖没有 participant 的远端事件、本地音轨排除、自动播放恢复和挂断清理；真实麦克风、扬声器及浏览器声音权限仍需设备验收。

普通 OpenAI 兼容 PCM TTS 无词时间戳，因此上下文和文字历史采用句级提交：完整播放的句子进入下一轮上下文，正在播放的半句在打断时舍弃。打断标记仍然保存，可能出现正文为空的中断记录。不能由音频长度准确推算用户已听到哪些字。固定开场也通过同样机制提交。

`tests/test_voice.py` 使用真实 Pipecat 管线、官方音频输出队列和实际 TTS 适配器验证分阶段打断：LLM 仅生成未完整句子、固定开场 TTS 尚未返回音频、固定开场仅播放半句，以及完整第一句后第二句播放期间。测试检查旧生成任务被取消、输出音频计数停止增长、空转写不会续播开场、下一次 LLM 上下文只包含已完整播放内容。这些测试替换模型网络响应和最后设备写入；真实麦克风、扬声器及实际模型服务仍需浏览器联调验收。

模型请求失败时发送按 ASR / LLM / TTS 分类的中文提示，取消本通会话，保留已提交文字；提示不包含上游响应、密钥或原始异常。挂断和浏览器断开同样取消管线，释放连接和三个模型客户端。取消之前清除尚未完成的用户转写聚合，避免 Pipecat 1.12.0 的默认取消收尾把它交给 LLM；已提交用户文字和助手完整播放句子仍保留，挂断时助手的中断标记照常收尾。应用保存文字历史，不保存录音。

相关依据：[Pipecat 打断机制](https://docs.pipecat.ai/pipecat/fundamentals/interruptions)、[会话初始化](https://docs.pipecat.ai/pipecat/learn/session-initialization)、[1.12.0 回合结束策略](https://github.com/pipecat-ai/pipecat/blob/v1.12.0/src/pipecat/turns/user_stop/speech_timeout_user_turn_stop_strategy.py)、[1.12.0 TTS 句级文本处理](https://github.com/pipecat-ai/pipecat/blob/v1.12.0/src/pipecat/services/tts_service.py)。
