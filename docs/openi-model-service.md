# OpenI 常驻三模型服务

依据用户最新要求保持 GPU 运行，自有服务器仍负责通话入口、上下文、业务提示词、工具校验、打断、播放与历史。OpenI 同 GPU 常驻 SenseVoiceSmall、Qwen3-1.7B、Qwen3-TTS Base0.6B，分别提供标准兼容接口。旧供应商配置备份后再切换，不删除旧 provider。持续服务和此前可自动过期的独立实验是不同启动模式。

## 启动与资源

使用 `experiments/openi_latency/settings.service.json` 模板，复制为实验私库的 `settings.run.json`，只填强随机 API 令牌的 SHA256，原令牌保存在服务器受限权限配置中。模板不含供应商凭据。`serve_models=true` 时启动器复用已验证的镜像、权重校验、ASR 隔离进程和依赖锁，直接启动常驻模型 HTTP 服务；不经过实验的两小时客户端窗口、70轮预算或自动测试循环。模型加载、warmup 完成才开放端口，不将准备过程当成 ready。

端口取 OpenI 的 `OPENI_SELF_PORT`，不可在业务配置里猜端口。挂载站内 SenseVoiceSmall 与 Qwen3-1.7B，镜像复用 `voice-qwen3-weights-20261115` 的 TTS 权重和参考音。不会在本地下载权重或推理。当前 A10040GB 规格标价9积分/小时，全天约216积分；平台自身资源回收、代理地址失效及配额不是本应用能保证的永久在线能力。任务重建可能更换域名，需更新服务器三组地址。

## API 与配置

所有模型接口使用 `Authorization: Bearer ...`，仅 `/health` 无需鉴权；不保存录音、PCM、对话日志或原始密钥，不启用访问日志。请求体最多8MiB，ASR最多120秒，LLM输入上限16384 token、单次生成768 token/30秒，TTS每次最多2000字。上限是独立配置，不回写业务资料。

| 角色 | 接口 | 模型与参数 |
| --- | --- | --- |
| ASR | POST `/v1/audio/transcriptions` | multipart `model=FunAudioLLM/SenseVoiceSmall`，`file`为16kHz单声道16bit WAV，返回`text`；整句识别 |
| LLM | POST `/v1/chat/completions` | `model=Qwen/Qwen3-1.7B`；SSE或完整JSON，关闭思考，完整接收服务器消息与函数工具 |
| TTS | POST `/v1/audio/speech` | `model=Qwen/Qwen3-TTS-12Hz-0.6B-Base`，`voice=reference`，`response_format=pcm`，24kHz单声道16bitLE流 |

服务器三组根地址均填 `https://<实际OpenI代理>/v1`，ASR/TTS协议选兼容OpenAI。TTS仅当前镜像公开参考音色，不声称支持任意音色或风格指令；旧MiMo专属语气字段需要在切换配置时清空。默认没有暴露任意模型下载、代码执行、令牌回读或重启接口。

Qwen工具标签按任意文本块边界解析为标准`delta.tool_calls`，不将工具JSON播报；名称必须属于请求提供的工具，参数必须是对象，标签缺失或不完整会失败。是否真正执行挂断、证据及挽留校验仍在原服务器执行，小模型支持工具协议不代表业务准确性已验收。

ASR、LLM、TTS分别加锁，允许LLM继续生成时TTS合成已完成小句；不将三个接口串进一个全局锁。每个流独立取消标记与有界队列，HTTP断连后丢弃旧结果，旧生成退出前不复用相同模型；TTS每句PCM实际流式返回。服务关闭时释放ASR子进程。未知模型、音色、格式明确拒绝，不能静默换用其他模型。

## 切换与验证

先备份服务器原配置（含凭据，只存在服务器0700目录/0600文件），验证GPU健康、鉴权、真实TTS PCM、TTS→WAV→ASR、真实流式LLM和工具，再通过本机配置API保存三组地址与专属随机令牌。已有通话使用开始时快照，配置影响下一通；不强行中断当前通话。验证保存结果只输出脱敏配置。

通用HTTP接口比同GPU实验管线多了服务器往返和业务提示词，不能把实验的首音数字直接称为线上结果。真实VAD最低配置仍为200ms，与实验100ms模拟端点不同；连续服务部署不更改这一验证边界。服务运行与当前健康、任务ID、代理地址和实际联通验证结果在部署完成后记录。
