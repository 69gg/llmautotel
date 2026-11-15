# OpenI 三模型延迟原型（实验）

独立于正式语音应用。验证一张 NVIDIA GPU 同机运行流式 Paraformer、Qwen3-4B-Instruct-2507、CosyVoice3-0.5B 的延迟，使用 OpenI 官方在线推理代理传输 WebSocket，不使用隧道或端口穿透，不修改正式配置、历史和线路。模型、源代码版本、测试问题、提示词和限制见 `settings.json`；源代码与 Python 依赖均锁定版本。

这是选型原型，尚未接正式 Pipecat 管线、真实 VAD、电话、浏览器音频与连续对话。问题为内存中生成的合成语音，产品资料为虚构测试资料。这里的通过不等于正式通话验收。

## 运行

在 OpenI 独立私有仓库中部署这些文件，选择 CUDA 12.4 / Python 3.10 / PyTorch 2.6 镜像、单卡 A100 40GB，启动入口 `openi_start.py`。先从 `settings.json` 生成 `settings.run.json`，其中 `token_sha256` 仅保存测试访问令牌的 SHA-256；原始令牌只存本机权限 0600 的文件，不能写 Git、URL、日志或聊天。

启动只安装轻量网关依赖。先验证官方代理允许双向 WebSocket，再发送认证 `load` 指令才安装隔离的模型环境、下载模型并加载。仅一个客户端可使用服务，模型回合最多 20 次，输入最多 20 秒；两小时后不接受新连接。这不等于停止 GPU 计费，完成或失败后必须在平台停止任务并确认状态。

```sh
uv run python experiments/openi_latency/client.py --url "$OPENI_URL" \
  --token-file data/openi-token --mode probe
uv run python experiments/openi_latency/client.py --url "$OPENI_URL" \
  --token-file data/openi-token --mode load
uv run python experiments/openi_latency/client.py --url "$OPENI_URL" \
  --token-file data/openi-token --mode status
uv run python experiments/openi_latency/client.py --url "$OPENI_URL" \
  --token-file data/openi-token --mode local --repeats 2 --output data/openi-local.json
uv run python experiments/openi_latency/client.py --url "$OPENI_URL" \
  --token-file data/openi-token --mode remote --repeats 2 --output data/openi-remote.json
```

可把客户端和 `protocol.py` 复制到媒体服务器执行 remote，得到服务器与 OpenI 的真实往返及有效首音。代理地址是平台给出的在线推理地址，不能用容器内 IP。

## 计时与局限

输入按 16kHz、单声道 int16 PCM、20ms 块实时发送。ASR 在用户讲话期间处理流，模拟停口等待默认 200ms，再最终识别、生成回复。LLM 的文本迭代器直接输入 CosyVoice 双流路径，音频边产生边返回。

口径为最后一个有效输入块的计划播放结束时刻，到内存终端播放队列中首段有效输出声音。输入尾部静音剔除，输出前导静音和包间等待计入；20ms RMS 窗口阈值为100，报告首包与有效音频的区别。local 测 OpenI 同机内存终端，remote 包含官方代理网络及播放队列。报告保留识别和回复，需核对语义是否有效；少量样本的 p50/p95 仅描述本次数据，不代表稳定承诺。

合成问题会预热 TTS，所以首轮回复不能称为完整冷启动。依赖、下载、模型加载的启动耗时与已加载后的响应分开记录。不保存输入或输出录音。生产回声消除、真实停口检测、耳机/外放、并发及打断未由本实验验收。

取消使用生成代次过滤迟到音频，但这是实验级机制，不替换正式应用的打断保障。HTTP 状态接口和 WebSocket 控制指令需要认证；健康接口只返回阶段，不返回日志或模型资料。

## 本机验证

```sh
uv run python -m pytest tests/test_openi_prototype.py -q
uv run ruff check experiments/openi_latency tests/test_openi_prototype.py
```

官方源码：[FunASR](https://github.com/modelscope/FunASR)、[CosyVoice](https://github.com/FunAudioLLM/CosyVoice)、[Qwen3](https://github.com/QwenLM/Qwen3)。本机没有 NVIDIA GPU，不把单元测试结果写成模型延迟。
