# 语音工作台前端

React、TypeScript 与 Vite 实现，使用 pnpm；依赖版本由 `package.json` 和 `pnpm-lock.yaml` 固定。

```sh
pnpm install --frozen-lockfile
pnpm dev
```

开发服务默认只监听本机。`/api` 代理到本机后端 `http://127.0.0.1:8765`，可复制 `.env.example` 到 `.env.local`，通过 `LLMAUTOTEL_API_URL` 修改代理目标。供应商密钥只能在配置页面输入，不要放进前端环境变量。

```sh
pnpm build
pnpm test
```

`build` 同时执行 TypeScript 检查和生产构建。测试覆盖配置保存、保留/清除密钥、保存失败恢复以及 API 错误提示。

配置回读只包含密钥是否存在；空密钥输入在保存时省略，显式清除使用 `null`。成功保存后清空密钥输入，不使用浏览器持久存储。语音与历史模块在后续提交接入。
