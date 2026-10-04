# LLMAutoTel

本机网页语音销售助手。配置销售目标、产品资料、话术和独立的 ASR、文本 LLM、TTS 服务，通过浏览器与 AI 对话。

## 开发环境

需要 uv、Python 3.12、Node.js 和 pnpm。项目用 `.python-version` 选择 Python 3.12，不依赖系统 Python 版本。

```sh
uv sync --locked
uv run llmautotel
```

前端开发另开终端：

```sh
cd frontend
pnpm install --frozen-lockfile
pnpm dev
```

前端开发服务器代理 `/api` 到本机后端。后端默认地址 `http://127.0.0.1:8765`。构建前端后，后端也会提供同源网页：

```sh
pnpm --dir frontend build
uv run llmautotel
```

进程环境变量见 `.env.example`，程序从环境读取变量，不自动加载该文件。数据默认写到 `data/`，目录权限 `0700`、SQLite 文件权限 `0600`，已排除出 Git。模型地址、密钥、模型名由网页配置；密钥只保存在服务端，回读只返回是否已设置。

## 验证

```sh
uv run pytest
uv run ruff check src tests
pnpm --dir frontend test
pnpm --dir frontend build
```

第一阶段按配置与持久化、模型接入、语音通话、文字历史四块交付。后续模块完成时同步补充接口与验收文档。
