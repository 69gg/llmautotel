# OpenI 三模型延迟原型（实验）

独立于正式语音应用。验证一张 NVIDIA GPU 同机运行流式 Paraformer、Qwen3-4B-Instruct-2507、CosyVoice3-0.5B 的延迟，使用 OpenI 官方在线推理代理传输 WebSocket，不使用隧道或端口穿透，不修改正式配置、历史和线路。模型、源代码版本、测试问题、提示词和限制见 `settings.json`；源代码与 Python 依赖均锁定版本。

这是选型原型，尚未接正式 Pipecat 管线、真实 VAD、电话、浏览器音频与连续对话。问题为内存中生成的合成语音，产品资料为虚构测试资料。这里的通过不等于正式通话验收。

## 运行

在 OpenI 独立私有仓库中部署这些文件，选择 CUDA 12.4 / Python 3.10 / PyTorch 2.6 镜像、单卡 A100 40GB，启动入口 `openi_start.py`。先从 `settings.json` 生成 `settings.run.json`，其中 `token_sha256` 仅保存测试访问令牌的 SHA-256；原始令牌只存本机权限 0600 的文件，不能写 Git、URL、日志或聊天。

启动只安装轻量网关依赖。先验证官方代理允许双向 WebSocket，再发送认证 `load` 指令才安装隔离的模型环境、下载模型并加载。仅一个客户端可使用服务，模型回合最多 20 次，输入最多 20 秒；两小时后不接受新连接。这不等于停止 GPU 计费，完成或失败后必须在平台停止任务并确认状态。

模型准备阶段先从 `python_candidates` 寻找并验证镜像自带的 Torch/CUDA 环境。平台启动器的 Python 可能与镜像标签不同，不能直接据标签认定运行版本。验证 Torch、Torchaudio 版本及实际 CUDA 运算成功后，继承镜像的 GPU 库，其余 Python 依赖按锁文件安装；找不到符合配置的环境立即失败，避免重新下载整套 Torch/CUDA。最终以实测报告中的版本为准。

默认附带 `cosyvoice-source.tar.gz`，启动前验证配置中的 SHA-256，包含锁定版本的 CosyVoice 运行源码、Matcha-TTS 子模块、上游示例参考音频和许可证。这样无需占用 GPU 等待 GitHub 克隆。CosyVoice 使用 Apache-2.0，Matcha-TTS 使用 MIT；来源提交记录在包内 `CosyVoice/source-revisions.json`，来源链接见本文末尾。更新源码时必须重新构建源码包、核对许可证并更新散列，不能只修改版本字段。将 `source_archive` 留空时仍支持原来的 Git 获取方式。

模型虚拟环境默认建在容器本地 `/tmp/llmautotel-latency-worker`，避免向平台挂载的代码目录拷贝大量 Python 包；可用 `worker_environment` 修改。源码包校验失败与依赖准备失败会直接报告初始化失败，不会继续等待模型就绪。

```sh
uv run python experiments/openi_latency/client.py --url "$OPENI_URL" \
  --token-file data/openi-token --mode probe
uv run python experiments/openi_latency/client.py --url "$OPENI_URL" \
  --token-file data/openi-token --mode load
uv run python experiments/openi_latency/client.py --url "$OPENI_URL" \
  --token-file data/openi-token --mode status
uv run python experiments/openi_latency/client.py --url "$OPENI_URL" \
  --token-file data/openi-token --mode wait
uv run python experiments/openi_latency/client.py --url "$OPENI_URL" \
  --token-file data/openi-token --mode local --repeats 2 --output data/openi-local.json
uv run python experiments/openi_latency/client.py --url "$OPENI_URL" \
  --token-file data/openi-token --mode remote --repeats 2 --output data/openi-remote.json
```

可把客户端和 `protocol.py` 复制到媒体服务器执行 remote，得到服务器与 OpenI 的真实往返及有效首音。代理地址是平台给出的在线推理地址，不能用容器内 IP。

初始化等待使用认证 HTTP 轮询，避免官方代理关闭空闲 WebSocket；只有正式测试占用唯一音频连接。模型输出以 EOS、取消或超时结束，总长度受模型自身上下文窗口限制，避免 Transformers 默认 20 个新 token 截断回复。

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

## 2026-11-15 初始化与网络记录

前两次初始化尝试分别遇到整套 GPU 依赖下载超时、TTS 子模块 Git 克隆超时，未得到完整语音回复样本；对应任务均已停止。第二次已完成 129 个依赖包安装，进一步定位了 GitHub 子模块获取的问题，现使用上述预打包源码规避。

第二次任务的服务器→OpenI 官方代理 WebSocket 往返为 42.8、42.2、42.7、42.3、42.8 ms，中位数 42.7 ms。这仅说明该路径的小消息往返开销，不包含 ASR、LLM、TTS、音频编码和播放，不能作为 1 秒语音验收结果。后续完整样本须单独记录。

### 使用源码包的后续尝试

任务 `934853` 使用独立仓库提交 `0221405db5`，源码包散列校验和 129 个依赖包安装均已通过。实测解释器为 Python 3.11.11，Torch/Torchaudio 为 2.6.0+cu124，CUDA 12.4；与平台镜像名称中的 Python 3.10 标签不同。ASR 已加载成功，随后进入 Qwen 权重下载；13:06 的日志显示第一个 3.69 GiB 分片约 19%，TTS 尚未加载。

本次服务器→OpenI 的 WebSocket 往返为 38.8、38.5、38.9、44.5、39.1 ms，中位数 38.9 ms。完整模型回复样本仍为 **0**，同机和服务器转发的有效首音均未测得，不能判断是否达到 1 秒。初始化等待客户端最终出现 HTTP `ReadTimeout`；这不证明模型推理失败，也不代表推理耗时超过 1 秒。

为限制无结果的 GPU 用时，本轮结束初始化等待。平台已确认任务于 2026-11-15 13:12:15 停止，状态为 `STOPPED`；自 12:36:55 起运行 35 分 20 秒，按 9 积分/小时估算约 5.30 积分，非官方账单。媒体服务器上的临时测试令牌已删除，正式服务与配置未修改。

停止前已提交 `voice-latency-prepared-20261115` 环境缓存镜像，最近核对仍为“提交中”，不能认定缓存保存成功；未验证从镜像重建，也不表示全部模型权重已经就绪。后续应先准备并校验完整权重，再启动 GPU 实测，避免在 GPU 运行期间重复安装和长时间下载。本轮没有新增代码，沿用此前已通过的 8 项原型测试；本次只补充真实运行记录并检查文档差异。
