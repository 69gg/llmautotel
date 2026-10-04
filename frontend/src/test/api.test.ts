import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError, request } from '../api';

afterEach(() => { vi.unstubAllGlobals(); vi.unstubAllEnvs(); });

describe('服务请求', () => {
  it('显示后端业务错误', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: '已有一通会话' }), { status: 409 })));
    await expect(request('/api/settings')).rejects.toThrow('已有一通会话');
  });

  it('校验错误不暴露请求中的密钥或数据', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: [{ input: 'secret-data' }] }), { status: 422 })));
    await expect(request('/api/settings')).rejects.toThrow('请检查填写内容后重试。');
  });

  it('网络故障提供可操作提示', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
    await expect(request('/api/settings')).rejects.toEqual(new ApiError('无法连接服务，请确认后端已启动。', 0));
  });

  it('超过配置期限的请求被取消并显示中文提示', async () => {
    vi.stubEnv('VITE_API_TIMEOUT_MS', '5');
    let aborted = false;
    vi.stubGlobal('fetch', vi.fn((_path: string, options: RequestInit) => new Promise<Response>((_resolve, reject) => {
      options.signal?.addEventListener('abort', () => { aborted = true; reject(options.signal?.reason); }, { once: true });
    })));
    await expect(request('/api/calls', { method: 'POST' })).rejects.toThrow('服务响应超时');
    expect(aborted).toBe(true);
  });

  it('外部取消用于组件卸载，不显示为网络故障', async () => {
    const controller = new AbortController();
    vi.stubGlobal('fetch', vi.fn((_path: string, options: RequestInit) => new Promise<Response>((_resolve, reject) => {
      options.signal?.addEventListener('abort', () => reject(options.signal?.reason), { once: true });
    })));
    const pending = request('/api/settings', { signal: controller.signal });
    controller.abort();
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' });
  });
});
