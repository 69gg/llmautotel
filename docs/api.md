# 本机 API

所有接口以 `/api` 为前缀。当前服务面向本机单用户，默认仅监听 `127.0.0.1`。

| 方法 | 路径 | 行为 |
| --- | --- | --- |
| GET | `/health` | 返回 `{ "status": "ok" }` |
| GET | `/settings` | 返回销售、模型与语音配置，模型含 `api_key_set`，不返回密钥 |
| PUT | `/settings` | 保存完整配置，返回脱敏后的配置 |

配置包含 `sales`（`goal`、`product_info`、`instructions`、`opening`）、`asr`、`llm`、`tts` 和 `voice`。模型公共字段为 `base_url`、`model`、`timeout_seconds`。ASR 增加 `language`；TTS 增加 `voice`、`sample_rate`。语音配置为 `vad_start_seconds`、`vad_stop_seconds`、`vad_confidence`。

保存模型密钥时：省略 `api_key` 表示保持原值，字符串表示替换，`null` 表示清除。保存时不要携带只读字段 `api_key_set`。配置允许暂存空字段；开始通话前检查必要配置。

校验错误返回 HTTP 422，`detail` 列表仅包含字段位置、错误类型与说明，不回显输入值。
