# OpenI 三模型延迟原型（实验）

独立于正式语音应用。验证一张 NVIDIA GPU 同机运行流式 Paraformer、Qwen3-4B-Instruct-2507、CosyVoice3-0.5B 的延迟，使用 OpenI 官方在线推理代理传输 WebSocket，不使用隧道或端口穿透，不修改正式配置、历史和线路。模型、源代码版本、测试问题、提示词和限制见 `settings.json`；源代码与 Python 依赖均锁定版本。

这是选型原型，尚未接正式 Pipecat 管线、真实 VAD、电话、浏览器音频与连续对话。问题为内存中生成的合成语音，产品资料为虚构测试资料。这里的通过不等于正式通话验收。

## 运行

### 全站内模型基线

`settings.station.json` 使用站内 `SenseVoiceSmall`、`Qwen3-1.7B`、`Fun-CosyVoice3-0.5B-2512` 三个模型，保留原来的流式 Paraformer 配置。创建任务时选择三个预训练模型，并由此配置生成 `settings.run.json`。所有权重来自 `c2net.prepare()` 返回的站内路径，不依赖镜像的用户缓存，不回退外网下载。

这组 ASR 是整句识别，输入音频继续实时传输，但仅在停口后识别整段；LLM 和 TTS 继续流式输出。`asr_streaming` 默认 `true`，设为 `false` 时使用整句模式；`asr_generate_options` 可配置语言等模型识别参数。报告记录实际模式，不能将整句识别结果写成流式 ASR 的验收。识别输出的语言、情绪等特殊标记不会进入 LLM 的用户文本。

### 站内权重与镜像缓存实验

`settings.mounted.json` 是另一组独立实验配置：Qwen3-1.7B（关闭思考）、原来的流式 Paraformer、CosyVoice3-0.5B-2512。创建任务时选择站内 `Qwen3-1.7B` 和 `Fun-CosyVoice3-0.5B-2512` 两个预训练模型，使用 `voice-latency-prepared-20261115` 镜像；从该配置生成带令牌散列的 `settings.run.json`。原来的 `settings.json` 保留。

准备程序按[平台环境初始化文档](https://openi.pcl.ac.cn/docs/index.html#/cloudbrain/codepath)，通过镜像解释器调用 `c2net.context.prepare()`，使用返回的 `pretrain_model_path` 获取站内模型。ASR 从镜像保留的 ModelScope 缓存读取。目录名、缓存根目录和必需文件均可配置；缺失权重、缺失分片、空文件或不唯一的缓存立即失败。此配置禁止回退外网下载权重，Transformers 使用离线模式。平台仍可能需要在站内同步文件，不能保证准备零等待。

文件检查通过后才加载模型。每次最多 12 个模型回合，准备模型最多等待 600 秒；网关有效期为 20 分钟，**不会自动停止 GPU 计费**，测试完成或到达本轮操作时限后仍须停止平台任务并核对终止状态。首次应分别核实缓存镜像重建、站内模型准备和真正的推理延迟，不能用模型目录存在代替运行验证。

2026-11-15 后续核对，缓存镜像已经显示“提交成功”，以下历史段落的“提交中”是当时状态；能否重建和权重是否完整以本轮实测为准。

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

### 站内模型首次准备核对

任务 `934882` 使用 `cd1e0812dd`，从已提交成功的缓存镜像启动；实际 GPU 运算和 Torch/Torchaudio 2.6.0+cu124 检查通过，`c2net.prepare()` 也已完成 Qwen3-1.7B 与 CosyVoice3 的站内准备。但配置的 ModelScope 缓存根目录未找到流式 ASR，报 `cached_model_expected_one: asr, found=0`，在依赖安装和三模型加载之前终止；没有外网权重回退。已验证站内准备链路，不代表三模型推理通过。

官方代理 WebSocket 往返中位数 40.0 ms，完整模型回复样本仍为 0。平台确认本任务自 13:48:35 运行至 13:53:05，状态 `STOPPED`，用时 4 分 30 秒；按 9 积分/时估算 0.675 积分，非官方账单。此前误启动的旧配置任务 `934875` 已于 13:36:45 停止，仅运行 20 秒（估算 0.05 积分）；未发送模型加载指令。所有时间均为 2026-11-15 Asia/Shanghai。

由于用户缓存未能复用，下一组改为上述三个全站内模型，先验证完整链路再比较延迟；不再假定“镜像提交成功”就包含全部权重。

### 全站内基线初始化核对

任务 `934894` 使用 `decf46f48a`，三个站内模型均已准备并通过文件完整性校验，SenseVoiceSmall 和 Qwen3-1.7B 均已加载。CosyVoice3 的 Matcha-TTS 依赖在导入 `matcha.utils.utils` 时缺少 `wget`，尚未开始合成；现已将该源码实际依赖的 `wget==3.2` 加入输入清单并重新生成锁文件，其余依赖版本未变。

网关往返中位数为 42.9 ms，完整模型回复样本为 **0**。平台确认任务从 2026-11-15 14:07:15 运行至 14:11:25（Asia/Shanghai），状态 `STOPPED`，用时 4 分 10 秒；按 9 积分/时估算 0.625 积分，非官方账单。本次没有从外网下载模型权重，也没有修改正式服务。
